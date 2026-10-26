"""Validity checks of the Weblate test bed (cases/weblate). G0.4, G0.5 and G0.7 are method-free and run on
all splits; G0.6 runs the expert rules (methods/rules_weblate.py) and, as a second reference, the
evaluator with the hand-written reference specification (audit_rules/weblate/reference_spec.json), on the
development split only. Output: runs/gate0_weblate_report.json.
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness" / "weblate"))
sys.path.insert(0, str(ROOT / "methods"))
sys.path.insert(0, str(ROOT / "evaluation"))
import export_case  # noqa: E402
import evaluator  # noqa: E402
import rules_weblate  # noqa: E402
import score_generic  # noqa: E402
import weblate_env  # noqa: E402
from sqltool import SQLTool  # noqa: E402

CASES = ROOT / "cases" / "weblate"
WORLDS = weblate_env.WORLDS_ROOT
TABLES = ("audit_log", "account_log", "users", "teams", "team_members", "tokens", "token_validity", "projects",
          "units", "source_coverage")
SPEC = json.loads((ROOT / "audit_rules" / "weblate" / "reference_spec.json").read_text())
SPEC.pop("_note", None)


def dump(db):
    con = sqlite3.connect(db)
    out = {t: sorted(map(tuple, con.execute(f"SELECT * FROM {t}").fetchall()), key=repr) for t in TABLES}
    con.close()
    return out


def log_content(db):
    """Rows of both logs without ids (ids differ between worlds); used for pair differences."""
    con = sqlite3.connect(db)
    rows = [("audit_log",) + tuple(r) for r in con.execute(
        "SELECT ts, action_name, user_id, author_id, unit_id, target, details FROM audit_log")]
    rows += [("account_log",) + tuple(r) for r in con.execute(
        "SELECT ts, user_id, username, activity, actor_username, team, project FROM account_log")]
    con.close()
    return Counter(rows)


def main():
    index = json.loads((CASES / "private" / "index.json").read_text())
    report = {}
    by_world = {(e["world_id"], e["variant"]): e["case_id"] for e in index}
    bases = sorted({e["base_id"] for e in index})

    # G0.4: AUTH/UNAUTH pairs differ only in decisive records (V0 public views).
    g04, g04_sizes, g04_tables = defaultdict(Counter), [], defaultdict(Counter)
    for b in bases:
        ca = CASES / "public" / by_world[(f"{b}__AUTH", "V0")] / "episode.sqlite"
        cu = CASES / "public" / by_world[(f"{b}__UNAUTH", "V0")] / "episode.sqlite"
        ra, ru = log_content(ca), log_content(cu)
        diff = (ra - ru) + (ru - ra)
        g04_sizes.append(sum(diff.values()))
        pattern = b.rsplit("_", 1)[0]
        for row, n in diff.items():
            label = f"{row[0]}:{row[2] if row[0] == 'audit_log' else row[4]}"
            g04[pattern][label] += n
        da, du = dump(ca), dump(cu)
        for t in TABLES[2:]:
            if da[t] != du[t]:
                g04_tables[pattern][t] += 1
    report["G0.4"] = {"differing_log_rows_per_pair": dict(Counter(g04_sizes)),
                      "differing_rows_by_pattern": {k: dict(v) for k, v in g04.items()},
                      "differing_state_tables_by_pattern": {k: dict(v) for k, v in g04_tables.items()}}

    # G0.5: every unanswerable case has a twin world of opposite latent truth whose public view under
    # the same variant is identical (creator-swap twins; expiry-decided cases are never unanswerable).
    tw_dir = CASES / "gate0_twins"
    n_unans, n_with_twin, n_identical, examined = 0, 0, 0, 0
    detail = Counter()
    for e in index:
        if not e["world_ok"]:
            continue
        truth = json.loads((CASES / "private" / f"{e['case_id']}.json").read_text())
        if truth["answerable"]:
            continue
        n_unans += 1
        b, kind, v = truth["base_id"], truth["world_kind"], truth["variant"]
        orig, twin = WORLDS / f"{b}__{kind}", WORLDS / f"{b}__{kind}__twin"
        if not (twin / "status.json").exists() or not json.loads((twin / "status.json").read_text())["ok"]:
            detail["twin_missing"] += 1
            continue
        views, truths = {}, {}
        for tag, wdir in (("orig", orig), ("twin", twin)):
            out = tw_dir / f"{b}__{kind}" / tag / v
            led, w, seed, srow, cov = export_case.build_public(wdir, v, "cTWINCHECK", out)
            views[tag] = dump(out / "episode.sqlite")
            truths[tag] = export_case.build_truth(led, w, seed, srow, cov, v)["latent_truth"]
        examined += 1
        if truths["orig"] != truths["twin"]:
            n_with_twin += 1
            if views["orig"] == views["twin"]:
                n_identical += 1
            else:
                detail[f"view_differs:{truth['pattern']}:{v}"] += 1
        else:
            detail[f"truth_same:{truth['pattern']}:{v}"] += 1
    report["G0.5"] = {"unanswerable_cases": n_unans, "examined": examined,
                      "twin_with_opposite_truth": n_with_twin, "identical_public_view": n_identical,
                      "detail": dict(detail)}

    # G0.7: leakage scan of every public file.
    forbidden = ["AUTH", "UNAUTH", "twin", "removal_order", "readd", "other_team", "token_confusion", "expiry_order",
                 "engE", "engF", "engG", "engH", "adm1", "adm2", "rev1", "rev2", "tr1", "tr2", "focal", "latent",
                 "answerab", "K_E", "K_F"]
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

    # G0.6 (dev only): expert rules (+ ablations) and the evaluator with the hand-written spec.
    dev = [e for e in index if e["split"] == "dev" and e["world_ok"]]
    spec = evaluator.validate_spec(SPEC)
    methods = (("expert_rules", lambda tool: rules_weblate.investigate(tool)),
               ("closed_world_rules", lambda tool: rules_weblate.investigate(tool, closed_world=True)),
               ("current_state_rules", lambda tool: rules_weblate.investigate(tool, current_state_only=True)),
               ("evaluator_handwritten_spec", lambda tool: evaluator.evaluate(tool, spec)))
    g06 = {}
    for name, fn in methods:
        recs, max_q = [], 0
        for e in dev:
            cdir = CASES / "public" / e["case_id"]
            truth = json.loads((CASES / "private" / f"{e['case_id']}.json").read_text())
            tool = SQLTool(cdir, max_queries=12)
            sub = fn(tool)
            ids = [r[0] for r in sqlite3.connect(cdir / "episode.sqlite").execute("SELECT id FROM audit_log")]
            rec = score_generic.score(truth, sub, ids, observed_ids=tool.observed_audit_ids,
                                      observed_tables=tool.observed_tables)
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
            "by_variant_success": {v: f"{sum(r['success'] for r in recs if r['variant']==v)}/"
                                      f"{sum(1 for r in recs if r['variant']==v)}"
                                   for v in sorted({r["variant"] for r in recs})},
        }
    report["G0.6_dev"] = g06
    (ROOT / "runs" / "gate0_weblate_report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
