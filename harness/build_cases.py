"""Generate specs, execute all worlds through pretix in parallel, export cases.

Split is by base event (all paired worlds and visibility variants of a base event share it):
instances 0-3 of each pattern -> dev, 4-9 -> test (sealed when generated).
"""
import json
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_case  # noqa: E402
import worlds  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "cases"
PY = ROOT / "app_env" / "venv" / "bin" / "python"
N_INSTANCES = 10
DEV_INSTANCES = range(0, 4)


def run_one(spec_path, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "stderr.log", "w") as err:
        p = subprocess.run([str(PY), str(ROOT / "harness" / "run_world.py"), str(spec_path), str(out_dir)],
                           stdout=subprocess.PIPE, stderr=err, text=True)
    return {"world": out_dir.name, "returncode": p.returncode, "stdout": p.stdout.strip()[-300:]}


def main():
    (CASES / "specs").mkdir(parents=True, exist_ok=True)
    jobs = []
    for pattern in worlds.PATTERNS:
        for i in range(N_INSTANCES):
            split = "dev" if i in DEV_INSTANCES else "test"
            base = worlds.base_event(pattern, i, split)
            for kind in ("AUTH", "UNAUTH"):
                for twin in (False, True):
                    spec = worlds.world_spec(base, kind, twin=twin)
                    sp = CASES / "specs" / f"{spec['world_id']}.json"
                    sp.write_text(json.dumps(spec, indent=1))
                    jobs.append((sp, CASES / "worlds" / spec["world_id"]))
    with ThreadPoolExecutor(max_workers=32) as ex:
        results = list(ex.map(lambda j: run_one(*j), jobs))
    (CASES / "world_run_results.json").write_text(json.dumps(results, indent=1))
    failed = [r for r in results if r["returncode"] != 0 or '"ok": true' not in r["stdout"]]
    print(f"worlds executed: {len(results)}; failed or not-ok: {len(failed)}")

    rng = random.Random("ceta-pretix-case-ids")
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
                     "base_id": spec["base"]["base_id"], "split": spec["base"]["split"],
                     "world_ok": status["ok"]}
            if status["ok"]:
                ledger, world, seed, seed_row, cov = export_case.build_public(
                    wdir, variant, case_id, CASES / "public" / case_id)
                truth = export_case.build_truth(ledger, world, seed, seed_row, cov, variant)
                truth["case_id"] = case_id
                (CASES / "private" / f"{case_id}.json").write_text(json.dumps(truth, indent=1))
            index.append(entry)
    (CASES / "private" / "index.json").write_text(json.dumps(index, indent=1))
    print(f"cases indexed: {len(index)}; exported: {sum(e['world_ok'] for e in index)}")


if __name__ == "__main__":
    main()
