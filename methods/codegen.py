"""Program-synthesis baseline P: the LLM writes the decision procedure directly as a Python function
from the same two public documents the compiler sees (policy.md and schema.md), with no specification
language and no evaluator. The generated program runs in a restricted subprocess (codegen_runner.py) that
exposes only `query`; the parent process owns the shared SQLTool, so the query log, the budget (12
queries, 40 rows, 400 chars) and the observed-evidence accounting are exactly those of every other method.

Generation: one LLM call per draw (same backbone and sampling as the compiler: thinking on, temperature
0.6, top_p 0.95, 8192 generated tokens). A draw whose program fails to import or does not define
decide(alert, query) is returned to the model with the error for at most MAX_REPAIRS repair rounds;
there is never feedback on case outcomes. Every attempt and every raw reply is kept.

Usage: codegen.py RUN_NAME K --docs DIR   (draws K independent programs into runs/RUN_NAME/draws/)
Selection rule, fixed before any case is run: highest development c&g, ties -> lowest index.
"""
import argparse
import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "methods"))
from llm import LLM  # noqa: E402
from sqltool import BudgetExhausted  # noqa: E402

MAX_REPAIRS = 2
EVIDENCE_CAP = 8
CASE_TIMEOUT_S = 60
RUNNER = ROOT / "methods" / "codegen_runner.py"

PREAMBLE = """You are writing the decision procedure of an authorization-audit tool for an application. The tool
decides, for one recorded operation at time t, whether it was authorized under the organization's
written policy, using only records in a read-only database. Write the procedure as a Python function
from the application's policy and schema documents below. Apply the policy exactly as written, using
only the tables and columns in the schema document. Do not invent tables, columns or action values.

Deliver one self-contained Python module that defines

    def decide(alert, query):

Interface:
- `alert` is a dict with keys `case_id`, `alert` and `question`. `alert["alert"]` describes the
  operation under review: `audit_log_id` is the id of the operation's row in the table `audit_log` and
  `ts` is that row's ISO-8601 UTC timestamp; its other keys are descriptive fields about the operation.
  Read everything else from the database.
- `query(sql, params=())` runs one read-only SQL statement (SQLite dialect; JSON functions such as
  json_extract are available; `params` is a list or tuple of JSON-serialisable values bound to `?`
  placeholders). It returns {"ok": true, "rows": [...], "truncated": bool, "remaining_queries": int}
  with each row a dict mapping column name to value, or {"ok": false, "error": str,
  "remaining_queries": int} on an SQL error. Budget per case: at most 12 queries, at most 40 rows per
  query (more are cut off and `truncated` is true), string cells longer than 400 characters are cut.
  The 13th query raises BudgetExhausted (a RuntimeError subclass available as a module-level name;
  `query` is also available as a module-level name).
- `decide` returns a dict with the keys the policy's "What to return" section names: `verdict` (one of
  "authorized", "unauthorized", "insufficient_evidence"), `evidence` (a list of at most 8 items in the
  citation forms the policy states; an item may additionally carry "role": "supports_authorization",
  "supports_violation" or "context"), the principal field named there (null if not established) and
  `unresolved` (a list of strings).
- The function is called once per alert in a fresh process with a 60-second limit. An exception, a
  timeout or a non-dict return value counts as a failed case. Only the standard-library modules json,
  datetime, re, math, collections, itertools, functools, operator, string, typing, dataclasses, enum,
  decimal, bisect, heapq, copy, time and calendar can be imported; there is no file, network or
  process access.

Reply with the complete module in one ```python code block and nothing else.
"""

SYSTEM = "You write software precisely. Reply with one Python program in a single ```python code block and nothing else."
REPAIR = ("The program failed to import or does not define decide(alert, query): {error}\n"
          "Correct it and reply with the complete corrected program in one ```python code block.")


class CodegenError(RuntimeError):
    """A generated program crashed, timed out or could not be run; recorded as method_error per case."""

    def __init__(self, message, child_traceback=""):
        super().__init__(message)
        self.child_traceback = child_traceback


