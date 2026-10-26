"""Project an executed Wagtail world into a public investigation view and a private truth record.

Public view (episode.sqlite + alert.json) is what investigators see. Private truth is computed from
the execution ledger and the world state dump, never from the investigator-facing logs alone, and is
never shown to investigators.

Public tables (see audit_rules/wagtail/schema.md):
  audit_log       page log (Wagtail PageLogEntry rows), inside the coverage window
  model_log       log of actions on users and groups (ModelLogEntry rows), inside the coverage window
  users, groups, group_members, pages, revisions, workflows, workflow_tasks, workflow_pages
                  current state at export time
  source_coverage
Visibility variants: V0 = coverage from before all background activity; V1a = coverage starts just
before the policy's 48 h window of the audited publication (older history hidden; the window is
complete); V1b = coverage starts at the base event's cutoff_b (just after the focal submission for
the patterns whose decisive fact is the hidden submitter; just before the window otherwise).
"""
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

VARIANTS = ("V0", "V1a", "V1b")
START, APPROVE, PUBLISH = "wagtail.workflow.start", "wagtail.workflow.approve", "wagtail.publish"

SCHEMA = """
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, action TEXT, user_id INTEGER, page_id INTEGER,
  revision_id INTEGER, content_changed INTEGER, data TEXT);
CREATE TABLE model_log (id INTEGER PRIMARY KEY, ts TEXT, action TEXT, user_id INTEGER, object_type TEXT,
  object_id TEXT, data TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, is_superuser INTEGER,
  is_active INTEGER);
CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE group_members (group_name TEXT, username TEXT, PRIMARY KEY (group_name, username));
CREATE TABLE pages (id INTEGER PRIMARY KEY, title TEXT, slug TEXT, parent_id INTEGER, live INTEGER,
  live_revision_id INTEGER, latest_revision_id INTEGER, first_published_at TEXT, last_published_at TEXT);
CREATE TABLE revisions (id INTEGER PRIMARY KEY, page_id INTEGER, created_at TEXT);
CREATE TABLE workflows (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE workflow_tasks (workflow_id INTEGER, sort_order INTEGER, task_name TEXT, approver_group TEXT);
CREATE TABLE workflow_pages (page_id INTEGER, workflow_id INTEGER);
CREATE TABLE source_coverage (source TEXT PRIMARY KEY, covered_from TEXT, covered_to TEXT, note TEXT);
"""


def _ts(s):
    return datetime.fromisoformat(s)


def step_ts(ledger, step):
    return (_ts(ledger["T0"]) + timedelta(minutes=step["minute"])).isoformat()


def coverage_start(ledger, variant):
    T0 = _ts(ledger["T0"])
    spec = ledger["spec"]
    base = spec["base"]
    if variant == "V0":
        return T0 + timedelta(minutes=-4 * 24 * 60 - 60)
    if variant == "V1a":
        return T0 + timedelta(minutes=base["t_s"] - spec["window_minutes"] - 10)
    if variant == "V1b":
        return T0 + timedelta(minutes=base["cutoff_b"][spec["world_kind"]])
    raise ValueError(variant)


def export_time(ledger):
    return _ts(ledger["T0"]) + timedelta(minutes=ledger["spec"]["export_minute"])


def find_seed_row(ledger, world):
    seed = next(s for s in ledger["steps"] if s.get("focal") == "seed")
    pub = [r for r in seed.get("rows", []) if r["action"] == PUBLISH]
    assert len(pub) == 1, ("seed step did not write exactly one publish row", seed.get("status"), seed.get("error"))
    rows = [r for r in world["page_log"] if r["id"] == pub[0]["id"]]
    assert len(rows) == 1
    return seed, rows[0]


