"""Project an executed Indico world into a public investigation view and a private truth record.

Public view (episode.sqlite + alert.json) is what investigators see. Private truth is computed from
the execution ledger and the world state dump (complete logs from the setup on, current teams), never
from the investigator-facing logs alone, and is never shown to investigators.

Public tables (see audit_rules/indico/schema.md):
  audit_log       event log (Indico EventLogEntry rows), inside the coverage window
  category_log    category log (CategoryLogEntry rows), inside the coverage window
  app_log         application log (AppLogEntry rows), inside the coverage window
  users, categories, events, event_team, category_team   current state at export time
  source_coverage
Visibility variants (differ only in the coverage windows of the three logs):
  V0   every log from before all background activity (D-4);
  V1a  every log from 20 minutes before the audited change (earlier history hidden; team membership
       at t is decided by later records and the current teams);
  V1b  audit_log and app_log as V0; category_log only from 60 minutes after the audited change (the
       category team at t cannot be determined).
"""
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

VARIANTS = ("V0", "V1a", "V1b")
ADDED, REMOVED = "ACL entry added", "ACL entry removed"

SCHEMA = """
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, event_id INTEGER, user_id INTEGER, realm TEXT, kind TEXT,
  module TEXT, summary TEXT, data TEXT);
CREATE TABLE category_log (id INTEGER PRIMARY KEY, ts TEXT, category_id INTEGER, user_id INTEGER, realm TEXT, kind TEXT,
  module TEXT, summary TEXT, data TEXT);
CREATE TABLE app_log (id INTEGER PRIMARY KEY, ts TEXT, user_id INTEGER, realm TEXT, kind TEXT, module TEXT, summary TEXT,
  data TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, full_name TEXT, email TEXT, is_admin INTEGER);
CREATE TABLE categories (id INTEGER PRIMARY KEY, title TEXT, parent_id INTEGER);
CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT, category_id INTEGER, start_dt TEXT, end_dt TEXT);
CREATE TABLE event_team (event_id INTEGER, user_name TEXT, PRIMARY KEY (event_id, user_name));
CREATE TABLE category_team (category_id INTEGER, user_name TEXT, PRIMARY KEY (category_id, user_name));
CREATE TABLE source_coverage (source TEXT PRIMARY KEY, covered_from TEXT, covered_to TEXT, note TEXT);
"""
LOG_NOTE = "Complete for all recorded actions within the window. Nothing before covered_from is retained."
SNAP_NOTE = "Snapshot of current state at export time; no history."


def _ts(s):
    return datetime.fromisoformat(s)


def export_time(ledger):
    return _ts(ledger["T0"]) + timedelta(minutes=ledger["spec"]["export_minute"])


def coverage(ledger, variant):
    """Coverage start per log source for a variant."""
    T0 = _ts(ledger["T0"])
    base = ledger["spec"]["base"]
    full = T0 + timedelta(minutes=-4 * 24 * 60 - 60)
    if variant == "V0":
        return {"audit_log": full, "category_log": full, "app_log": full}
    if variant == "V1a":
        c = T0 + timedelta(minutes=base["t_s"] - 20)
        return {"audit_log": c, "category_log": c, "app_log": c}
    if variant == "V1b":
        return {"audit_log": full, "category_log": T0 + timedelta(minutes=base["t_s"] + 60), "app_log": full}
    raise ValueError(variant)


def find_seed_row(ledger, world):
    seed = next(s for s in ledger["steps"] if s.get("focal") == "seed")
    rows = [r for r in seed["rows"]["event_log"] if r["realm"] == "management" and r["user_id"] == seed["actor_user_id"]]
    assert len(rows) == 1, ("seed step did not write exactly one management row", seed.get("status"), seed.get("error"))
    full = [r for r in world["event_log"] if r["id"] == rows[0]["id"]]
    assert len(full) == 1
    return seed, full[0]