def codegen_prompt(docs_dir):
    docs = Path(docs_dir)
    return (PREAMBLE + "\n# Application documents\n\n" + (docs / "policy.md").read_text() + "\n\n" +
            (docs / "schema.md").read_text())


def extract_code(text):
    """The program is the longest fenced code block; an unterminated fence runs to the end of the reply;
    without any fence the whole reply is the program."""
    blocks = re.findall(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", text, flags=re.S)
    if blocks:
        return max(blocks, key=len), "fenced"
    m = re.search(r"```(?:python|py)?[ \t]*\r?\n", text)
    if m:
        return text[m.end():], "unterminated_fence"
    return text.strip(), "whole_reply"


def _spawn(code_path):
    return subprocess.Popen([sys.executable, "-I", "-S", str(RUNNER), str(code_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


def _finish(proc, timer):
    timer.cancel()
    if proc.poll() is None:
        proc.kill()
    try:
        stderr = proc.stderr.read()
    except Exception:
        stderr = ""
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    for s in (proc.stdin, proc.stdout, proc.stderr):
        try:
            s.close()
        except Exception:
            pass
    return (stderr or "")[-2000:]


def _talk(proc, request, tool, timeout):
    """Runs the JSON-lines protocol with the child in the calling thread (the SQLTool's sqlite
    connection is bound to this thread). Returns (state, stderr_tail, timed_out)."""
    state = {"result": None, "error": None, "done": False}
    flags = {"timed_out": False}

    def on_timeout():
        flags["timed_out"] = True
        if proc.poll() is None:
            proc.kill()

    timer = threading.Timer(timeout, on_timeout)
    timer.daemon = True
    timer.start()
    try:
        proc.stdin.write(json.dumps(request, default=str) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                state["error"] = {"type": "ProtocolError", "message": f"unreadable line from runner: {line[:200]!r}"}
                state["done"] = True
                break
            op = msg.get("op")
            if op == "query":
                sql, params = msg.get("sql"), msg.get("params")
                if tool is None:
                    reply = {"ok": False, "error": "no database in check mode", "remaining_queries": 0}
                elif not isinstance(sql, str):
                    reply = {"ok": False, "error": "sql must be a string", "remaining_queries": tool.max_queries - tool.queries}
                else:
                    try:
                        reply = tool.query(sql, tuple(params) if isinstance(params, (list, tuple)) else ())
                    except BudgetExhausted as exc:
                        reply = {"budget_exhausted": True, "error": str(exc)}
                    except Exception as exc:  # unexpected tool failure (not an SQL error)
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                                 "remaining_queries": tool.max_queries - tool.queries}
                proc.stdin.write(json.dumps(reply, default=str) + "\n")
                proc.stdin.flush()
            elif op == "result":
                state["result"], state["done"] = msg.get("value"), True
                break
            elif op == "error":
                state["error"], state["done"] = msg, True
                break
            else:
                state["error"] = {"type": "ProtocolError", "message": f"unknown op {op!r}"}
                state["done"] = True
                break
    except (BrokenPipeError, OSError, ValueError) as exc:
        if not state["done"]:
            state["error"] = {"type": type(exc).__name__, "message": str(exc)[:300]}
    stderr = _finish(proc, timer)
    return state, stderr, flags["timed_out"]


def check_program(code_path, timeout=CASE_TIMEOUT_S):
    """Import check in the restricted runner (no database): (ok, error_message, detail)."""
    state, stderr, timed_out = _talk(_spawn(code_path), {"op": "check"}, None, timeout)
    if timed_out:
        return False, f"timeout: importing the program took more than {timeout} s", stderr
    if state["error"]:
        e = state["error"]
        return False, f"{e.get('type')}: {e.get('message')}", e.get("traceback", "") or stderr
    if not state["done"]:
        return False, "runner exited without a result", stderr
    return True, None, ""


def run_program(code_path, tool, timeout=CASE_TIMEOUT_S):
    """One case: (final, trace). Raises CodegenError on a crash, a timeout or a runner failure (the
    runner records it as method_error); returns (None, trace) when decide() returns a non-dict
    (invalid_output). Evidence beyond EVIDENCE_CAP items is cut, as the other methods' schemas cap it."""
    state, stderr, timed_out = _talk(_spawn(code_path), {"op": "run", "alert": tool.alert}, tool, timeout)
    trace = [{"phase": "program", "code": str(code_path), "timed_out": timed_out, "child_stderr": stderr}]
    if timed_out:
        raise CodegenError(f"timeout: decide() did not return within {timeout} s", stderr)
    if state["error"]:
        e = state["error"]
        raise CodegenError(f"{e.get('type')}: {e.get('message')}"[:300], e.get("traceback", "") or stderr)
    if not state["done"]:
        raise CodegenError("runner exited without a result", stderr)
    final = state["result"]
    if not isinstance(final, dict):
        trace[0]["non_dict_result"] = repr(final)[:300]
        return None, trace
    ev = final.get("evidence")
    if isinstance(ev, list) and len(ev) > EVIDENCE_CAP:
        trace[0]["evidence_truncated_from"] = len(ev)
        final["evidence"] = ev[:EVIDENCE_CAP]
    return final, trace


def generate_program(llm, docs_dir, out_dir, index, max_repairs=MAX_REPAIRS):
    """One draw: returns the record (also written to out_dir/gen_<index>.json); the program of every
    attempt is kept as draw_<index>_attempt<r>.py and the final one as draw_<index>.py."""
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": codegen_prompt(docs_dir)}]
    rec = {"index": index, "docs": str(docs_dir), "max_repairs": max_repairs, "attempts": []}
    valid, code_path = False, None
    for rnd in range(max_repairs + 1):
        out = llm.chat(msgs)
        code, how = extract_code(out["content"])
        (out_dir / f"draw_{index}_attempt{rnd}.raw.txt").write_text(out["content"])
        code_path = out_dir / f"draw_{index}_attempt{rnd}.py"
        code_path.write_text(code)
        ok, err, detail = check_program(code_path)
        att = {"round": rnd, "valid": ok, "extraction": how, "finish_reason": out["finish_reason"],
               "reasoning_chars": len(out["reasoning"]), "code_chars": len(code), "code_lines": code.count("\n") + 1}
        if ok:
            rec["attempts"].append(att)
            valid = True
            break
        att.update(error=(err or "")[:300], detail=(detail or "")[-1500:])
        rec["attempts"].append(att)
        msgs.append({"role": "assistant", "content": out["content"]})
        msgs.append({"role": "user", "content": REPAIR.format(error=(err or "")[:300])})
    (out_dir / f"draw_{index}.py").write_text(code_path.read_text())
    rec.update(ok=True, valid=valid, repairs_used=len(rec["attempts"]) - 1, cost=llm.cost(),
               program=f"draw_{index}.py")
    if not valid:
        rec["validation_error"] = rec["attempts"][-1].get("error")
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_name")
    ap.add_argument("k", type=int)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:8100")
    ap.add_argument("--model", default="qwen3-32b")
    ap.add_argument("--max-repairs", type=int, default=MAX_REPAIRS)
    args = ap.parse_args()
    out = ROOT / "runs" / args.run_name / "draws"
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.txt").write_text(codegen_prompt(args.docs))
    for i in range(args.k):
        llm = LLM(args.base_url, args.model, temperature=0.6)
        started = datetime.now(timezone.utc).isoformat()
        try:
            rec = generate_program(llm, args.docs, out, i, max_repairs=args.max_repairs)
        except Exception as exc:
            rec = {"index": i, "docs": args.docs, "ok": False, "valid": False,
                   "error": f"{type(exc).__name__}: {exc}"[:300], "cost": llm.cost()}
        rec.update(started_at=started, finished_at=datetime.now(timezone.utc).isoformat(),
                   backbone={"base_url": args.base_url, "model": args.model, "temperature": llm.temperature,
                             "top_p": llm.top_p, "max_tokens": llm.max_tokens, "thinking": llm.thinking})
        (out / f"gen_{i}.json").write_text(json.dumps(rec, indent=1))
        print(json.dumps({"index": i, "ok": rec["ok"], "valid": rec.get("valid"), "repairs_used": rec.get("repairs_used"),
                          "cost": rec.get("cost")}))


if __name__ == "__main__":
    main()
