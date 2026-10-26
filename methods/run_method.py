"""Run one method over one split of one application and score every case independently. Nothing is
filtered: infrastructure failures, invalid outputs and budget exhaustion are kept as records.

Usage: run_method.py METHOD SPLIT RUN_NAME [--app pretix|weblate|wagtail|indico] [--backbone NAME]
       [--base-url URL --model M --thinking on|off --concurrency N --rep R --spec FILE --code FILE --tag T]
       [--cases DIR]

Methods:
- rules, rules_closed_world, rules_current_state (expert rules R and their weak variants; Wagtail also has
  rules_no_time_limit and rules_no_separation_of_duty);
- compiled (CETA: the deterministic evaluator on the specification given by --spec),
  compiled_closed_world (E1), compiled_current_state (E2), compiled_no_after (E3),
  compiled_no_fallback (E4), compiled_no_coverage (E5), and compiled_abl (the ablation copy with every
  switch off; equals compiled on every case);
- sql_agent (G1), hypothesis_verify (G2), explore_reflect (G3), timeline_agent (G4) and their _informed
  variants;
- pool_judge (J) and pool_judge_informed;
- codegen (P: the generated program given by --code, run in the restricted runner).

--backbone selects a profile of methods/backbones.json (base URL, served model name, thinking flag,
chat_template_kwargs, role merging); explicit --base-url / --model / --thinking win. Results go to
runs/RUN_NAME/METHOD[TAG]_rep[R]/results.jsonl with one trajectory file per case.
"""
import argparse
import importlib
import json
import sqlite3
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "methods"))
sys.path.insert(0, str(ROOT / "evaluation"))
import app_profile  # noqa: E402
import app_profile_ext  # noqa: E402,F401  (profiles of the appended applications A3+, registered on import)

APP = app_profile.select(next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--app"), "pretix"))
import agents  # noqa: E402
import agents_reflect_timeline  # noqa: E402
import codegen  # noqa: E402
import evaluator  # noqa: E402
import evaluator_ablations  # noqa: E402
import pool_judge as pc  # noqa: E402
from llm import LLM, InfraError  # noqa: E402
from sqltool import SQLTool  # noqa: E402

rules = importlib.import_module(APP["rules_module"])
score = importlib.import_module(APP["scorer"])
CASES = APP["cases"]
BACKBONES = json.loads((ROOT / "methods" / "backbones.json").read_text())
METHODS = {**agents.METHODS, **agents_reflect_timeline.METHODS}
ABL_METHODS = {
    "compiled_abl": {},
    "compiled_no_after": {"no_after_rule": True},
    "compiled_no_fallback": {"no_snapshot_fallback": True},
    "compiled_no_coverage": {"no_coverage_table": True},
    "compiled_abl_closed_world": {"closed_world": True},
    "compiled_abl_current_state": {"current_state_only": True},
}


class BackboneLLM(LLM):
    """LLM client with per-backbone inference settings:
    - extra_kwargs: merged into the request's chat_template_kwargs on top of {"enable_thinking": thinking}
      (for example reasoning_effort for the Qwen3.5-family templates);
    - merge_roles: consecutive messages of the same role are joined before the call (Gemma 3's template
      requires strict user / assistant alternation; the agent loops send two user messages in a row when
      the query budget is exhausted right after a query result).
    With neither setting the call is delegated to LLM.chat unchanged."""

    def __init__(self, *a, extra_kwargs=None, merge_roles=False, **kw):
        super().__init__(*a, **kw)
        self.extra_kwargs = dict(extra_kwargs or {})
        self.merge_roles = merge_roles

    def body(self, messages, schema=None):
        if self.merge_roles:
            merged = []
            for m in messages:
                if merged and merged[-1]["role"] == m["role"]:
                    merged[-1] = {"role": m["role"], "content": merged[-1]["content"] + "\n\n" + m["content"]}
                else:
                    merged.append(dict(m))
            messages = merged
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "top_p": self.top_p, "max_tokens": self.max_tokens,
                "chat_template_kwargs": {"enable_thinking": self.thinking, **self.extra_kwargs}}
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "action", "schema": schema}}
        return body

    def chat(self, messages, schema=None):
        if not self.extra_kwargs and not self.merge_roles:
            return super().chat(messages, schema)
        body = self.body(messages, schema)
        last = None
        for attempt in range(self.retries + 1):
            try:
                resp = requests.post(f"{self.base_url}/v1/chat/completions", json=body, timeout=self.timeout)
                if resp.status_code != 200:
                    raise InfraError(f"http {resp.status_code}: {resp.text[:200]}")
                data = resp.json()
                break
            except (requests.RequestException, InfraError, ValueError) as exc:
                last = exc
                if attempt < self.retries:
                    self.retried += 1
                    time.sleep(5 * (attempt + 1))
        else:
            raise InfraError(str(last)[:300])
        self.calls += 1
        usage = data.get("usage") or {}
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        choice = data["choices"][0]
        msg = choice["message"]
        return {"content": msg.get("content") or "", "reasoning": msg.get("reasoning_content") or msg.get("reasoning") or "",
                "finish_reason": choice.get("finish_reason")}