def build_public(world_dir, variant, case_id, out_dir):
    ledger = json.loads((world_dir / "ledger.json").read_text())
    world = json.loads((world_dir / "world_full.json").read_text())
    cov = coverage(ledger, variant)
    seed, seed_row = find_seed_row(ledger, world)
    exp = export_time(ledger)
    for n in ("event_log", "category_log", "app_log"):
        assert all(_ts(r["ts"]) < exp for r in world[n]), f"{n} row after export time"
    assert _ts(seed_row["ts"]) >= cov["audit_log"], "audited row outside the coverage window"
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / "episode.sqlite"
    if db.exists():
        db.unlink()
    tmp_dir = Path(tempfile.mkdtemp(prefix="in_export_", dir="/tmp"))
    tmp_db = tmp_dir / "episode.sqlite"
    con = sqlite3.connect(tmp_db)
    con.executescript(SCHEMA)
    for r in world["event_log"]:
        if _ts(r["ts"]) >= cov["audit_log"]:
            con.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["ts"], r["event_id"], r["user_id"], r["realm"], r["kind"], r["module"], r["summary"],
                         json.dumps(r["data"])))
    for r in world["category_log"]:
        if _ts(r["ts"]) >= cov["category_log"]:
            con.execute("INSERT INTO category_log VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["ts"], r["category_id"], r["user_id"], r["realm"], r["kind"], r["module"],
                         r["summary"], json.dumps(r["data"])))
    for r in world["app_log"]:
        if _ts(r["ts"]) >= cov["app_log"]:
            con.execute("INSERT INTO app_log VALUES (?,?,?,?,?,?,?,?)",
                        (r["id"], r["ts"], r["user_id"], r["realm"], r["kind"], r["module"], r["summary"],
                         json.dumps(r["data"])))
    name = {u["id"]: u["full_name"] for u in world["users"]}
    for u in world["users"]:
        con.execute("INSERT INTO users VALUES (?,?,?,?)", (u["id"], u["full_name"], u["email"], int(u["is_admin"])))
    for c in world["categories"]:
        con.execute("INSERT INTO categories VALUES (?,?,?)", (c["id"], c["title"], c["parent_id"]))
    for e in world["events"]:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?)", (e["id"], e["title"], e["category_id"], e["start_dt"], e["end_dt"]))
    for eid, members in world["event_team"].items():
        for uid in members:
            con.execute("INSERT INTO event_team VALUES (?,?)", (int(eid), name[uid]))
    for cid, members in world["category_team"].items():
        for uid in members:
            con.execute("INSERT INTO category_team VALUES (?,?)", (int(cid), name[uid]))
    for src in ("audit_log", "category_log", "app_log"):
        con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)", (src, cov[src].isoformat(), exp.isoformat(), LOG_NOTE))
    for src in ("users", "categories", "events", "event_team", "category_team"):
        con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)", (src, exp.isoformat(), exp.isoformat(), SNAP_NOTE))
    con.commit()
    con.close()
    shutil.move(str(tmp_db), str(db))
    shutil.rmtree(tmp_dir, ignore_errors=True)
    alert = {
        "case_id": case_id,
        "alert": {
            "audit_log_id": seed_row["id"], "ts": seed_row["ts"], "summary": seed_row["summary"],
            "actor_user_id": seed_row["user_id"], "event_id": seed_row["event_id"],
        },
        "question": ("Was this change to the event authorized under the organization's event management policy at "
                     "the time it was performed? Answer authorized, unauthorized, or insufficient_evidence, and cite "
                     "the records that establish your answer."),
    }
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=1))
    return ledger, world, seed, seed_row, cov


def _kleene_or(a, b):
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def membership(rows, scope_col, scope, subject_name, key, cov_from, exp, team_present, snap_table, snap_key):
    """Latent and projected membership of `subject_name` in the team of `scope` at the operation key.
    rows: the COMPLETE log of the source (from the setup on). Projection follows policy P3/P4."""
    changes = [r for r in rows if r[scope_col] == scope and r["summary"] in (ADDED, REMOVED)
               and isinstance(r["data"], dict) and r["data"].get("User") == subject_name]
    changes.sort(key=lambda r: (_ts(r["ts"]), r["id"]))
    before = [r for r in changes if (_ts(r["ts"]), r["id"]) < key]
    after = [r for r in changes if (_ts(r["ts"]), r["id"]) > key]
    latent = bool(before) and before[-1]["summary"] == ADDED
    t = key[0]
    vis_before = [r for r in before if _ts(r["ts"]) >= cov_from]
    vis_after = [r for r in after if _ts(r["ts"]) >= cov_from]
    proofs, value = [], None
    # (1) last change at or before t, if the source covers it up to t
    if vis_before and before[-1]["id"] == vis_before[-1]["id"]:
        proofs.append([{"source": snap_table[0], "id": vis_before[-1]["id"]}])
    # (2) first change after t, if the source covers t up to it
    if after and cov_from <= t and vis_after and vis_after[0]["id"] == after[0]["id"]:
        proofs.append([{"source": snap_table[0], "id": after[0]["id"]}])
    # (3) the current team when no change after t is recorded and the source covers t to the export
    if not after and cov_from <= t:
        proofs.append([{"source": snap_table[1], snap_key: scope, "user_name": subject_name, "present": team_present}])
    if proofs:
        value = latent
    if not after and cov_from <= t:
        assert team_present == latent, ("current team contradicts the log", scope, subject_name)
    return latent, value, proofs, [r["id"] for r in changes]


