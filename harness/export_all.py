"""Export public cases and private truth from already-executed worlds (no re-execution)."""
import json
import random
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_cases  # noqa: E402
import export_case  # noqa: E402
import worlds  # noqa: E402

CASES = build_cases.CASES


def main():
    for d in ("public", "private"):
        shutil.rmtree(CASES / d, ignore_errors=True)
    (CASES / "private").mkdir(parents=True)
    rng = random.Random("ceta-pretix-case-ids")
    index = []
    for pattern in worlds.PATTERNS:
        for i in range(build_cases.N_INSTANCES):
            split = "dev" if i in build_cases.DEV_INSTANCES else "test"
            base = worlds.base_event(pattern, i, split)
            for kind in ("AUTH", "UNAUTH"):
                wid = f"{base['base_id']}__{kind}"
                wdir = CASES / "worlds" / wid
                ok = json.loads((wdir / "status.json").read_text())["ok"]
                for variant in export_case.VARIANTS:
                    cid = "c" + format(rng.getrandbits(40), "010x")
                    if ok:
                        led, w, seed, srow, cov = export_case.build_public(wdir, variant, cid, CASES / "public" / cid)
                        t = export_case.build_truth(led, w, seed, srow, cov, variant)
                        t["case_id"] = cid
                        (CASES / "private" / f"{cid}.json").write_text(json.dumps(t, indent=1))
                    index.append({"case_id": cid, "world_id": wid, "variant": variant,
                                  "base_id": base["base_id"], "split": split, "world_ok": ok})
    (CASES / "private" / "index.json").write_text(json.dumps(index, indent=1))
    truths = [json.loads((CASES / "private" / f"{e['case_id']}.json").read_text()) for e in index if e["world_ok"]]
    print("cases:", len(index), dict(Counter(e["split"] for e in index)))
    print("expected verdicts:", dict(Counter((t["split"], t["expected_verdict"]) for t in truths)))
    print("latent truth:", dict(Counter((t["split"], t["latent_truth"]) for t in truths)))


if __name__ == "__main__":
    main()
