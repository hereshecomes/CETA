"""Project an executed world into a public investigation view and a private truth record.

Public view (episode.sqlite + alert.json) is what investigators see.
Private truth is computed from the execution ledger and world state, never from the
investigator-facing audit log alone, and is never shown to investigators.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

VARIANTS = ("V0", "V1a", "V1b")
MEMBER_ACTIONS = {"pretix.team.member.added": "added", "pretix.team.member.removed": "removed"}
SEED_ACTIONS = {"mark_paid": "pretix.event.order.paid", "extend": "pretix.event.order.expirychanged"}

SCHEMA = """
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, action TEXT, actor_user_id INTEGER,
  actor_token_id INTEGER, event TEXT, object_type TEXT, object_id INTEGER, data TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);
CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, all_events INTEGER, limit_events TEXT,
  all_event_permissions INTEGER, event_permissions TEXT, all_organizer_permissions INTEGER,
  organizer_permissions TEXT);
CREATE TABLE team_members (team_id INTEGER, user_id INTEGER, PRIMARY KEY (team_id, user_id));
CREATE TABLE team_tokens (id INTEGER PRIMARY KEY, team_id INTEGER, name TEXT, active INTEGER);
CREATE TABLE events (id INTEGER PRIMARY KEY, slug TEXT, name TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, code TEXT, event TEXT, status TEXT, email TEXT, expires TEXT);
CREATE TABLE source_coverage (source TEXT PRIMARY KEY, covered_from TEXT, covered_to TEXT, note TEXT);
"""


def _ts(s):
    return datetime.fromisoformat(s)


def step_ts(ledger, step):
    """Scenario time of a ledger step (T0 + minute offset), as ISO string."""
    return (_ts(ledger["T0"]) + timedelta(minutes=step["minute"])).isoformat()


def _public_data(row):
    """Drop rendered e-mail bodies (long, irrelevant text); keep everything else verbatim."""
    if row["action"].startswith("pretix.event.order.email."):
        d = json.loads(row["data"])
        return json.dumps({k: d[k] for k in ("subject",) if k in d})
    return row["data"]


def coverage_start(ledger, variant):
    T0 = _ts(ledger["T0"])
    base = ledger["spec"]["base"]
    if variant == "V0":
        return T0 - timedelta(minutes=30)
    if variant == "V1a":
        return T0 + timedelta(minutes=base["t_c"] - 20, seconds=30)
    if variant == "V1b":
        return T0 + timedelta(minutes=base["cutoff_b"], seconds=30)
    raise ValueError(variant)


def find_seed_row(ledger, world):
    seed = next(s for s in ledger["steps"] if s.get("focal") == "seed")
    order_id = next(o["id"] for o in world["orders"] if o["code"] == seed["order_code"])
    rows = [r for r in world["audit_log"] if r["action"] == SEED_ACTIONS[seed["kind"]]
            and r["object_id"] == order_id and r["actor_token_id"] == seed["token_id"]]
    assert len(rows) == 1, ("seed row not unique", len(rows))
    return seed, rows[0]


def build_public(world_dir, variant, case_id, out_dir):
    ledger = json.loads((world_dir / "ledger.json").read_text())
    world = json.loads((world_dir / "world_full.json").read_text())
    cov = coverage_start(ledger, variant)
    seed, seed_row = find_seed_row(ledger, world)
    export_time = _ts(ledger["T0"]) + timedelta(hours=24)  # fixed per base event, after every step
    assert all(_ts(r["ts"]) < export_time for r in world["audit_log"])
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / "episode.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    for r in world["audit_log"]:
        if _ts(r["ts"]) >= cov:
            con.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["ts"], r["action"], r["actor_user_id"], r["actor_token_id"], r["event"],
                         r["object_type"], r["object_id"], _public_data(r)))
    for u in world["users"]:
        con.execute("INSERT INTO users VALUES (?,?)", (u["id"], u["email"]))
    for t in world["teams"]:
        con.execute("INSERT INTO teams VALUES (?,?,?,?,?,?,?,?)",
                    (t["id"], t["name"], int(t["all_events"]), json.dumps(t["limit_events"]),
                     int(t["all_event_permissions"]), json.dumps(t["event_permissions"]),
                     int(t["all_organizer_permissions"]), json.dumps(t["organizer_permissions"])))
        for m in t["members"]:
            con.execute("INSERT INTO team_members VALUES (?,?)", (t["id"], m))
    for tk in world["tokens"]:
        con.execute("INSERT INTO team_tokens VALUES (?,?,?,?)", (tk["id"], tk["team_id"], tk["name"], int(tk["active"])))
    for e in world["events"]:
        con.execute("INSERT INTO events VALUES (?,?,?)", (e["id"], e["slug"], e["name"]))
    for o in world["orders"]:
        con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)",
                    (o["id"], o["code"], o["event"], o["status"], o["email"], o["expires"]))
    con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)",
                ("audit_log", cov.isoformat(), export_time.isoformat(),
                 "Complete for all audited actions within the window. Nothing before covered_from is retained. "
                 "Team state at covered_from is not itself recorded."))
    for src in ("users", "teams", "team_members", "team_tokens", "events", "orders"):
        con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)",
                    (src, export_time.isoformat(), export_time.isoformat(),
                     "Snapshot of current state at export time; no history."))
    con.commit()
    con.close()
    order = next(o for o in world["orders"] if o["id"] == seed_row["object_id"])
    alert = {
        "case_id": case_id,
        "alert": {
            "audit_log_id": seed_row["id"], "ts": seed_row["ts"], "action": seed_row["action"],
            "actor_token_id": seed_row["actor_token_id"], "event": seed_row["event"],
            "order_code": order["code"],
        },
        "question": ("Was this operation authorized under the organization's API token policy at the time "
                     "it was performed? Answer authorized, unauthorized, or insufficient_evidence, and cite "
                     "the records that establish your answer."),
    }
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=1))
    return ledger, world, seed, seed_row, cov


def membership_events(ledger, world, user_role, team_key):
    """Ledger-derived membership changes for (user, team), each matched to its audit row."""
    uid = ledger["user_ids"][user_role]
    tid = ledger["team_ids"][team_key]
    out = []
    for s in ledger["steps"]:
        if s["kind"] in ("member_add", "member_remove") and s["user"] == user_role and s["team"] == team_key and s["ok"]:
            action = "pretix.team.member.added" if s["kind"] == "member_add" else "pretix.team.member.removed"
            rows = [r for r in world["audit_log"] if r["action"] == action and r["object_id"] == tid
                    and r["ts"] == step_ts(ledger, s) and json.loads(r["data"]).get("user") == uid]
            assert len(rows) == 1
            out.append({"ts": step_ts(ledger, s), "kind": MEMBER_ACTIONS[action], "row_id": rows[0]["id"]})
    return out


def build_truth(ledger, world, seed, seed_row, cov, variant):
    spec = ledger["spec"]
    creation = next(s for s in ledger["steps"] if s["kind"] == "token_create" and s["token"] == seed["token"])
    issuer_role = creation["actor"]
    uid = ledger["user_ids"][issuer_role]
    team_key = creation["team"]
    tid = ledger["team_ids"][team_key]
    creation_row = next(r for r in world["audit_log"] if r["action"] == "pretix.team.token.created"
                        and json.loads(r["data"]).get("id") == seed["token_id"])
    events = membership_events(ledger, world, issuer_role, team_key)
    t_s = _ts(step_ts(ledger, seed))
    member = issuer_role in spec["base"]["initial"].get(team_key, [])
    for e in events:
        if _ts(e["ts"]) < t_s:
            member = e["kind"] == "added"
    latent = "authorized" if member else "unauthorized"
    creation_visible = _ts(creation_row["ts"]) >= cov
    answerable = creation_visible  # membership at t_s is always recoverable inside the coverage window
    before = [e for e in events if _ts(e["ts"]) < t_s and _ts(e["ts"]) >= cov]
    after = [e for e in events if _ts(e["ts"]) > t_s]
    current_member = uid in next(t["members"] for t in world["teams"] if t["id"] == tid)
    proofs = []
    if before:
        proofs.append({"rule": "R1_last_change_before", "row_ids": [before[-1]["row_id"]]})
    if after:
        proofs.append({"rule": "R2_first_change_after", "row_ids": [after[0]["row_id"]]})
    else:
        proofs.append({"rule": "R3_current_state_no_later_change",
                       "current_membership": {"team_id": tid, "user_id": uid, "member": current_member}})
    return {
        "variant": variant, "world_id": spec["world_id"], "base_id": spec["base"]["base_id"],
        "pattern": spec["base"]["pattern"], "split": spec["base"]["split"], "world_kind": spec["world_kind"],
        "latent_truth": latent,
        "answerable": answerable,
        "answerability_basis": ("token creation record inside audit coverage" if answerable
                                else "token creation record precedes audit coverage; no other public source binds token to issuer"),
        "expected_verdict": latent if answerable else "insufficient_evidence",
        "seed_row_id": seed_row["id"], "token_id": seed["token_id"], "token_team_id": tid,
        "issuer_user_id": uid, "creation_row_id": creation_row["id"] if creation_visible else None,
        "membership_events": events, "member_at_event_time": member,
        "membership_proofs": proofs, "coverage_from": cov.isoformat(),
    }