def build_public(world_dir, variant, case_id, out_dir):
    ledger = json.loads((world_dir / "ledger.json").read_text())
    world = json.loads((world_dir / "world_full.json").read_text())
    cov = coverage_start(ledger, variant)
    seed, seed_row = find_seed_row(ledger, world)
    exp = export_time(ledger)
    assert all(_ts(r["ts"]) < exp for r in world["page_log"]), "page log row after export time"
    assert all(_ts(r["ts"]) < exp for r in world["model_log"]), "model log row after export time"
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / "episode.sqlite"
    if db.exists():
        db.unlink()
    # The database is built on local disk and moved into place (SQLite journaling on NFS is slow).
    tmp_dir = Path(tempfile.mkdtemp(prefix="wt_export_", dir="/tmp"))
    tmp_db = tmp_dir / "episode.sqlite"
    con = sqlite3.connect(tmp_db)
    con.executescript(SCHEMA)
    for r in world["page_log"]:
        if _ts(r["ts"]) >= cov:
            con.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?)",
                        (r["id"], r["ts"], r["action"], r["user_id"], r["page_id"], r["revision_id"],
                         int(bool(r["content_changed"])), json.dumps(r["data"])))
    for r in world["model_log"]:
        if _ts(r["ts"]) >= cov:
            con.execute("INSERT INTO model_log VALUES (?,?,?,?,?,?,?)",
                        (r["id"], r["ts"], r["action"], r["user_id"], r["object_type"], r["object_id"], json.dumps(r["data"])))
    uname = {u["id"]: u["username"] for u in world["users"]}
    for u in world["users"]:
        con.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
                    (u["id"], u["username"], u["first_name"], u["last_name"], int(u["is_superuser"]), int(u["is_active"])))
    for g in world["groups"]:
        con.execute("INSERT INTO groups VALUES (?,?)", (g["id"], g["name"]))
        for uid in g["members"]:
            con.execute("INSERT INTO group_members VALUES (?,?)", (g["name"], uname[uid]))
    for p in world["pages"]:
        con.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                    (p["id"], p["title"], p["slug"], p["parent_id"], int(p["live"]), p["live_revision_id"],
                     p["latest_revision_id"], p["first_published_at"], p["last_published_at"]))
    for r in world["revisions"]:
        con.execute("INSERT INTO revisions VALUES (?,?,?)", (r["id"], r["page_id"], r["created_at"]))
    for w in world["workflows"]:
        con.execute("INSERT INTO workflows VALUES (?,?)", (w["id"], w["name"]))
    for t in world["workflow_tasks"]:
        con.execute("INSERT INTO workflow_tasks VALUES (?,?,?,?)", (t["workflow_id"], t["sort_order"], t["task_name"], t["approver_group"]))
    for wp in world["workflow_pages"]:
        con.execute("INSERT INTO workflow_pages VALUES (?,?)", (wp["page_id"], wp["workflow_id"]))
    for src in ("audit_log", "model_log"):
        con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)",
                    (src, cov.isoformat(), exp.isoformat(),
                     "Complete for all recorded actions within the window. Nothing before covered_from is retained."))
    for src in ("users", "groups", "group_members", "pages", "revisions", "workflows", "workflow_tasks", "workflow_pages"):
        con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)",
                    (src, exp.isoformat(), exp.isoformat(), "Snapshot of current state at export time; no history."))
    con.commit()
    con.close()
    shutil.move(str(tmp_db), str(db))
    shutil.rmtree(tmp_dir, ignore_errors=True)
    alert = {
        "case_id": case_id,
        "alert": {
            "audit_log_id": seed_row["id"], "ts": seed_row["ts"], "action": PUBLISH,
            "actor_user_id": seed_row["user_id"], "page_id": seed_row["page_id"], "revision_id": seed_row["revision_id"],
        },
        "question": ("Was this publication authorized under the organization's content publication policy at the "
                     "time it was performed? Answer authorized, unauthorized, or insufficient_evidence, and cite "
                     "the records that establish your answer."),
    }
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=1))
    return ledger, world, seed, seed_row, cov


