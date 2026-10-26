"""Coverage sweep (RQ5): re-export the non-twin test worlds of pretix and Weblate with the start of log
coverage moved to fixed offsets from T0, without executing, compiling or selecting again.

For every offset and every test world (its V0 case), the world's executed ledger is exported again with
coverage starting at T0 + offset minutes. Every new undecidable view must have a twin world of opposite
truth whose exported view (tables and alert) is identical; otherwise the sweep stops. At offset -30 the
exported view and truth must equal the original V0 case. Each offset is written as a self-contained case
root, runs/coverage_sweep/<app>/offset_<o>/{public,private,private/index.json}, on which every method
runs unchanged:

    python methods/run_method.py compiled test sweep_pretix_-30 --app pretix --spec SPEC \\
        --cases runs/coverage_sweep/pretix/offset_-30

Usage: coverage_sweep.py --app pretix|weblate [--offsets -30 60 80 100 150]
"""
import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("CETA_DATA", str(ROOT / "data")))
OFFSETS = [-30, 60, 80, 100, 150]
APPS = {"pretix": dict(base=ROOT / "cases", worlds=ROOT / "cases" / "worlds", exporter=ROOT / "harness" / "export_case.py"),
        "weblate": dict(base=ROOT / "cases" / "weblate", worlds=DATA / "worlds" / "weblate",
                        exporter=ROOT / "harness" / "weblate" / "export_case.py")}


def read(p):
    return json.loads(Path(p).read_text())


def dump(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    out = {n: sorted(con.execute(f'SELECT * FROM "{n}"').fetchall(), key=repr) for n in names}
    con.close()
    return out


def exporter(app):
    s = importlib.util.spec_from_file_location("sweep_export_" + app, APPS[app]["exporter"])
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, choices=sorted(APPS))
    ap.add_argument("--offsets", type=int, nargs="+", default=OFFSETS)
    args = ap.parse_args()
    cfg = APPS[args.app]
    index = [e for e in read(cfg["base"] / "private" / "index.json") if e["split"] == "test" and e["variant"] == "V0"]
    exp = exporter(args.app)
    out_root = ROOT / "runs" / "coverage_sweep" / args.app
    report = {"app": args.app, "n_worlds": len(index), "offsets": {}}
    for offset in args.offsets:
        exp.coverage_start = lambda ledger, variant, o=offset: datetime.fromisoformat(ledger["T0"]) + timedelta(minutes=o)
        dest = out_root / f"offset_{offset}"
        if dest.exists():
            shutil.rmtree(dest)
        (dest / "private").mkdir(parents=True)
        entries, n_und, n_eq = [], 0, 0
        for e in index:
            cid, variant = e["case_id"], f"offset_{offset}"
            view = dest / "public" / cid
            led, w, seed, srow, cov = exp.build_public(cfg["worlds"] / e["world_id"], variant, cid, view)
            if cov > datetime.fromisoformat(srow["ts"]):
                raise SystemExit(f"coverage excludes the operation: {cid} at offset {offset}")
            truth = exp.build_truth(led, w, seed, srow, cov, variant)
            truth["case_id"] = cid
            if offset == -30:  # the earliest offset reproduces the original full-coverage view
                orig = cfg["base"] / "public" / cid
                old = read(cfg["base"] / "private" / f"{cid}.json")
                if (dump(view / "episode.sqlite") != dump(orig / "episode.sqlite")
                        or read(view / "alert.json") != read(orig / "alert.json")
                        or (truth["latent_truth"], truth["answerable"]) != (old["latent_truth"], old["answerable"])):
                    raise SystemExit(f"offset -30 does not reproduce the original view: {cid}")
                n_eq += 1
            if not truth["answerable"]:  # certify with the twin world of opposite truth
                twin_dir = cfg["worlds"] / (e["world_id"] + "__twin")
                if not read(twin_dir / "status.json")["ok"]:
                    raise SystemExit(f"twin not executed: {e['world_id']}")
                tmp = out_root / "_twin_tmp" / cid
                tl, tw, ts, tr, tc = exp.build_public(twin_dir, variant, cid, tmp)
                tt = exp.build_truth(tl, tw, ts, tr, tc, variant)
                certified = (tt["latent_truth"] != truth["latent_truth"]
                             and dump(view / "episode.sqlite") == dump(tmp / "episode.sqlite")
                             and read(view / "alert.json") == read(tmp / "alert.json"))
                shutil.rmtree(tmp)
                if not certified:
                    raise SystemExit(f"uncertified undecidable view: {cid} at offset {offset}")
                n_und += 1
            (dest / "private" / f"{cid}.json").write_text(json.dumps(truth, indent=1))
            entries.append({**e, "variant": variant})
        (dest / "private" / "index.json").write_text(json.dumps(entries, indent=1))
        shutil.rmtree(out_root / "_twin_tmp", ignore_errors=True)
        report["offsets"][offset] = {"views": len(entries), "undecidable_certified_by_twin": n_und,
                                     "equal_to_original": n_eq if offset == -30 else None}
        print(json.dumps({"offset": offset, **report["offsets"][offset]}), flush=True)
    (out_root / "sweep_report.json").write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
