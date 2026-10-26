"""Compile-and-select step of CETA for one application and one model: K independent draws with the
compiler (methods/compiler.py, public documents only) through a profile of methods/backbones.json,
development evaluation of every valid draw through methods/run_method.py, and selection by the fixed
rule (highest development c&g, ties -> lowest index). The test run of the selected specification is a
separate call of methods/run_method.py.

Writes runs/RUN_NAME/specs/{compile_<i>.json, spec_<i>.json, compile_prompt.txt} and
runs/RUN_NAME/summary.json. Resumable: a draw whose compile_<i>.json exists is not redrawn; a development
evaluation whose results exist is not rerun.

Usage: compile_and_select.py RUN_NAME --app pretix|weblate|wagtail|indico --backbone NAME [--k 5]
       [--concurrency 8] [--max-repairs 2] [--parallel 1]
"""
import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "methods"))
import analyze  # noqa: E402
import app_profile  # noqa: E402
import app_profile_ext  # noqa: E402,F401  (registers wagtail and indico)
import compiler  # noqa: E402
import evaluator as ev  # noqa: E402
from run_method import BackboneLLM, BACKBONES  # noqa: E402


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sh(*cmd):
    p = subprocess.run([str(c) for c in cmd], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        print(p.stdout[-3000:])
        raise SystemExit(f"failed: {' '.join(str(c) for c in cmd)}")
    return p.stdout


def summary_of(results):
    _, recs = analyze.load(results)
    return analyze.summary(recs)


def make_llm(prof):
    return BackboneLLM(prof["base_url"], prof["model"], temperature=0.6, thinking=bool(prof["thinking"]),
                 extra_kwargs=prof.get("chat_template_kwargs") or {}, merge_roles=bool(prof.get("merge_consecutive_roles")))


def backbone_record(name, prof):
    return {"profile": name, "label": prof.get("label"), "model": prof["model"], "base_url": prof["base_url"],
            "thinking": bool(prof["thinking"]), "chat_template_kwargs": prof.get("chat_template_kwargs") or {},
            "merge_consecutive_roles": bool(prof.get("merge_consecutive_roles"))}


def compile_k(name, prof, docs, out, k, max_repairs, parallel=1):
    """Same loop and record as compiler.main, with the profile's BackboneLLM instead of the default LLM. The K draws are
    independent (each its own client and conversation), so with parallel > 1 they are drawn at the same time."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "compile_prompt.txt").write_text(compiler.compile_prompt(docs))
    todo = []
    for i in range(k):
        if (out / f"compile_{i}.json").exists():
            print(json.dumps({"index": i, "resumed": True}))
        else:
            todo.append(i)
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        list(ex.map(lambda i: draw_one(name, prof, docs, out, i, max_repairs), todo))


def draw_one(name, prof, docs, out, i, max_repairs):
    if True:  # one fresh client per draw
        llm = make_llm(prof)
        rec = {"index": i, "docs": str(docs), "max_repairs": max_repairs, "backbone": backbone_record(name, prof),
               "started_at": now()}
        t0 = time.time()
        try:
            spec, raw, attempts, valid = compiler.compile_spec(llm, docs, max_repairs=max_repairs)
            rec.update(ok=True, valid=valid, spec=spec, attempts=attempts, repairs_used=len(attempts) - 1,
                       cost=llm.cost(), finish_reason=raw["finish_reason"], reasoning_chars=len(raw["reasoning"]),
                       raw_content_head=raw["content"][:2000])
            if not valid:
                rec["validation_error"] = attempts[-1].get("error")
            if spec is not None:
                (out / f"spec_{i}.json").write_text(json.dumps(spec, indent=1))
        except Exception as exc:
            rec.update(ok=False, valid=False, error=f"{type(exc).__name__}: {exc}"[:300], cost=llm.cost())
        rec.update(finished_at=now(), wall_s=round(time.time() - t0, 1))
        (out / f"compile_{i}.json").write_text(json.dumps(rec, indent=1))
        print(json.dumps({"index": i, "ok": rec["ok"], "valid": rec.get("valid"), "repairs_used": rec.get("repairs_used"),
                          "finish_reason": rec.get("finish_reason"), "cost": rec.get("cost"), "wall_s": rec["wall_s"]}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_name")
    ap.add_argument("--app", required=True)
    ap.add_argument("--backbone", required=True)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--max-repairs", type=int, default=compiler.MAX_REPAIRS)
    ap.add_argument("--parallel", type=int, default=1, help="draws in parallel; dev evaluations of the valid draws in parallel")
    args = ap.parse_args()
    profile = app_profile.select(args.app)
    prof = BACKBONES[args.backbone]
    run = ROOT / "runs" / args.run_name
    run.mkdir(parents=True, exist_ok=True)
    out = {"run": args.run_name, "app": args.app, "k": args.k, "docs": str(profile["docs"]),
           "backbone": backbone_record(args.backbone, prof), "rep": 0, "started_at": now()}

    print("== 1. compile (documents only, backbone", args.backbone, ")")
    specs = run / "specs"
    compile_k(args.backbone, prof, profile["docs"], specs, args.k, args.max_repairs, args.parallel)
    out["compile"], valid = {}, []
    for i in range(args.k):
        rec = json.loads((specs / f"compile_{i}.json").read_text())
        out["compile"][f"spec_{i}"] = {k: rec.get(k) for k in ("ok", "valid", "validation_error", "error", "cost",
                                                              "finish_reason", "repairs_used", "attempts", "reasoning_chars", "wall_s")}
        out["compile"][f"spec_{i}"]["single_shot_valid"] = bool(rec.get("valid")) and not rec.get("repairs_used")
        if rec.get("valid"):
            valid.append(i)
            spec = ev.validate_spec(rec["spec"])
            out["compile"][f"spec_{i}"]["shape"] = {
                "principal_kind": spec["principal"]["kind"], "n_scopes": len(spec["scopes"]),
                "paths": [[c["kind"] for c in p["conditions"]] for p in spec["paths"]],
                "coverage": bool(spec["coverage"])}
    out["valid_draws"] = valid
    print(json.dumps({k: {kk: v.get(kk) for kk in ("ok", "valid", "repairs_used", "finish_reason", "validation_error", "error")}
                      for k, v in out["compile"].items()}, indent=1))

    print("== 2. dev evaluation + selection")
    out["dev"] = {}

    def dev_eval(i):
        res = run / f"compiled_spec{i}_rep0" / "results.jsonl"
        if not res.exists():
            sh(PY, ROOT / "methods" / "run_method.py", "compiled", "dev", args.run_name, "--app", args.app,
               "--backbone", args.backbone, "--spec", specs / f"spec_{i}.json", "--tag", f"_spec{i}",
               "--concurrency", str(args.concurrency))
        return i, summary_of(res)
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as ex:
        for i, sm in ex.map(dev_eval, valid):
            out["dev"][f"spec_{i}"] = sm
    ranked = sorted(valid, key=lambda i: (-out["dev"][f"spec_{i}"]["primary_balanced_success"], i))
    out["selected"] = ranked[0] if ranked else None
    out["selection_rule"] = "highest dev primary_balanced_success, ties -> lowest index"
    out["finished_at"] = now()
    print({i: out["dev"][f"spec_{i}"]["primary_balanced_success"] for i in valid}, "selected:", out["selected"])
    (run / "summary.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
