"""Validity checks of the pretix test bed. G0.4 (paired worlds differ only in deciding rows), G0.5 (twins
of undecidable worlds have identical public views) and G0.7 (no public case contains a label string) are
method-free and run on all splits; G0.6 runs the expert rules on the development split only.
Output: runs/gate0_report.json.
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "methods"))
sys.path.insert(0, str(ROOT / "evaluation"))
import export_case  # noqa: E402
import rules  # noqa: E402
import score  # noqa: E402
from sqltool import SQLTool  # noqa: E402

CASES = ROOT / "cases"
TABLES = ("audit_log", "users", "teams", "team_members", "team_tokens", "events", "orders", "source_coverage")


def dump(db):
    con = sqlite3.connect(db)
    out = {t: sorted(map(tuple, con.execute(f"SELECT * FROM {t}").fetchall()), key=repr) for t in TABLES}
    con.close()
    return out


def audit_content(db):
    con = sqlite3.connect(db)
    rows = con.execute("SELECT ts, action, actor_user_id, actor_token_id, event, object_type, object_id, data "
                       "FROM audit_log").fetchall()
    acts = dict(con.execute("SELECT ts || '|' || action || '|' || IFNULL(actor_token_id,'') || '|' || "
                            "IFNULL(actor_user_id,'') || '|' || object_id || '|' || data, action FROM audit_log").fetchall())
    con.close()
    return Counter(rows), acts


def main():
    index = json.loads((CASES / "private" / "index.json").read_text())
    report = {}

    # G0.4: AUTH/UNAUTH pairs differ only in decisive focal records (V0 public views).
    by_world = {(e["world_id"], e["variant"]): e["case_id"] for e in index}
    bases = sorted({e["base_id"] for e in index})
    g04 = defaultdict(Counter)
    g04_sizes = []
    for b in bases:
        ca = CASES / "public" / by_world[(f"{b}__AUTH", "V0")] / "episode.sqlite"
        cu = CASES / "public" / by_world[(f"{b}__UNAUTH", "V0")] / "episode.sqlite"
        (ra, _), (ru, _) = audit_content(ca), audit_content(cu)
        diff = (ra - ru) + (ru - ra)
        g04_sizes.append(sum(diff.values()))
        pattern = b.rsplit("_", 1)[0]
        for row, n in diff.items():
            g04[pattern][row[1]] += n
        da, du = dump(ca), dump(cu)
        for t in TABLES[1:]:
            if da[t] != du[t]:
                g04[pattern][f"table:{t}"] += 1
    report["G0.4"] = {"differing_audit_rows_per_pair": dict(Counter(g04_sizes)),
                      "differing_actions_by_pattern": {k: dict(v) for k, v in g04.items()}}

    # G0.5: issuer-swap twins (opposite latent truth) vs originals.
    tw_dir = CASES / "gate0_twins"
    same_v1b, same_v0, n = 0, 0, 0
    truth_flip = 0
    for b in bases:
        for kind in ("AUTH", "UNAUTH"):
            orig, twin = CASES / "worlds" / f"{b}__{kind}", CASES / "worlds" / f"{b}__{kind}__twin"
            views = {}
            for tag, wdir in (("orig", orig), ("twin", twin)):
                for v in ("V0", "V1b"):
                    out = tw_dir / f"{b}__{kind}" / tag / v
                    led, w, seed, srow, cov = export_case.build_public(wdir, v, "cTWINCHECK", out)
                    views[(tag, v)] = dump(out / "episode.sqlite")
                    if v == "V0":
                        views[(tag, "truth")] = export_case.build_truth(led, w, seed, srow, cov, v)["latent_truth"]
            n += 1
            same_v1b += views[("orig", "V1b")] == views[("twin", "V1b")]
            same_v0 += views[("orig", "V0")] == views[("twin", "V0")]
            truth_flip += views[("orig", "truth")] != views[("twin", "truth")]
    report["G0.5"] = {"pairs": n, "latent_truth_differs": truth_flip,
                      "identical_public_view_V1b": same_v1b, "identical_public_view_V0": same_v0}

    # G0.7: leakage scan of every public file.
    forbidden = ["AUTH", "UNAUTH", "twin", "removal_order", "readd", "other_team", "token_confusion",
                 "engE", "engF", "engG", "engH", "adm1", "adm2", "box1", "box2", "focal", "latent", "answerab"]
    hits = Counter()
    for d in (CASES / "public").iterdir():
        blob = (d / "alert.json").read_text()
        con = sqlite3.connect(d / "episode.sqlite")
        for t in TABLES:
            blob += repr(con.execute(f"SELECT * FROM {t}").fetchall())
        con.close()
        for f in forbidden:
            if f in blob:
                hits[f] += 1
    report["G0.7"] = {"public_cases_scanned": len(list((CASES / "public").iterdir())),
                      "forbidden_string_hits": dict(hits)}

    # G0.6 (dev only): expert rules + weak-rule ablations under the shared budget.
    dev = [e for e in index if e["split"] == "dev" and e["world_ok"]]
    g06 = {}
    for name, kw in (("expert_rules", {}), ("closed_world_rules", {"closed_world": True}),
                     ("current_state_rules", {"current_state_only": True})):
        recs, max_q = [], 0
        for e in dev:
            cdir = CASES / "public" / e["case_id"]
            truth = json.loads((CASES / "private" / f"{e['case_id']}.json").read_text())
            tool = SQLTool(cdir, max_queries=12)
            sub = rules.investigate(tool, **kw)
            ids = [r[0] for r in sqlite3.connect(cdir / "episode.sqlite").execute("SELECT id FROM audit_log")]
            rec = score.score(truth, sub, ids)
            rec["queries"] = tool.queries
            rec["n_evidence"] = len(sub["evidence"])
            max_q = max(max_q, tool.queries)
            recs.append(rec)
        ans = [r for r in recs if r["answerable"]]
        una = [r for r in recs if not r["answerable"]]
        g06[name] = {
            "answerable_success": f"{sum(r['success'] for r in ans)}/{len(ans)}",
            "answerable_success_by_truth": {lt: f"{sum(r['success'] for r in ans if r['latent_truth']==lt)}/"
                                                f"{sum(1 for r in ans if r['latent_truth']==lt)}"
                                            for lt in ("authorized", "unauthorized")},
            "unanswerable_correct_abstain": f"{sum(r['success'] for r in una)}/{len(una)}",
            "false_escalation": sum(r["false_escalation"] for r in ans),
            "max_queries": max_q, "max_evidence_items": max(r["n_evidence"] for r in recs),
            "by_pattern_success": {p: f"{sum(r['success'] for r in recs if r['pattern']==p)}/"
                                      f"{sum(1 for r in recs if r['pattern']==p)}"
                                   for p in sorted({r["pattern"] for r in recs})},
        }
    report["G0.6_dev"] = g06
    (ROOT / "runs" / "gate0_report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