def _kleene_and(a, b):
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def build_truth(ledger, world, seed, seed_row, cov, variant):
    spec = ledger["spec"]
    base = spec["base"]
    window = timedelta(minutes=spec["window_minutes"])
    t = _ts(seed_row["ts"])
    key = (t, seed_row["id"])
    principal, page_id, revision_id = seed_row["user_id"], seed_row["page_id"], seed_row["revision_id"]
    window_start = t - window
    exp = export_time(ledger)
    rows = world["page_log"]

    def before(r):
        return (_ts(r["ts"]), r["id"]) < key and _ts(r["ts"]) >= window_start

    starts = [r for r in rows if r["action"] == START and r["page_id"] == page_id and before(r)]
    qualifying_a = [r for r in starts if r["user_id"] != principal]
    approves = [r for r in rows if r["action"] == APPROVE and r["revision_id"] == revision_id and before(r)]
    visible_a = [r for r in qualifying_a if _ts(r["ts"]) >= cov]
    visible_b = [r for r in approves if _ts(r["ts"]) >= cov]
    covered = cov <= window_start and exp >= t
    val_a = True if visible_a else (False if covered else None)
    val_b = True if visible_b else (False if covered else None)
    latent_a, latent_b = bool(qualifying_a), bool(approves)

    # Cross-check against the execution ledger (which step did what, by whom, when).
    seed_actor = seed["actor_user_id"]
    ledger_a = any(s["kind"] == "submit" and s.get("ok") and s.get("page_id") == page_id and s["actor_user_id"] != seed_actor
                   and window_start <= _ts(step_ts(ledger, s)) < t for s in ledger["steps"])
    assert ledger_a == latent_a, ("ledger/dump disagree on condition (a)", spec["world_id"])
    if seed["kind"] == "approve":
        appr = [r for r in seed["rows"] if r["action"] == APPROVE]
        ledger_b = bool(appr) and appr[0]["revision_id"] == revision_id
    else:
        ledger_b = False
    assert ledger_b == latent_b, ("ledger/dump disagree on condition (b)", spec["world_id"])

    verdict = _kleene_and(val_a, val_b)
    latent = "authorized" if (latent_a and latent_b) else "unauthorized"
    answerable = verdict is not None
    if answerable:
        assert ("authorized" if verdict else "unauthorized") == latent, ("projection contradicts latent truth", spec["world_id"])
    if verdict is True:
        basis = "qualifying submission by another user and approval of the published revision both visible"
    elif verdict is False:
        basis = ("audit log covers the whole 48 h window: " +
                 ("no submission by another user" if val_a is False else "") +
                 (" and " if val_a is False and val_b is False else "") +
                 ("no approval of the published revision" if val_b is False else ""))
    else:
        basis = ("audit log does not cover the whole 48 h window and the decisive records are not visible: " +
                 ("submission by another user not visible" if val_a is None else "") +
                 (", " if val_a is None and val_b is None else "") +
                 ("approval of the published revision not visible" if val_b is None else ""))
    coverage_item = {"source": "source_coverage", "for": "audit_log"}
    alternatives = []
    if verdict is True:
        for sa in visible_a:
            for sb in visible_b:
                alternatives.append([{"source": "audit_log", "id": sa["id"]}, {"source": "audit_log", "id": sb["id"]}])
    elif verdict is False:
        alternatives.append([coverage_item])
    submission = next((s for s in ledger["steps"] if s.get("focal") == "submission"), None)
    return {
        "variant": variant, "world_id": spec["world_id"], "base_id": base["base_id"], "pattern": base["pattern"],
        "split": base["split"], "world_kind": spec["world_kind"],
        "latent_truth": latent, "submitted_by_other_in_window": latent_a, "published_revision_approved_in_window": latent_b,
        "projected_condition_a": val_a, "projected_condition_b": val_b,
        "answerable": answerable, "answerability_basis": basis,
        "expected_verdict": (latent if answerable else "insufficient_evidence"),
        "seed_row_id": seed_row["id"], "publisher_user_id": principal, "page_id": page_id, "revision_id": revision_id,
        "window_start": window_start.isoformat(), "window_covered": covered,
        "focal_submitter_user_id": submission["actor_user_id"] if submission else None,
        "qualifying_submission_rows": [r["id"] for r in qualifying_a], "visible_submission_rows": [r["id"] for r in visible_a],
        "qualifying_approval_rows": [r["id"] for r in approves], "visible_approval_rows": [r["id"] for r in visible_b],
        "proof_alternatives": alternatives,
        "coverage_from": cov.isoformat(), "export_time": exp.isoformat(),
    }
