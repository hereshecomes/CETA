"""Compile-and-select on modified public documents (document ablations E11 / E12 / E14): K = 5 independent
draws with the unchanged compiler (methods/compiler.compile_spec: same prompt function, schema, repair loop
with 2 rounds, T = 0.6) given a documents directory prepared by docs_ablation_prepare.py, through a profile
of methods/backbones.json; development evaluation of every valid draw through methods/run_method.py
`compiled` (the evaluator does not read the documents); selection by the fixed rule (highest development
c&g, ties -> lowest index). Record format as in compile_and_select.py. Resumable draw by draw.

Usage: compile_docs_ablation_run.py RUN_NAME --app pretix|weblate --docs DIR --variant E11|E12|E14
       [--backbone qwen3-32b] [--k 5]
"""
import argparse
import os
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
import app_profile_ext  # noqa: E402,F401
import compiler  # noqa: E402
import evaluator as ev  # noqa: E402
from run_method import BackboneLLM, BACKBONES  # noqa: E402


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rel(p):
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)


def sh(*cmd):
    p = subprocess.run([str(c) for c in cmd], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode != 0:
        print(p.stdout[-3000:])
        raise SystemExit(f"failed: {' '.join(str(c) for c in cmd)}")
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_name")
    ap.add_argument("--app", required=True)
    ap.add_argument("--docs", required=True)
    ap.add_argument("--variant", required=True)
    ap.add_argument("--backbone", default="qwen3-32b")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    app_profile.select(args.app)
    prof = BACKBONES[args.backbone]
    docs = Path(os.path.abspath(args.docs))
    run = ROOT / "runs" / args.run_name
    specs = run / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    prompt = compiler.compile_prompt(docs)
    (specs / "compile_prompt.txt").write_text(prompt)
    orig = compiler.compile_prompt(app_profile.current()["docs"])
    ident = {"docs": rel(docs), "prompt_differs_from_original": prompt != orig,
             "compiler": "methods/compiler.compile_spec, max_repairs 2, T 0.6, schema SPEC_SCHEMA"}
    (run / "identity.json").write_text(json.dumps(ident, indent=1))
    rec_bb = {"profile": args.backbone, "label": prof.get("label"), "model": prof["model"], "base_url": prof["base_url"],
              "thinking": bool(prof["thinking"]), "chat_template_kwargs": prof.get("chat_template_kwargs") or {}}
    out = {"run": args.run_name, "app": args.app, "variant": args.variant, "k": args.k, "docs": ident["docs"],
           "backbone": rec_bb, "rep": 0, "identity": ident, "started_at": now()}

    def draw(i):
        if (specs / f"compile_{i}.json").exists():
            print(json.dumps({"index": i, "resumed": True}), flush=True)
            return
        llm = BackboneLLM(prof["base_url"], prof["model"], temperature=0.6, thinking=bool(prof["thinking"]),
                    extra_kwargs=prof.get("chat_template_kwargs") or {}, merge_roles=bool(prof.get("merge_consecutive_roles")))
        rec = {"index": i, "variant": args.variant, "docs": ident["docs"], "max_repairs": compiler.MAX_REPAIRS,
               "backbone": rec_bb, "started_at": now()}
        t0 = time.time()
        try:
            spec, raw, attempts, valid = compiler.compile_spec(llm, docs)
            rec.update(ok=True, valid=valid, spec=spec, attempts=attempts, repairs_used=len(attempts) - 1, cost=llm.cost(),
                       finish_reason=raw["finish_reason"], reasoning_chars=len(raw["reasoning"]), raw_content_head=raw["content"][:2000])
            if not valid:
                rec["validation_error"] = attempts[-1].get("error")
            if spec is not None:
                (specs / f"spec_{i}.json").write_text(json.dumps(spec, indent=1))
        except Exception as exc:
            rec.update(ok=False, valid=False, error=f"{type(exc).__name__}: {exc}"[:300], cost=llm.cost())
        rec.update(finished_at=now(), wall_s=round(time.time() - t0, 1))
        (specs / f"compile_{i}.json").write_text(json.dumps(rec, indent=1))
        print(json.dumps({"index": i, "ok": rec["ok"], "valid": rec.get("valid"), "repairs_used": rec.get("repairs_used"),
                          "finish_reason": rec.get("finish_reason"), "cost": rec.get("cost"), "wall_s": rec["wall_s"]}), flush=True)

    with ThreadPoolExecutor(max_workers=args.k) as ex:
        list(ex.map(draw, range(args.k)))
    out["compile"], valid = {}, []
    for i in range(args.k):
        rec = json.loads((specs / f"compile_{i}.json").read_text())
        c = {k: rec.get(k) for k in ("ok", "valid", "validation_error", "error", "cost", "finish_reason", "repairs_used",
                                     "attempts", "reasoning_chars", "wall_s")}
        c["single_shot_valid"] = bool(rec.get("valid")) and not rec.get("repairs_used")
        if rec.get("valid"):
            valid.append(i)
            spec = ev.validate_spec(rec["spec"])
            c["shape"] = {"principal_kind": spec["principal"]["kind"], "n_scopes": len(spec["scopes"]),
                          "paths": [[x["kind"] for x in p["conditions"]] for p in spec["paths"]], "coverage": bool(spec["coverage"])}
        out["compile"][f"spec_{i}"] = c
    out["valid_draws"] = valid
    out["n_single_shot_valid"] = sum(1 for v in out["compile"].values() if v["single_shot_valid"])
    out["repairs_used"] = {k: v.get("repairs_used") for k, v in out["compile"].items()}
    out["dev"] = {}
    for i in valid:
        res = run / f"compiled_spec{i}_rep0" / "results.jsonl"
        if not res.exists():
            sh(PY, ROOT / "methods" / "run_method.py", "compiled", "dev", args.run_name, "--app", args.app,
               "--backbone", args.backbone, "--spec", specs / f"spec_{i}.json", "--tag", f"_spec{i}", "--concurrency", str(args.concurrency))
        _, recs = analyze.load(res)
        out["dev"][f"spec_{i}"] = analyze.summary(recs)
    ranked = sorted(valid, key=lambda i: (-out["dev"][f"spec_{i}"]["primary_balanced_success"], i))
    out["selected"] = ranked[0] if ranked else None
    out["selection_rule"] = "highest dev primary_balanced_success, ties -> lowest index"
    out["finished_at"] = now()
    print({i: out["dev"][f"spec_{i}"]["primary_balanced_success"] for i in valid}, "selected:", out["selected"])
    (run / "summary.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
