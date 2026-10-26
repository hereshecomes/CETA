"""Sensitivity variants S1 / S2 of the program-synthesis baseline (generation only). Development
evaluation, selection and test evaluation of the draws written here use evaluation/codegen_run.py with
--skip-generate.

S1: the original preamble (codegen.PREAMBLE, imported, not copied) with the per-call generation limit raised
    from 8192 to 32768 tokens, capped so that prompt + generation stay inside the server's context (40960):
    limit = min(32768, 40960 - P - 256), P = the prompt's token count as returned by the server's /tokenize
    endpoint for the same messages (fallback if that endpoint fails: ceil(chars / 2.5), an over-estimate).
    The HTTP transport timeout is 7200 s instead of 600 s; the 2 transport retries are unchanged.
S2: S1 plus the abstract worked example EXAMPLE (below) appended to the preamble, where the compiler's
    manual carries its abstract example: after the preamble, before "# Application documents".
Everything else (system message, program extraction, import check in the restricted runner, repair rule
and repair message, record format) is the baseline code, imported from codegen.py.

A draw whose generation fails for a transport reason (llm.InfraError after the client's retries) is
regenerated once with the same settings; the failed record and its attempt files are kept with the suffix
`.infra_failure<n>`. A model reply is never regenerated.

Usage: codegen_sens.py RUN_NAME K --docs DIR --variant S1|S2 [--max-tokens 32768] [--context-limit 40960]
       [--margin 256] [--http-timeout 7200] [--parallel 2]
"""
import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "methods"))
import codegen  # noqa: E402  (baseline: PREAMBLE, SYSTEM, REPAIR, MAX_REPAIRS, extract_code, check_program)
from llm import LLM, InfraError  # noqa: E402

VARIANTS = ("S1", "S2")
DEFAULT_MAX_TOKENS = 32768
DEFAULT_CONTEXT_LIMIT = 40960
DEFAULT_MARGIN = 256
DEFAULT_HTTP_TIMEOUT = 7200

# The abstract worked example of variant S2. Names are
# invented and resemble neither application; the example policy declares its grant table complete, so the
# program's `unauthorized` on an absent grant follows that sentence of the example policy.
EXAMPLE = """
### Abstract example (names are invented; use the application's own)
An example application records door unlocks. `audit_log(id, ts, action, badge_id, door, details)` holds
one row per recorded event; unlocks are the rows with `action = 'door.unlocked'`. `badges(id, holder)`
lists the user each badge was issued to; `doors(id, zone)` places each door in a zone;
`zone_access(id, user, zone, valid_from, valid_to)` lists the periods during which a user may enter a
zone (`valid_to` NULL = open-ended); the table is complete: a user without a matching row had no
access. Policy: the badge's holder is accountable for the unlock; an unlock at time t is authorized if
and only if the holder has a `zone_access` row for the door's zone whose period contains t; if the
holder cannot be established, the answer is `insufficient_evidence`. What to return: `verdict`,
`evidence` (rows cited as `{"source": <table>, "id": <row id>}`), `holder` (if established) and
`unresolved`.

```python
def decide(alert, query):
    op_id = alert["alert"]["audit_log_id"]
    t = alert["alert"]["ts"]
    r = query("SELECT id, ts, badge_id, door FROM audit_log WHERE id = ?", [op_id])
    if not r["ok"] or not r["rows"]:
        return {"verdict": "insufficient_evidence", "evidence": [], "holder": None,
                "unresolved": ["operation row %s not found" % op_id]}
    op = r["rows"][0]
    evidence = [{"source": "audit_log", "id": op["id"], "role": "context"}]
    r = query("SELECT id, holder FROM badges WHERE id = ?", [op["badge_id"]])
    if not r["ok"] or not r["rows"]:
        return {"verdict": "insufficient_evidence", "evidence": evidence, "holder": None,
                "unresolved": ["holder of badge %s" % op["badge_id"]]}
    badge = r["rows"][0]
    evidence.append({"source": "badges", "id": badge["id"], "role": "context"})
    r = query("SELECT id, zone FROM doors WHERE id = ?", [op["door"]])
    if not r["ok"] or not r["rows"]:
        return {"verdict": "insufficient_evidence", "evidence": evidence, "holder": badge["holder"],
                "unresolved": ["zone of door %s" % op["door"]]}
    door = r["rows"][0]
    evidence.append({"source": "doors", "id": door["id"], "role": "context"})
    r = query("SELECT id FROM zone_access WHERE user = ? AND zone = ? AND valid_from <= ? "
              "AND (valid_to IS NULL OR valid_to >= ?)", [badge["holder"], door["zone"], t, t])
    if r["ok"] and r["rows"]:
        evidence.append({"source": "zone_access", "id": r["rows"][0]["id"], "role": "supports_authorization"})
        return {"verdict": "authorized", "evidence": evidence, "holder": badge["holder"], "unresolved": []}
    return {"verdict": "unauthorized", "evidence": evidence, "holder": badge["holder"], "unresolved": []}
```
"""


