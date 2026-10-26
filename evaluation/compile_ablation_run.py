"""Compiler ablation E6-E10 for one application and one variant: K draws with
methods/compiler_ablations.py through a profile of methods/backbones.json, development evaluation of every
valid draw through methods/run_method.py `compiled`, and selection by the fixed rule (highest development
c&g, ties -> lowest index). The test run of the selected draw is a separate call of methods/run_method.py.
Record format as in compile_and_select.py (specs/compile_<i>.json, spec_<i>.json, compile_prompt.txt, plus
schema.json and identity.json). Resumable: a draw whose compile_<i>.json exists is not redrawn; a
development evaluation whose results exist is not rerun.

Usage: compile_ablation_run.py RUN_NAME --app pretix|weblate|wagtail|indico --variant E6|E7|E8|E9|E10
       [--backbone qwen3-32b] [--k N  (override, smoke runs only)] [--concurrency 8]
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
import app_profile_ext  # noqa: E402,F401
import compiler_ablations as abl  # noqa: E402
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


def backbone_record(name, prof):
    return {"profile": name, "label": prof.get("label"), "model": prof["model"], "base_url": prof["base_url"],
            "thinking": bool(prof["thinking"]), "chat_template_kwargs": prof.get("chat_template_kwargs") or {},
            "merge_consecutive_roles": bool(prof.get("merge_consecutive_roles"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_name")
    ap.add_argument("--app", required=True)
    ap.add_argument("--variant", required=True, choices=sorted(abl.VARIANTS))
    ap.add_argument("--backbone", default="qwen3-32b")
    ap.add_argument("--k", type=int, default=None, help="override of the variant's K (smoke runs only)")
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    V = abl.VARIANTS[args.variant]
    k = args.k or V["k"]
    profile = app_profile.select(args.app)
    prof = BACKBONES[args.backbone]
    docs = profile["docs"]
    run = ROOT / "runs" / args.run_name
    specs = run / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    prompt = abl.compile_prompt(docs, V["example"])
    schema = abl.schema_for(args.variant)
    (specs / "compile_prompt.txt").write_text(prompt)
    (specs / "schema.json").write_text(json.dumps(schema, indent=1))
    ident = abl.identity_checks(docs)
    ident.update(variant=args.variant, config=V, k_used=k,
                 prompt_is_compiler_prompt=prompt == abl.cv.compile_prompt(docs),
                 schema_is_compiler_schema=schema is abl.cv.SPEC_SCHEMA)
    (run / "identity.json").write_text(json.dumps(ident, indent=1))
    out = {"run": args.run_name, "app": args.app, "variant": args.variant, "variant_config": V, "k": k,
           "predicted": abl.PREDICTED[args.variant], "docs": str(docs), "backbone": backbone_record(args.backbone, prof),
           "rep": 0, "identity": ident, "started_at": now()}

    print(f"== 1. compile ablation {args.variant} ({V['name']}): K={k}, schema {V['schema']}, max_repairs {V['max_repairs']}, "
          f"example {V['example']}, T={V['temperature']}")
    def draw(i):
        if (specs / f"compile_{i}.json").exists():
            print(json.dumps({"index": i, "resumed": True}))
            return
        llm = BackboneLLM(prof["base_url"], prof["model"], temperature=V["temperature"], thinking=bool(prof["thinking"]),
                    extra_kwargs=prof.get("chat_template_kwargs") or {}, merge_roles=bool(prof.get("merge_consecutive_roles")))
        rec = {"index": i, "variant": args.variant, "docs": str(docs), "max_repairs": V["max_repairs"],
               "temperature": V["temperature"], "schema": V["schema"], "example": V["example"],
               "backbone": backbone_record(args.backbone, prof), "started_at": now()}
        t0 = time.time()
        try:
            spec, raw, attempts, valid = abl.compile_spec(llm, prompt, schema, V["max_repairs"])
            rec.update(ok=True, valid=valid, spec=spec, attempts=attempts, repairs_used=len(attempts) - 1,
                       cost=llm.cost(), finish_reason=raw["finish_reason"], reasoning_chars=len(raw["reasoning"]),
                       raw_content_head=raw["content"][:2000])
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

    # the K draws are independent (one fresh client each), so they are drawn in parallel
    with ThreadPoolExecutor(max_workers=k) as ex:
        list(ex.map(draw, range(k)))

    out["compile"], valid = {}, []
    for i in range(k):
        rec = json.loads((specs / f"compile_{i}.json").read_text())
        out["compile"][f"spec_{i}"] = {kk: rec.get(kk) for kk in ("ok", "valid", "validation_error", "error", "cost",
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
    out["n_single_shot_valid"] = sum(1 for v in out["compile"].values() if v["single_shot_valid"])
    out["repairs_used"] = {kk: v.get("repairs_used") for kk, v in out["compile"].items()}

    print("== 2. dev evaluation + selection")
    out["dev"] = {}
    for i in valid:
        res = run / f"compiled_spec{i}_rep0" / "results.jsonl"
        if not res.exists():
            sh(PY, ROOT / "methods" / "run_method.py", "compiled", "dev", args.run_name, "--app", args.app,
               "--backbone", args.backbone, "--spec", specs / f"spec_{i}.json", "--tag", f"_spec{i}",
               "--concurrency", str(args.concurrency))
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