def make_llm(args):
    return BackboneLLM(args.base_url, args.model, temperature=args.temperature, max_tokens=args.max_tokens,
                 thinking=args.thinking, extra_kwargs=args.chat_template_kwargs, merge_roles=args.merge_roles)


def run_case(args, case):
    cdir = CASES / "public" / case["case_id"]
    tool = SQLTool(cdir, max_queries=args.max_queries)
    t0 = time.time()
    rec = {"case_id": case["case_id"], "method": args.method, "rep": args.rep}
    llm = None
    try:
        if args.method.startswith("rules"):
            kw = {"rules_closed_world": {"closed_world": True},
                  "rules_current_state": {"current_state_only": True},
                  # weak rules of Wagtail, whose policy has no current-state fallback:
                  # ignore the time limit / ignore separation of duty
                  "rules_no_time_limit": {"no_time_limit": True},
                  "rules_no_separation_of_duty": {"no_actor_check": True}}.get(args.method, {})
            final, trace = rules.investigate(tool, **kw), []
        elif args.method in ABL_METHODS:
            spec = evaluator_ablations.validate_spec(args.spec_obj)  # invalid spec -> method_error on every case
            final, trace = evaluator_ablations.evaluate(tool, spec, **ABL_METHODS[args.method]), []
        elif args.method.startswith("compiled"):
            kw = {"compiled_closed_world": {"closed_world": True},
                  "compiled_current_state": {"current_state_only": True}}.get(args.method, {})
            spec = evaluator.validate_spec(args.spec_obj)  # invalid spec -> method_error on every case
            final, trace = evaluator.evaluate(tool, spec, **kw), []
        elif args.method.startswith("codegen"):
            final, trace = codegen.run_program(args.code, tool, timeout=args.timeout)
        elif args.method in ("pool_judge", "pool_judge_informed"):
            llm = make_llm(args)
            final, trace = pc.pool_judge(llm, tool, informed=args.method.endswith("_informed"))
        else:
            llm = make_llm(args)
            final, trace = METHODS[args.method](llm, tool)
        rec["status"] = "completed" if final is not None else "invalid_output"
    except InfraError as exc:
        final, trace = None, []
        rec["status"] = "infra_failure"
        rec["error"] = str(exc)[:300]
    except Exception as exc:  # method crash is recorded, not dropped
        final, trace = None, []
        rec["status"] = "method_error"
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rec["traceback"] = traceback.format_exc()[-1500:]
        if getattr(exc, "child_traceback", ""):
            rec["child_traceback"] = exc.child_traceback[-1500:]
    rec["wall_s"] = round(time.time() - t0, 2)
    rec["tool_cost"] = tool.cost()
    rec["llm_cost"] = llm.cost() if llm else None
    if llm:
        rec["backbone"] = {"profile": args.backbone, "model": args.model, "thinking": args.thinking,
                           "merge_consecutive_roles": args.merge_roles, "chat_template_kwargs": args.chat_template_kwargs}
    rec["submission"] = final
    truth = json.loads((CASES / "private" / f"{case['case_id']}.json").read_text())
    ids = [r[0] for r in sqlite3.connect(cdir / "episode.sqlite").execute("SELECT id FROM audit_log")]
    obs_ids, obs_tables = set(tool.observed_audit_ids), set(tool.observed_tables)
    for t in trace:
        if isinstance(t, dict) and "pool_audit_ids" in t:
            obs_ids |= set(t["pool_audit_ids"])
            obs_tables |= set(t.get("pool_tables", ["team_members"]))
    rec["score"] = score.score(truth, final, ids, observed_ids=obs_ids, observed_tables=obs_tables)
    traj = {"record": rec, "tool_log": tool.log, "trace": trace}
    (args.out / "trajectories" / f"{case['case_id']}.json").write_text(json.dumps(traj, indent=1, default=str))
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("method")
    ap.add_argument("split")
    ap.add_argument("run_name")
    ap.add_argument("--app", default="pretix")
    ap.add_argument("--backbone", default=None, help="profile name in methods/backbones.json")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--thinking", default=None, choices=["on", "off"])
    ap.add_argument("--chat-template-kwargs", default=None,
                    help="JSON object merged over the profile's chat_template_kwargs (e.g. '{\"reasoning_effort\": \"low\"}')")
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--max-queries", type=int, default=12)
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--spec", default=None)
    ap.add_argument("--code", default=None, help="generated program for the codegen method")
    ap.add_argument("--timeout", type=float, default=codegen.CASE_TIMEOUT_S)
    ap.add_argument("--tag", default="")
    ap.add_argument("--cases", default=None, help="case root to use instead of the profile's (e.g. a coverage-sweep offset)")
    args = ap.parse_args()
    global CASES
    if args.cases:
        CASES = Path(args.cases).resolve()
    profile = BACKBONES[args.backbone] if args.backbone else {}
    if args.backbone and args.backbone not in BACKBONES:
        raise SystemExit(f"unknown backbone {args.backbone!r}; known: {sorted(k for k in BACKBONES if not k.startswith('_'))}")
    args.base_url = args.base_url or profile.get("base_url", "http://127.0.0.1:8100")
    args.model = args.model or profile.get("model", "qwen3-32b")
    args.thinking = (args.thinking == "on") if args.thinking else bool(profile.get("thinking", True))
    args.merge_roles = bool(profile.get("merge_consecutive_roles", False))
    cli_kw = json.loads(args.chat_template_kwargs) if args.chat_template_kwargs else {}
    args.chat_template_kwargs = {**(profile.get("chat_template_kwargs") or {}), **cli_kw}
    args.spec_obj = json.loads(Path(args.spec).read_text()) if args.spec else None
    if args.method.startswith("codegen"):
        if not args.code:
            raise SystemExit("codegen needs --code FILE")
        args.code = Path(args.code).resolve()
    args.out = ROOT / "runs" / args.run_name / f"{args.method}{args.tag}_rep{args.rep}"
    (args.out / "trajectories").mkdir(parents=True, exist_ok=True)
    index = json.loads((CASES / "private" / "index.json").read_text())
    cases = [e for e in index if e["split"] == args.split and e["world_ok"]]
    if args.limit:
        cases = cases[: args.limit]
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        recs = list(ex.map(lambda c: run_case(args, c), cases))
    with open(args.out / "results.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r, default=str) + "\n")
    from collections import Counter
    print(json.dumps({"method": args.method, "app": app_profile.name(), "n": len(recs), "backbone": args.backbone,
                      "model": args.model, "thinking": args.thinking, "chat_template_kwargs": args.chat_template_kwargs,
                      "status": dict(Counter(r["status"] for r in recs))}))


if __name__ == "__main__":
    main()
