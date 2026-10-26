"""Human annotation packets.

For every test world of the four applications: the alert, the complete record (all log rows, coverage
pushed back 30 days before T0, plus the export-time current-state tables) and the three views the methods
saw (V0/V1a/V1b, shown under shuffled letters). No labels, world names, case ids or method outputs go into
the packet; the mapping from letters to cases and the ledger truth stay in mapping_private.json next to it.
Read-only on all inputs. Writes runs/annotation/annotation_packet/ (see
audit_results/human_annotation/ANNOTATION_GUIDE.md for the instructions given to the annotators).
"""
import csv
import importlib.util
import json
import os
import random
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("CETA_DATA", str(ROOT / "data")))  # executed worlds of weblate / wagtail / indico
OUT = ROOT / "runs" / "annotation"
PACKET = OUT / "annotation_packet"
TMP = Path(os.environ.get("CETA_TMP", "/tmp/ceta")) / "annotation_build"
SEED = 20261026
APPS = {
    "pretix": dict(code="PX", base=ROOT / "cases", worlds=ROOT / "cases/worlds", exporter=ROOT / "harness/export_case.py"),
    "weblate": dict(code="WB", base=ROOT / "cases/weblate", worlds=DATA / "worlds/weblate", exporter=ROOT / "harness/weblate/export_case.py"),
    "wagtail": dict(code="WT", base=ROOT / "cases/wagtail", worlds=DATA / "worlds/wagtail", exporter=ROOT / "harness/wagtail/export_case.py"),
    "indico": dict(code="IN", base=ROOT / "cases/indico", worlds=DATA / "worlds/indico", exporter=ROOT / "harness/indico/export_case.py"),
}
VARIANTS = ["V0", "V1a", "V1b"]


