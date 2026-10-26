"""Generate specs, execute all Indico worlds in parallel, export cases.

Split is by base event (all paired worlds and visibility variants of a base event share it):
core patterns: instances 0-3 -> dev, 4-9 -> test; holdout patterns: every instance -> test
(pattern-level holdout). The test split is sealed at creation.

Worlds per base event: AUTH, UNAUTH, the UNAUTH twin (every pattern) and the AUTH twin (category
patterns); twins are used only for G0.5.

Usage: build_cases.py [--only-run | --only-export] [--limit N] [--workers W]
Worlds (ledger + state dump) go to the data directory (indico_env.WORLDS_ROOT); cases to cases/indico/.
"""
import argparse
import json
import queue
import random
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_case  # noqa: E402
import indico_env  # noqa: E402
import worlds  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "cases" / "indico"
WORLDS = indico_env.WORLDS_ROOT
PY = indico_env.PY
N_INSTANCES = 10
DEV_INSTANCES = range(0, 4)


def split_of(pattern, i):
    return "dev" if (pattern in worlds.PATTERNS_CORE and i in DEV_INSTANCES) else "test"


def all_specs():
    out = []
    for pattern in worlds.PATTERNS:
        for i in range(N_INSTANCES):
            base = worlds.base_event(pattern, i, split_of(pattern, i))
            out.append(worlds.world_spec(base, "AUTH"))
            out.append(worlds.world_spec(base, "UNAUTH"))
            out.append(worlds.world_spec(base, "UNAUTH", twin=True))
            if pattern in worlds.CATEGORY_PATTERNS:
                out.append(worlds.world_spec(base, "AUTH", twin=True))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-run", action="store_true")
    ap.add_argument("--only-export", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    (CASES / "specs").mkdir(parents=True, exist_ok=True)
    WORLDS.mkdir(parents=True, exist_ok=True)
    specs = all_specs()
    if args.limit:
        specs = specs[: args.limit]
    jobs = []
    for spec in specs:
        sp = CASES / "specs" / f"{spec['world_id']}.json"
        sp.write_text(json.dumps(spec, indent=1))
        jobs.append((sp, WORLDS / spec["world_id"]))
    if not args.only_export:
        slots = queue.Queue()
        for i in range(args.workers):
            slots.put(2 + i)  # Redis databases 2.. (0/1: template and probe)

        def run_one(job):
            spec_path, out_dir = job
            slot = slots.get()
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                with open(out_dir / "stderr.log", "w") as err:
                    p = subprocess.run([str(PY), str(Path(__file__).parent / "run_world.py"), str(spec_path), str(out_dir),
                                        str(slot)], stdout=subprocess.PIPE, stderr=err, text=True)
                return {"world": out_dir.name, "returncode": p.returncode, "stdout": p.stdout.strip()[-300:]}
            finally:
                slots.put(slot)

        indico_env.start_cluster()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            results = list(ex.map(run_one, jobs))
        (CASES / "world_run_results.json").write_text(json.dumps(results, indent=1))
        failed = [r for r in results if r["returncode"] != 0 or '"ok": true' not in r["stdout"]]
        print(f"worlds executed: {len(results)}; failed or not-ok: {len(failed)}")
        for r in failed[:10]:
            print("  FAILED", r["world"], r["stdout"][-200:])
    if args.only_run:
        return
    rng = random.Random("ceta-indico-case-ids")
    index = []
    (CASES / "private").mkdir(exist_ok=True)
    for sp, wdir in jobs:
        spec = json.loads(sp.read_text())
        if spec["twin"]:
            continue
        status = json.loads((wdir / "status.json").read_text()) if (wdir / "status.json").exists() else {"ok": False}
        for variant in export_case.VARIANTS:
            case_id = "c" + format(rng.getrandbits(40), "010x")
            entry = {"case_id": case_id, "world_id": spec["world_id"], "variant": variant,
                     "base_id": spec["base"]["base_id"], "split": spec["base"]["split"], "world_ok": status["ok"]}
            if status["ok"]:
                ledger, world, seed, seed_row, cov = export_case.build_public(
                    wdir, variant, case_id, CASES / "public" / case_id)
                truth = export_case.build_truth(ledger, world, seed, seed_row, cov, variant)
                truth["case_id"] = case_id
                (CASES / "private" / f"{case_id}.json").write_text(json.dumps(truth, indent=1))
            index.append(entry)
    (CASES / "private" / "index.json").write_text(json.dumps(index, indent=1))
    truths = [json.loads((CASES / "private" / f"{e['case_id']}.json").read_text()) for e in index if e["world_ok"]]
    print(f"cases indexed: {len(index)}; exported: {sum(e['world_ok'] for e in index)}")
    print("expected verdicts:", dict(Counter((t["split"], t["expected_verdict"]) for t in truths)))
    print("latent truth:", dict(Counter((t["split"], t["latent_truth"]) for t in truths)))
    print("answerable by pattern/variant:",
          dict(Counter((t["pattern"], t["variant"], t["answerable"]) for t in truths)))


if __name__ == "__main__":
    main()
