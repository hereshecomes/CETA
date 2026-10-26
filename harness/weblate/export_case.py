"""Project an executed Weblate world into a public investigation view and a private truth record.

Public view (episode.sqlite + alert.json) is what investigators see. Private truth is computed from
the execution ledger and the world state dump, never from the investigator-facing logs alone, and is
never shown to investigators.

Public tables (see audit_rules/weblate/schema.md):
  audit_log      project history (Weblate Change rows of the project), inside the coverage window
  account_log    user account log (Weblate AuditLog rows), inside the coverage window; the fixed
                 parameter keys team / project / username(actor) are also given as columns
  users, teams, team_members, tokens, token_validity, units   current state at export time
  source_coverage
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

VARIANTS = ("V0", "V1a", "V1b")
ACTION_NAMES = {0: 'update', 1: 'complete', 2: 'change', 3: 'comment', 4: 'suggestion', 5: 'new', 6: 'auto', 7: 'accept', 8: 'revert', 9: 'upload', 13: 'new_source', 14: 'lock', 15: 'unlock', 17: 'commit', 18: 'push', 19: 'reset', 20: 'merge', 21: 'rebase', 22: 'failed_merge', 23: 'failed_rebase', 24: 'parse_error', 25: 'remove_translation', 26: 'suggestion_delete', 27: 'replace', 28: 'failed_push', 29: 'suggestion_cleanup', 30: 'source_change', 31: 'new_unit', 32: 'bulk_edit', 33: 'access_edit', 34: 'add_user', 35: 'remove_user', 36: 'approve', 37: 'marked_edit', 38: 'remove_component', 39: 'remove_project', 41: 'rename_project', 42: 'rename_component', 43: 'move_component', 45: 'new_contributor', 46: 'announcement', 47: 'alert', 48: 'added_language', 49: 'requested_language', 50: 'create_project', 51: 'create_component', 52: 'invite_user', 53: 'hook', 54: 'replace_upload', 55: 'license_change', 56: 'agreement_change', 57: 'screenshot_added', 58: 'screenshot_uploaded', 59: 'string_repo_update', 60: 'addon_create', 61: 'addon_change', 62: 'addon_remove', 63: 'string_remove', 64: 'comment_delete', 65: 'comment_resolve', 66: 'explanation', 67: 'remove_category', 68: 'rename_category', 69: 'move_category', 70: 'save_failed', 71: 'new_unit_repo', 72: 'string_upload_update', 73: 'new_unit_upload', 74: 'source_upload', 75: 'completed_component', 76: 'enforced_check', 77: 'propagated_edit', 78: 'file_upload', 79: 'extra_flags', 80: 'font_create', 81: 'font_change', 82: 'font_remove', 83: 'force_sync', 84: 'force_scan', 85: 'screenshot_removed', 86: 'label_add', 87: 'label_remove', 88: 'repo_cleanup', 89: 'new_source_upload', 90: 'new_source_repo', 91: 'project_backup', 92: 'project_restore', 93: 'component_restore', 94: 'user_revert', 95: 'project_setting_change', 96: 'component_setting_change', 97: 'user_access_change', 98: 'create_workspace', 99: 'workspace_setting_change', 100: 'move_project', 101: 'remote_update', 102: 'failed_remote_update', 103: 'alert_dismissed', 104: 'alert_reopened'}  # from weblate/trans/actions.py (ActionEvents), lower-cased
SEED_ACTIONS = {"change", "new"}

SCHEMA = """
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts TEXT, action INTEGER, action_name TEXT, user_id INTEGER,
  author_id INTEGER, project_id INTEGER, component_id INTEGER, translation_id INTEGER, unit_id INTEGER,
  target TEXT, details TEXT);
CREATE TABLE account_log (id INTEGER PRIMARY KEY, ts TEXT, user_id INTEGER, username TEXT, activity TEXT,
  actor_username TEXT, team TEXT, project TEXT, params TEXT);
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, is_bot INTEGER, is_active INTEGER,
  is_superuser INTEGER, date_joined TEXT);