def load_module(app, path):
    spec = importlib.util.spec_from_file_location("ht_export_" + app, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def dump(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cur = con.execute(f'SELECT * FROM "{name}"')
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if "id" in cols:
            rows.sort(key=lambda r: (r["id"] is None, r["id"]))
        out[name] = {"columns": cols, "rows": rows}
    con.close()
    return out


def ranges(ids):
    ids = sorted(ids)
    if not ids:
        return "none"
    parts, start, prev = [], ids[0], ids[0]
    for i in ids[1:]:
        if isinstance(i, int) and isinstance(prev, int) and i == prev + 1:
            prev = i
            continue
        parts.append(str(start) if start == prev else f"{start}–{prev}")
        start = prev = i
    parts.append(str(start) if start == prev else f"{start}–{prev}")
    return ", ".join(parts)


def jsonl(rows):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)


def render_full(tables):
    lines = []
    for name, t in tables.items():
        lines += [f"### {name} ({len(t['rows'])} rows)", "", "```jsonl", jsonl(t["rows"]) or "(empty table)", "```", ""]
    return lines


def render_view(view, full):
    lines = []
    cov = view.get("source_coverage", {"rows": []})["rows"]
    lines += ["Coverage (source_coverage):", "", "```jsonl", jsonl(cov), "```", ""]
    for name, t in view.items():
        if name == "source_coverage":
            continue
        f = full.get(name)
        if f is not None and t["rows"] == f["rows"]:
            lines.append(f"- **{name}**: identical to the complete record ({len(t['rows'])} rows).")
            continue
        if f is not None and "id" in t["columns"] and t["columns"] == f["columns"]:
            fmap = {r["id"]: r for r in f["rows"]}
            if all(r["id"] in fmap and fmap[r["id"]] == r for r in t["rows"]):
                vis = [r["id"] for r in t["rows"]]
                hid = [i for i in fmap if i not in set(vis)]
                lines.append(f"- **{name}**: {len(vis)} rows visible (ids {ranges(vis)}); {len(hid)} further rows of the "
                             f"complete record are **not visible** in this view (ids {ranges(hid)}).")
                continue
        lines += [f"- **{name}**: differs from the complete record; full content ({len(t['rows'])} rows):", "", "```jsonl",
                  jsonl(t["rows"]) or "(empty table)", "```"]
    lines.append("")
    return lines


def main():
    if PACKET.exists():
        sys.exit(f"{PACKET} exists; refusing to overwrite")
    rng = random.Random(SEED)
    mapping = {"created_at": datetime.utcnow().isoformat() + "Z", "seed": SEED, "apps": {}}
    gate = {}
    for app, cfg in APPS.items():
        mod = load_module(app, cfg["exporter"])
        index = [e for e in json.loads((cfg["base"] / "private/index.json").read_text()) if e["split"] == "test"]
        worlds = {}
        for e in index:
            worlds.setdefault(e["world_id"], {})[e["variant"]] = e["case_id"]
        assert all(set(v) == set(VARIANTS) for v in worlds.values()), app
        order = sorted(worlds)
        rng.shuffle(order)
        adir = PACKET / app
        (adir / "worlds").mkdir(parents=True)
        for doc in ("policy.md", "schema.md"):
            shutil.copy2(ROOT / "audit_rules" / app / doc, adir / doc)
        app_mapping = []
        form1, form2 = [], []
        n_full_ok = 0
        for k, wid in enumerate(order, 1):
            label = f"{cfg['code']}-{k:03d}"
            wdir = cfg["worlds"] / wid
            ledger = json.loads((wdir / "ledger.json").read_text())
            far = datetime.fromisoformat(ledger["T0"]) - timedelta(days=30)
            if app == "indico":
                mod.coverage = lambda led, var, far=far: {"audit_log": far, "category_log": far, "app_log": far}
            else:
                mod.coverage_start = lambda led, var, far=far: far
            tmp = TMP / app / label
            if tmp.exists():
                shutil.rmtree(tmp)
            led, w, seed, srow, cov = mod.build_public(wdir, "V0", "full", tmp)
            truth_full = mod.build_truth(led, w, seed, srow, cov, "V0")
            v0_truth = json.loads((cfg["base"] / "private" / f"{worlds[wid]['V0']}.json").read_text())
            assert truth_full["answerable"] is True, (app, wid, "full view not decidable")
            assert truth_full["latent_truth"] == v0_truth["latent_truth"], (app, wid, "full-view truth differs")
            n_full_ok += 1
            full = dump(tmp / "episode.sqlite")
            full.pop("source_coverage", None)
            alerts = {}
            for v in VARIANTS:
                a = json.loads((cfg["base"] / "public" / worlds[wid][v] / "alert.json").read_text())
                alerts[v] = a["alert"]
            assert all(alerts[v] == alerts["V0"] for v in VARIANTS), (app, wid, "alerts differ")
            letters = ["A", "B", "C"]
            perm = VARIANTS[:]
            rng.shuffle(perm)
            lines = [f"# {label} ({app})", "",
                     "Read policy.md and schema.md in the parent folder first; the annotation guide explains the forms.", "",
                     "## Alert", "", "```json", json.dumps(alerts["V0"], ensure_ascii=False, indent=1), "```", "",
                     "## Part 1: complete record (for form 1: world verdict)", "",
                     "Every log starts at its earliest row and is not truncated; current-state tables show the state "
                     "at export time.", ""]
            lines += render_full(full)
            lines += ["## Part 2: three views (for form 2: can this view alone decide?)", "",
                      "Each view is the data a method actually received: logs keep only the rows inside their coverage "
                      "window, current-state tables are unchanged. When judging a view, use only the rows visible in "
                      "that view.", ""]
            views = []
            for letter, v in zip(letters, perm):
                cid = worlds[wid][v]
                vt = dump(cfg["base"] / "public" / cid / "episode.sqlite")
                lines += [f"### View {letter}", ""] + render_view(vt, full)
                truth = json.loads((cfg["base"] / "private" / f"{cid}.json").read_text())
                views.append({"view": letter, "variant": v, "case_id": cid, "answerable": truth["answerable"],
                              "expected_verdict": truth.get("expected_verdict"),
                              "proof_alternatives": truth.get("proof_alternatives") or truth.get("membership_proofs")})
                form2.append([label, letter, "", "", "", ""])
            (adir / "worlds" / f"{label}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            form1.append([label, "", "", "", ""])
            app_mapping.append({"label": label, "world_id": wid, "latent_truth": v0_truth["latent_truth"],
                                 "pattern": v0_truth.get("pattern"), "views": views})
            shutil.rmtree(tmp)
        with open(adir / f"form1_world_verdicts_{app}.csv", "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f)
            wr.writerow(["world", "verdict (authorized/unauthorized)", "key_records", "uncertain (yes)", "notes"])
            wr.writerows(sorted(form1))
        with open(adir / f"form2_view_verdicts_{app}.csv", "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f)
            wr.writerow(["world", "view", "verdict (authorized/unauthorized/insufficient_evidence)", "evidence",
                         "uncertain (yes)", "notes"])
            wr.writerows(sorted(form2))
        mapping["apps"][app] = sorted(app_mapping, key=lambda r: r["label"])
        gate[app] = {"worlds": len(order), "views": 3 * len(order), "full_view_decidable_and_equal": n_full_ok}
        print(app, gate[app], flush=True)
    mapping["gate"] = gate
    (OUT / "mapping_private.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=1))
    # leak scan of the packet
    bad = []
    names = {r["world_id"] for a in mapping["apps"].values() for r in a}
    cids = {v["case_id"] for a in mapping["apps"].values() for r in a for v in r["views"]}
    for p in PACKET.rglob("*"):
        if p.is_file() and p.suffix in (".md", ".csv"):
            s = p.read_text(encoding="utf-8-sig")
            for tok in ["__AUTH", "__UNAUTH", "latent_truth", "answerable", "world_kind", "V1a", "V1b"]:
                if tok in s:
                    bad.append((str(p), tok))
            for c in cids:
                if c in s:
                    bad.append((str(p), "case_id"))
                    break
    print("LEAK_SCAN", bad[:10], len(bad))
    (OUT / "build_report.json").write_text(json.dumps({"gate": gate, "leak_scan": bad}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