def example_program():
    """The program of the abstract example alone (for the smoke test of the example in the runner)."""
    code, how = codegen.extract_code(EXAMPLE)
    assert how == "fenced"
    return code


def preamble(variant):
    if variant == "S1":
        return codegen.PREAMBLE
    if variant == "S2":
        return codegen.PREAMBLE + EXAMPLE
    raise ValueError(f"unknown variant {variant!r}")


def sens_prompt(docs_dir, variant):
    """S1: byte-identical to codegen.codegen_prompt(docs_dir). S2: the example between the preamble and
    the application documents."""
    docs = Path(docs_dir)
    return (preamble(variant) + "\n# Application documents\n\n" + (docs / "policy.md").read_text() + "\n\n" +
            (docs / "schema.md").read_text())


def count_prompt_tokens(base_url, model, messages, timeout=120):
    """Prompt tokens of the chat messages as the server counts them (POST /tokenize applies the chat
    template with the generation prompt); fallback: ceil(characters / 2.5), an over-estimate."""
    try:
        resp = requests.post(f"{base_url.rstrip('/')}/tokenize",
                             json={"model": model, "messages": messages, "add_generation_prompt": True}, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data.get("count"), int):
                return data["count"], "server_tokenize"
    except Exception:  # noqa: BLE001 - any failure falls back to the character estimate
        pass
    return math.ceil(sum(len(m["content"]) for m in messages) / 2.5), "chars_over_2.5"


def generate_program_sens(llm, docs_dir, out_dir, index, variant, max_tokens, context_limit, margin,
                          max_repairs=codegen.MAX_REPAIRS, rec=None):
    """One draw (the loop of codegen.generate_program with a per-call generation limit and per-attempt
    accounting). `rec` is filled progressively so that a transport failure leaves the attempts made so
    far in the failure record."""
    msgs = [{"role": "system", "content": codegen.SYSTEM}, {"role": "user", "content": sens_prompt(docs_dir, variant)}]
    if rec is None:
        rec = {}
    rec.update({"index": index, "docs": str(docs_dir), "variant": variant, "max_repairs": max_repairs, "attempts": []})
    valid, code_path = False, None
    for rnd in range(max_repairs + 1):
        counted, how_counted = count_prompt_tokens(llm.base_url, llm.model, msgs)
        cap = min(max_tokens, context_limit - counted - margin)
        llm.max_tokens = cap
        before = llm.cost()
        t0 = time.time()
        out = llm.chat(msgs)
        after = llm.cost()
        code, how = codegen.extract_code(out["content"])
        (out_dir / f"draw_{index}_attempt{rnd}.raw.txt").write_text(out["content"])
        (out_dir / f"draw_{index}_attempt{rnd}.reasoning.txt").write_text(out["reasoning"])
        code_path = out_dir / f"draw_{index}_attempt{rnd}.py"
        code_path.write_text(code)
        ok, err, detail = codegen.check_program(code_path)
        att = {"round": rnd, "valid": ok, "extraction": how, "finish_reason": out["finish_reason"],
               "reasoning_chars": len(out["reasoning"]), "content_chars": len(out["content"]),
               "code_chars": len(code), "code_lines": code.count("\n") + 1,
               "max_tokens_call": cap, "prompt_tokens_counted": counted, "prompt_count_method": how_counted,
               "prompt_tokens": after["prompt_tokens"] - before["prompt_tokens"],
               "completion_tokens": after["completion_tokens"] - before["completion_tokens"],
               "seconds": round(time.time() - t0, 1)}
        if ok:
            rec["attempts"].append(att)
            valid = True
            break
        att.update(error=(err or "")[:300], detail=(detail or "")[-1500:])
        rec["attempts"].append(att)
        msgs.append({"role": "assistant", "content": out["content"]})
        msgs.append({"role": "user", "content": codegen.REPAIR.format(error=(err or "")[:300])})
    (out_dir / f"draw_{index}.py").write_text(code_path.read_text())
    rec.update(ok=True, valid=valid, repairs_used=len(rec["attempts"]) - 1, cost=llm.cost(),
               program=f"draw_{index}.py")
    if not valid:
        rec["validation_error"] = rec["attempts"][-1].get("error")
    return rec


