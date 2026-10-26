"""Validity checks of the Indico test bed (cases/indico). G0.4, G0.5 and G0.7 are method-free and run on
all splits; G0.6 runs the expert rules (methods/rules_indico.py) with its weak-rule variants and, as a second reference, the
evaluator with the hand-written reference specification (audit_rules/indico/reference_spec.json), on the
development split only. Output: runs/gate0_indico_report.json.
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness" / "indico"))
sys.path.insert(0, str(ROOT / "methods"))
sys.path.insert(0, str(ROOT / "evaluation"))
import evaluator  # noqa: E402
import export_case  # noqa: E402
import indico_env  # noqa: E402
import rules_indico  # noqa: E402
import score_generic  # noqa: E402
import worlds  # noqa: E402
from sqltool import SQLTool  # noqa: E402

CASES = ROOT / "cases" / "indico"
WORLDS = indico_env.WORLDS_ROOT
TABLES = ("audit_log", "category_log", "app_log", "users", "categories", "events", "event_team", "category_team",
          "source_coverage")
SPEC = json.loads((ROOT / "audit_rules" / "indico" / "reference_spec.json").read_text())
SPEC.pop("_note", None)


def dump(db):
    con = sqlite3.connect(db)
    out = {t: sorted(map(tuple, con.execute(f"SELECT * FROM {t}").fetchall()), key=repr) for t in TABLES}
    con.close()
    return out


def log_content(db):
    """Rows of the three logs without ids; for pair differences."""
    con = sqlite3.connect(db)
    rows = [("audit_log",) + tuple(r) for r in con.execute("SELECT ts, event_id, user_id, summary, data FROM audit_log")]
    rows += [("category_log",) + tuple(r) for r in con.execute("SELECT ts, category_id, user_id, summary, data FROM category_log")]
    rows += [("app_log",) + tuple(r) for r in con.execute("SELECT ts, user_id, summary, data FROM app_log")]
    con.close()
    return Counter(rows)


def main():
    index = json.loads((CASES / "private" / "index.json").read_text())
    report = {"patterns_core": list(worlds.PATTERNS_CORE), "patterns_holdout": list(worlds.PATTERNS_HOLDOUT)}
    by_world = {(e["world_id"], e["variant"]): e["case_id"] for e in index}
    bases = sorted({e["base_id"] for e in index})
    truths = {e["case_id"]: json.loads((CASES / "private" / f"{e['case_id']}.json").read_text())
              for e in index if e["world_ok"]}
    report["worlds_ok"] = f"{sum(e['world_ok'] for e in index)}/{len(index)} cases from ok worlds"
    report["split_counts"] = {f"{k[0]}:{k[1]}": n for k, n in sorted(Counter((t["split"], t["pattern"]) for t in truths.values()).items())}
    report["holdout_in_dev"] = sum(1 for t in truths.values() if t["split"] == "dev" and t["pattern"] in worlds.PATTERNS_HOLDOUT)
    report["expected_by_split"] = {f"{k[0]}:{k[1]}": n for k, n in sorted(Counter((t["split"], t["expected_verdict"]) for t in truths.values()).items())}

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
            g04[pattern][f"{row[0]}:{row[-2] if row[0] != 'app_log' else row[2]}"] += n
        da, du = dump(ca), dump(cu)
        for t in TABLES[3:]:
            if da[t] != du[t]:
                g04_tables[pattern][t] += 1
    report["G0.4"] = {"differing_log_rows_per_pair": dict(Counter(g04_sizes)),
                      "differing_rows_by_pattern": {k: dict(v) for k, v in g04.items()},
                      "differing_state_tables_by_pattern": {k: dict(v) for k, v in g04_tables.items()}}

    # G0.5: every unanswerable case has a twin world of opposite latent truth whose public view under
    # the same variant is identical.
    tw_dir = CASES / "gate0_twins"
    n_unans, n_with_twin, n_identical, examined = 0, 0, 0, 0
    detail = Counter()
    for e in index:
        if not e["world_ok"]:
            continue
        truth = truths[e["case_id"]]
        if truth["answerable"]:
            continue
        n_unans += 1
        detail[f"unanswerable:{truth['pattern']}:{truth['world_kind']}:{truth['variant']}"] += 1
        b, kind, v = truth["base_id"], truth["world_kind"], truth["variant"]
        orig, twin = WORLDS / f"{b}__{kind}", WORLDS / f"{b}__{kind}__twin"
        if not (twin / "status.json").exists() or not json.loads((twin / "status.json").read_text())["ok"]:
            detail["twin_missing"] += 1
            continue
        views, tr = {}, {}
        for tag, wdir in (("orig", orig), ("twin", twin)):
            out = tw_dir / f"{b}__{kind}" / tag / v
            led, w, seed, srow, cov = export_case.build_public(wdir, v, "cTWINCHECK", out)
            views[tag] = dump(out / "episode.sqlite")
            tr[tag] = export_case.build_truth(led, w, seed, srow, cov, v)["latent_truth"]
        examined += 1
        if tr["orig"] != tr["twin"]:
            n_with_twin += 1
            if views["orig"] == views["twin"]:
                n_identical += 1
            else:
                diff_tables = [t for t in TABLES if views["orig"][t] != views["twin"][t]]
                detail[f"view_differs:{truth['pattern']}:{kind}:{v}:{','.join(diff_tables)}"] += 1
        else:
            detail[f"truth_same:{truth['pattern']}:{kind}:{v}"] += 1
    report["G0.5"] = {"unanswerable_cases": n_unans, "examined": examined,
                      "twin_with_opposite_truth": n_with_twin, "identical_public_view": n_identical,
                      "detail": dict(detail)}

    # G0.7: leakage scan of every public file.
    forbidden = ["AUTH", "UNAUTH", "twin", "focal", "latent", "answerab", "seed", "stand-in", "standin", "holdout",
                 "decoy"] + list(worlds.PATTERNS) + list(worlds.ROLES)
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
    methods = (("expert_rules", lambda tool: rules_indico.investigate(tool)),
               ("closed_world_rules", lambda tool: rules_indico.investigate(tool, closed_world=True)),
               ("current_state_only_rules", lambda tool: rules_indico.investigate(tool, current_state_only=True)),
               ("evaluator_handwritten_spec", lambda tool: evaluator.evaluate(tool, spec)))
    g06 = {}
    for name, fn in methods:
        recs, max_q = [], 0
        for e in dev:
            cdir = CASES / "public" / e["case_id"]
            truth = truths[e["case_id"]]
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
            "false_authorization": sum(1 for r in ans if r["latent_truth"] == "unauthorized" and r["verdict"] == "authorized"),
            "max_queries": max_q, "max_evidence_items": max(r["n_evidence"] for r in recs),
            "by_pattern_success": {p: f"{sum(r['success'] for r in recs if r['pattern']==p)}/"
                                      f"{sum(1 for r in recs if r['pattern']==p)}"
                                   for p in sorted({r["pattern"] for r in recs})},
            "by_variant_success": {v: f"{sum(r['success'] for r in recs if r['variant']==v)}/"
                                      f"{sum(1 for r in recs if r['variant']==v)}"
                                   for v in sorted({r["variant"] for r in recs})},
        }
    report["G0.6_dev"] = g06
    (ROOT / "runs" / "gate0_indico_report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