CREATE TABLE teams (id INTEGER PRIMARY KEY, name TEXT, project_id INTEGER);
CREATE TABLE team_members (team TEXT, username TEXT, PRIMARY KEY (team, username));
CREATE TABLE tokens (user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, team TEXT, is_active INTEGER,
  created_at TEXT);
CREATE TABLE token_validity (user_id INTEGER PRIMARY KEY, valid_from TEXT, valid_to TEXT);
CREATE TABLE projects (id INTEGER PRIMARY KEY, slug TEXT, name TEXT);
CREATE TABLE units (id INTEGER PRIMARY KEY, translation_id INTEGER, language TEXT, source TEXT);
CREATE TABLE source_coverage (source TEXT PRIMARY KEY, covered_from TEXT, covered_to TEXT, note TEXT);
"""


def _ts(s):
    return datetime.fromisoformat(s)


def _internal(u):
    """Weblate-internal accounts (the anonymous user and scope:name service bots) are not organisation accounts."""
    return u["username"] == "anonymous" or ":" in u["username"]


def step_ts(ledger, step):
    return (_ts(ledger["T0"]) + timedelta(minutes=step["minute"])).isoformat()


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


def export_time(ledger):
    return _ts(ledger["T0"]) + timedelta(minutes=ledger["spec"]["export_minute"])


def last_cleanup_before(ledger, t):
    T0 = _ts(ledger["T0"])
    runs = [T0 + timedelta(minutes=m) for m in ledger["spec"]["cleanup_minutes"]]
    runs = [r for r in runs if r <= t]
    return max(runs) if runs else None


def find_seed_row(ledger, world):
    seed = next(s for s in ledger["steps"] if s.get("focal") == "seed")
    rows = [r for r in world["changes"] if ACTION_NAMES.get(r["action"]) in SEED_ACTIONS
            and r["unit_id"] == seed["unit_id"] and r["user_id"] == seed["token_id"] and r["ts"] == step_ts(ledger, seed)]
    assert len(rows) == 1, ("seed row not unique", len(rows))
    return seed, rows[0]


def build_public(world_dir, variant, case_id, out_dir):
    ledger = json.loads((world_dir / "ledger.json").read_text())
    world = json.loads((world_dir / "world_full.json").read_text())
    cov = coverage_start(ledger, variant)
    seed, seed_row = find_seed_row(ledger, world)
    exp = export_time(ledger)
    assert all(_ts(r["ts"]) < exp for r in world["changes"]), "change after export time"
    assert all(_ts(r["ts"]) < exp for r in world["account_log"]), "account log row after export time"
    out_dir.mkdir(parents=True, exist_ok=True)
    db = out_dir / "episode.sqlite"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    world = dict(world, users=[u for u in world["users"] if not _internal(u)])
    uname = {u["id"]: u["username"] for u in world["users"]}
    for r in world["changes"]:
        if _ts(r["ts"]) >= cov:
            con.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["id"], r["ts"], r["action"], ACTION_NAMES.get(r["action"], str(r["action"])), r["user_id"],
                         r["author_id"], r["project_id"], r["component_id"], r["translation_id"], r["unit_id"],
                         r["target"], json.dumps(r["details"])))
    for a in world["account_log"]:
        if _ts(a["ts"]) >= cov:
            p = a["params"] or {}
            con.execute("INSERT INTO account_log VALUES (?,?,?,?,?,?,?,?,?)",
                        (a["id"], a["ts"], a["user_id"], uname.get(a["user_id"]), a["activity"],
                         p.get("username"), p.get("team"), p.get("project"), json.dumps(p)))
    for u in world["users"]:
        con.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?)",
                    (u["id"], u["username"], u["full_name"], int(u["is_bot"]), int(u["is_active"]),
                     int(u["is_superuser"]), u["date_joined"]))
    team_of_user = {}
    for t in world["teams"]:
        con.execute("INSERT INTO teams VALUES (?,?,?)", (t["id"], t["name"], t["project_id"]))
        for uid in t["members"]:
            if uid not in uname:
                continue
            con.execute("INSERT INTO team_members VALUES (?,?)", (t["name"], uname[uid]))
            team_of_user.setdefault(uid, t["name"])
    for u in world["users"]:
        if u["is_bot"]:
            con.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?)",
                        (u["id"], u["username"], u["full_name"], team_of_user.get(u["id"]), int(u["is_active"]),
                         u["date_joined"]))
            if u["is_active"]:
                con.execute("INSERT INTO token_validity VALUES (?,?,?)", (u["id"], u["date_joined"], u["date_expires"]))
    p = world["project"]
    con.execute("INSERT INTO projects VALUES (?,?,?)", (p["id"], p["slug"], p["name"]))
    for u in world["units"]:
        con.execute("INSERT INTO units VALUES (?,?,?,?)", (u["id"], u["translation_id"], u["language"], u["source"]))
    for src in ("audit_log", "account_log"):
        con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)",
                    (src, cov.isoformat(), exp.isoformat(),
                     "Complete for all recorded activity within the window. Nothing before covered_from is "
                     "retained. Membership and token state at covered_from is not itself recorded."))
    c_last = last_cleanup_before(ledger, exp)
    con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)",
                ("token_validity", (c_last or exp).isoformat(), exp.isoformat(),
                 "Validity periods of the project tokens that are active at export time. Complete from the last "
                 "run of the hourly expiry cleanup before the export: a token disabled by an earlier run is not "
                 "listed and its expiry date is no longer stored."))
    for src in ("users", "teams", "team_members", "tokens", "projects", "units"):
        con.execute("INSERT INTO source_coverage VALUES (?,?,?,?)",
                    (src, exp.isoformat(), exp.isoformat(), "Snapshot of current state at export time; no history."))
    con.commit()
    con.close()
    alert = {
        "case_id": case_id,
        "alert": {
            "audit_log_id": seed_row["id"], "ts": seed_row["ts"],
            "action": ACTION_NAMES.get(seed_row["action"]), "actor_user_id": seed_row["user_id"],
            "project_id": seed_row["project_id"], "unit_id": seed_row["unit_id"],
        },
        "question": ("Was this operation authorized under the organization's project token policy at the time "
                     "it was performed? Answer authorized, unauthorized, or insufficient_evidence, and cite "
                     "the records that establish your answer."),
    }
    (out_dir / "alert.json").write_text(json.dumps(alert, indent=1))
    return ledger, world, seed, seed_row, cov


def membership_events(ledger, world, user_role, team):
    """Ledger-derived membership changes for (user, team), each matched to its account-log row."""
    uid = ledger["user_ids"][user_role]
    out = []
    for s in ledger["steps"]:
        if not s.get("ok"):
            continue
        if s["kind"] in ("team_add", "team_remove") and s["user"] == user_role and s["team"] == team:
            kinds = [("team-add", "added")] if s["kind"] == "team_add" else [("team-remove", "removed")]
        elif s["kind"] == "project_remove" and s["user"] == user_role:
            kinds = [("team-remove", "removed")]
        else:
            continue
        for activity, kind in kinds:
            rows = [r for r in world["account_log"] if r["activity"] == activity and r["user_id"] == uid
                    and r["ts"] == step_ts(ledger, s) and (r["params"] or {}).get("team") == team]
            if s["kind"] == "project_remove" and not rows:
                continue  # the user was not in this team; the project removal did not touch it
            assert len(rows) == 1, ("membership row not unique", s, len(rows))
            out.append({"ts": step_ts(ledger, s), "kind": kind, "row_id": rows[0]["id"]})
    return out


def build_truth(ledger, world, seed, seed_row, cov, variant):
    spec = ledger["spec"]
    base = spec["base"]
    creation = next(s for s in ledger["steps"] if s["kind"] == "token_create" and s["token"] == seed["token"])
    creator_role = creation["actor"]
    creator_username = ledger["usernames"][creator_role]
    bot_id = seed["token_id"]
    bot = next(u for u in world["users"] if u["id"] == bot_id)
    team = next(t["name"] for t in world["teams"] if bot_id in t["members"]) if bot["is_active"] else "Administration"
    creation_row = next(r for r in world["account_log"] if r["activity"] == "token-created" and r["user_id"] == bot_id)
    t_s = _ts(step_ts(ledger, seed))
    exp = export_time(ledger)
    # Condition A: token validity at t_s (from the ledger: the expiry recorded at creation).
    expires = _ts(creation["expires_iso"]) if creation.get("expires_iso") else None
    valid = expires is None or t_s <= expires
    # Condition B: creator's membership in the token's team at t_s (from the ledger).
    events = membership_events(ledger, world, creator_role, team)
    member = creator_role in base["initial"].get(team, [])
    for e in events:
        if _ts(e["ts"]) < t_s:
            member = e["kind"] == "added"
    latent = "authorized" if (valid and member) else "unauthorized"
    # Answerability under the public projection, by the policy's own three-valued reading:
    # the verdict is determined if both conditions are determined, or if one is determined false.
    validity_known = bool(bot["is_active"]) and (valid or last_cleanup_before(ledger, exp) is None
                                                 or t_s >= last_cleanup_before(ledger, exp))
    binding_visible = _ts(creation_row["ts"]) >= cov
    membership_known = binding_visible  # membership at t_s is recoverable inside the coverage window
    if validity_known and not valid:
        answerable, basis = True, "token validity record shows the token expired before the operation"
    elif binding_visible and validity_known:
        answerable, basis = True, "token creation record inside account-log coverage; validity record present"
    elif not binding_visible:
        answerable, basis = False, "token creation record precedes account-log coverage; no other public source binds the token to its creator"
    else:
        answerable, basis = False, "token validity not determinable from the public records"
    before = [e for e in events if _ts(e["ts"]) < t_s and _ts(e["ts"]) >= cov]
    after = [e for e in events if _ts(e["ts"]) > t_s]
    current_member = creator_username in [
        m for t in world["teams"] if t["name"] == team for m in ({u["id"]: u["username"] for u in world["users"]}[x] for x in t["members"])]
    membership_proofs = []
    if before:
        membership_proofs.append({"rule": "R1_last_change_before", "row_ids": [before[-1]["row_id"]]})
    if after:
        membership_proofs.append({"rule": "R2_first_change_after", "row_ids": [after[0]["row_id"]]})
    else:
        membership_proofs.append({"rule": "R3_current_state_no_later_change",
                                  "current_membership": {"team": team, "username": creator_username, "present": current_member}})
    # Required evidence (alternatives): each alternative is a list of items that must all be cited.
    validity_item = {"source": "token_validity", "id": bot_id}
    binding_item = {"source": "account_log", "id": creation_row["id"]}
    alternatives = []
    if answerable:
        if not valid:
            # An expired token is established either by its listed validity period (valid_to < t) or,
            # for an absence-based conclusion, by the completeness record of token_validity for t
            # (policy P6: absence is informative inside the coverage window).
            alternatives.append([validity_item])
            alternatives.append([{"source": "source_coverage", "for": "token_validity"}])
        else:
            for p in membership_proofs:
                if "row_ids" in p:
                    alternatives.append([binding_item, validity_item] + [{"source": "account_log", "id": i} for i in p["row_ids"]])
                else:
                    cm = p["current_membership"]
                    alternatives.append([binding_item, validity_item,
                                         {"source": "team_members", "team": cm["team"], "username": cm["username"],
                                          "present": cm["present"]}])
    return {
        "variant": variant, "world_id": spec["world_id"], "base_id": base["base_id"], "pattern": base["pattern"],
        "split": base["split"], "world_kind": spec["world_kind"],
        "latent_truth": latent, "token_valid_at_event_time": valid, "member_at_event_time": member,
        "answerable": answerable, "answerability_basis": basis,
        "expected_verdict": latent if answerable else "insufficient_evidence",
        "seed_row_id": seed_row["id"], "token_id": bot_id, "token_team": team,
        "creator_username": creator_username, "creator_user_id": ledger["user_ids"][creator_role],
        "creation_row_id": creation_row["id"] if binding_visible else None,
        "token_expires": expires.isoformat() if expires else None,
        "membership_events": events, "membership_proofs": membership_proofs,
        "proof_alternatives": alternatives,
        "coverage_from": cov.isoformat(), "export_time": exp.isoformat(),
    }