def _keep_failed_attempt_files(out_dir, index, n):
    for p in sorted(out_dir.glob(f"draw_{index}_attempt*")):
        if ".infra_failure" in p.name:
            continue
        stem, suffix = p.name.split(".", 1)
        p.rename(out_dir / f"{stem}.infra_failure{n}.{suffix}")


def draw(args, out, i):
    backbone = {"base_url": args.base_url, "model": args.model, "temperature": 0.6, "top_p": 0.95,
                "max_tokens": args.max_tokens, "context_limit": args.context_limit, "margin": args.margin,
                "http_timeout": args.http_timeout, "thinking": True, "variant": args.variant}
    rec, n_infra = None, 0
    for regen in range(2):  # at most one regeneration, only after a transport failure
        llm = LLM(args.base_url, args.model, temperature=0.6, max_tokens=args.max_tokens, timeout=args.http_timeout)
        started = datetime.now(timezone.utc).isoformat()
        rec = {}
        try:
            rec = generate_program_sens(llm, args.docs, out, i, args.variant, args.max_tokens, args.context_limit,
                                        args.margin, max_repairs=args.max_repairs, rec=rec)
            break
        except InfraError as exc:
            rec.update(index=i, docs=str(args.docs), variant=args.variant, ok=False, valid=False,
                       error=f"InfraError: {exc}"[:300], cost=llm.cost(), started_at=started,
                       finished_at=datetime.now(timezone.utc).isoformat(), backbone=backbone)
            (out / f"gen_{i}.infra_failure{n_infra}.json").write_text(json.dumps(rec, indent=1))
            _keep_failed_attempt_files(out, i, n_infra)
            n_infra += 1
            if regen == 0:
                print(json.dumps({"index": i, "infra_failure": str(exc)[:200], "regenerating": True}), flush=True)
                continue
        except Exception as exc:  # noqa: BLE001 - recorded as a failed draw, as in the baseline
            rec.update(index=i, docs=str(args.docs), variant=args.variant, ok=False, valid=False,
                       error=f"{type(exc).__name__}: {exc}"[:300], cost=llm.cost())
            break
    rec.update(started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), backbone=backbone,
               infra_failures=n_infra)
    (out / f"gen_{i}.json").write_text(json.dumps(rec, indent=1))
    print(json.dumps({"index": i, "ok": rec["ok"], "valid": rec.get("valid"), "repairs_used": rec.get("repairs_used"),
                      "cost": rec.get("cost"), "infra_failures": n_infra,
                      "attempts": [(a["finish_reason"], a["completion_tokens"], a["valid"]) for a in rec.get("attempts", [])]}),
          flush=True)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_name")
    ap.add_argument("k", type=int)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--variant", required=True, choices=VARIANTS)
    ap.add_argument("--base-url", default="http://127.0.0.1:8100")
    ap.add_argument("--model", default="qwen3-32b")
    ap.add_argument("--max-repairs", type=int, default=codegen.MAX_REPAIRS)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--context-limit", type=int, default=DEFAULT_CONTEXT_LIMIT)
    ap.add_argument("--margin", type=int, default=DEFAULT_MARGIN)
    ap.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    ap.add_argument("--parallel", type=int, default=2, help="concurrent draws (independent; wall time only)")
    args = ap.parse_args()
    out = ROOT / "runs" / args.run_name / "draws"
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.txt").write_text(sens_prompt(args.docs, args.variant))
    (out / "variant.json").write_text(json.dumps({"variant": args.variant, "max_tokens": args.max_tokens,
                                                  "context_limit": args.context_limit, "margin": args.margin,
                                                  "http_timeout": args.http_timeout, "parallel": args.parallel,
                                                  "k": args.k, "docs": str(args.docs), "model": args.model,
                                                  "base_url": args.base_url}, indent=1))
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        list(pool.map(lambda i: draw(args, out, i), range(args.k)))


if __name__ == "__main__":
    main()