def build_truth(ledger, world, seed, seed_row, cov, variant):
    spec = ledger["spec"]
    base = spec["base"]
    t = _ts(seed_row["ts"])
    key = (t, seed_row["id"])
    exp = export_time(ledger)
    principal = seed_row["user_id"]
    event_id = seed_row["event_id"]
    name = {u["id"]: u["full_name"] for u in world["users"]}[principal]
    category_id = next(e["category_id"] for e in world["events"] if e["id"] == event_id)
    ev_present = principal in world["event_team"][str(event_id)]
    cat_present = principal in world["category_team"][str(category_id)]
    lat_a, val_a, proofs_a, rows_a = membership(world["event_log"], "event_id", event_id, name, key, cov["audit_log"], exp,
                                                ev_present, ("audit_log", "event_team"), "event_id")
    lat_b, val_b, proofs_b, rows_b = membership(world["category_log"], "category_id", category_id, name, key,
                                                cov["category_log"], exp, cat_present, ("category_log", "category_team"),
                                                "category_id")

    # Cross-check against the execution ledger (which ACL step ran, on what, for whom, when).
    def ledger_state(prefix, scope_key, scope_id, initial):
        state = initial
        for s in ledger["steps"]:
            if s["kind"].startswith(prefix) and s.get("ok") and s.get(scope_key) == scope_id and \
                    s.get("subject_user_id") == principal and _ts(s["t_iso"]) < t:
                state = s["kind"].endswith("_add")
        return state
    role = next(r for r, i in ledger["user_ids"].items() if i == principal)
    ev_key = next(k for k, i in ledger["event_ids"].items() if i == event_id)
    cat_key = next(k for k, i in ledger["category_ids"].items() if i == category_id)
    init_a = role in base["initial_event_teams"].get(ev_key, [])
    init_b = role in base["initial_category_teams"].get(cat_key, [])
    assert ledger_state("event_acl_", "event_id", event_id, init_a) == lat_a, ("ledger/log disagree on (a)", spec["world_id"])
    assert ledger_state("category_acl_", "category_id", category_id, init_b) == lat_b, ("ledger/log disagree on (b)", spec["world_id"])

    latent = "authorized" if (lat_a or lat_b) else "unauthorized"
    verdict = _kleene_or(val_a, val_b)
    answerable = verdict is not None
    if answerable:
        assert ("authorized" if verdict else "unauthorized") == latent, ("projection contradicts latent truth", spec["world_id"])
    alternatives = []
    if verdict is True:
        if val_a is True:
            alternatives += proofs_a
        if val_b is True:
            alternatives += proofs_b
    elif verdict is False:
        alternatives = [pa + pb for pa in proofs_a for pb in proofs_b]
    basis = {True: "on a team at t", False: "on neither team at t", None: "team membership at t not determinable"}[verdict]
    return {
        "variant": variant, "world_id": spec["world_id"], "base_id": base["base_id"], "pattern": base["pattern"],
        "split": base["split"], "world_kind": spec["world_kind"],
        "latent_truth": latent, "event_team_at_t": lat_a, "category_team_at_t": lat_b,
        "projected_event_team": val_a, "projected_category_team": val_b,
        "answerable": answerable, "answerability_basis": basis,
        "expected_verdict": latent if answerable else "insufficient_evidence",
        "seed_row_id": seed_row["id"], "actor_user_id": principal, "actor_name": name, "event_id": event_id,
        "category_id": category_id, "event_team_change_rows": rows_a, "category_team_change_rows": rows_b,
        "proof_alternatives": alternatives,
        "coverage_from": {k: v.isoformat() for k, v in cov.items()}, "export_time": exp.isoformat(),
    }
