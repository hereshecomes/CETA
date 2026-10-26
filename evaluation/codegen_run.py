"""Driver of the program-synthesis baseline P: K independent programs written by the LLM from the
application's two public documents, development evaluation of every importable program, selection by the
fixed rule (highest development c&g, ties -> lowest index), test evaluation of every importable program,
and a check whether the selected program exposes ablation switches.

Usage: codegen_run.py RUN_NAME --app pretix|weblate|wagtail|indico [--k 5] [--skip-generate]
"""
import argparse
import ast
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(ROOT / "evaluation"))
sys.path.insert(0, str(ROOT / "methods"))
import analyze  # noqa: E402
import app_profile  # noqa: E402
import app_profile_ext  # noqa: E402,F401  (registers wagtail and indico)

ABLATION_FLAGS = ("closed_world", "current_state_only")


def sh(*cmd, check=True):
    p = subprocess.run([str(c) for c in cmd], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if check and p.returncode != 0:
        print(p.stdout[-3000:])
        raise SystemExit(f"failed: {' '.join(str(c) for c in cmd)}")
    return p.stdout


def summary_of(results):
    _, recs = analyze.load(results)
    return analyze.summary(recs)


def decide_params(program):
    """Parameter names of decide() in the program text (ast only; the program is never imported here)."""
    try:
        tree = ast.parse(Path(program).read_text())
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "decide":
            a = node.args
            return [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_name")
    ap.add_argument("--app", required=True, choices=sorted(app_profile.PROFILES))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--skip-generate", action="store_true")
    args = ap.parse_args()
    profile = app_profile.select(args.app)
    run = ROOT / "runs" / args.run_name
    run.mkdir(parents=True, exist_ok=True)
    out = {"run": args.run_name, "app": args.app, "k": args.k, "docs": str(profile["docs"])}
    runner = ROOT / "methods" / "run_method.py"


    draws = run / "draws"
    if not args.skip_generate:
        print("== 1. generation (one LLM call per draw, repair only on import failure)")
        print(sh(PY, ROOT / "methods" / "codegen.py", args.run_name, args.k, "--docs", profile["docs"]))
    out["generation"], valid = {}, []
    for i in range(args.k):
        rec = json.loads((draws / f"gen_{i}.json").read_text())
        out["generation"][f"draw_{i}"] = {k: rec.get(k) for k in ("ok", "valid", "validation_error", "error", "cost",
                                                                "repairs_used", "attempts", "program")}
        if rec.get("valid"):
            valid.append(i)
            out["generation"][f"draw_{i}"]["decide_params"] = decide_params(draws / f"draw_{i}.py")
    out["valid_draws"] = valid
    print(json.dumps({k: {kk: v.get(kk) for kk in ("valid", "repairs_used", "cost", "validation_error")}
                      for k, v in out["generation"].items()}, indent=1))

    print("== 2. dev evaluation + selection")
    out["dev"] = {}
    for i in valid:
        sh(PY, runner, "codegen", "dev", f"{args.run_name}/dev", "--app", args.app,
           "--code", draws / f"draw_{i}.py", "--tag", f"_draw{i}", "--concurrency", "8")
        out["dev"][f"draw_{i}"] = summary_of(run / "dev" / f"codegen_draw{i}_rep0" / "results.jsonl")
    ranked = sorted(valid, key=lambda i: (-out["dev"][f"draw_{i}"]["primary_balanced_success"], i))
    out["selected"] = ranked[0] if ranked else None
    out["selection_rule"] = "highest dev primary_balanced_success, ties -> lowest index"
    print({i: out["dev"][f"draw_{i}"]["primary_balanced_success"] for i in valid}, "selected:", out["selected"])
    (run / "summary.json").write_text(json.dumps(out, indent=1))

    print("== 3. test evaluation of every importable draw")
    out["test"] = {}
    for i in range(args.k):
        if i not in valid:
            out["test"][f"draw_{i}"] = "not run (program failed to import)"
            continue
        sh(PY, runner, "codegen", "test", f"{args.run_name}/test", "--app", args.app,
           "--code", draws / f"draw_{i}.py", "--tag", f"_draw{i}", "--concurrency", "8")
        out["test"][f"draw_{i}"] = summary_of(run / "test" / f"codegen_draw{i}_rep0" / "results.jsonl")
    if out["selected"] is not None:
        link = run / "test" / "codegen_selected_rep0"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(f"codegen_draw{out['selected']}_rep0", link)
        out["test"]["codegen_selected"] = f"draw_{out['selected']}"

    print("== 4. ablation conditions of the selected program")
    if out["selected"] is None:
        out["ablations"] = "not run (no importable program)"
    else:
        params = decide_params(draws / f"draw_{out['selected']}.py") or []
        switches = [p for p in params if p in ABLATION_FLAGS]
        out["ablations"] = ({"possible": True, "switches": switches, "note": "decide() exposes ablation switches; run them by hand"}
                            if switches else
                            {"possible": False, "decide_params": params,
                             "note": "skipped: the generated decide() has no closed-world / current-state switch, and the "
                                     "generated code is never edited"})
    print(json.dumps(out["ablations"]))
    (run / "summary.json").write_text(json.dumps(out, indent=1))
    for i in range(args.k):
        t = out["test"].get(f"draw_{i}")
        d = out["dev"].get(f"draw_{i}", {})
        print(f"draw_{i}: valid={i in valid} dev={d.get('primary_balanced_success')} "
              f"test={t['primary_balanced_success'] if isinstance(t, dict) else t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
