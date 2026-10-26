"""Expert rules R for the Indico event management policy: event-time, coverage-qualified evaluation
written by hand from the public documents (audit_rules/indico/) only, before any test case of the
application was inspected. Uses the shared SQLTool and budget.

Flags give the weak-rule variants: closed-world negation (absence of a team change is always informative)
and current-state-only (team membership read from the current team tables, ignoring the log of ACL
changes).
"""
from datetime import datetime, timezone

ADDED, REMOVED = "ACL entry added", "ACL entry removed"


def _ts(v):
    if v is None:
        return None
    s = str(v)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _covers(cov, source, a, b):
    w = cov.get(source)
    if w is None:
        return False
    f, t = _ts(w[0]), _ts(w[1])
    return (f is None or f <= a) and (t is None or b <= t)


def _team(tool, cov, key, t, log, scope_col, scope, name, team_table, export, closed_world, current_state_only):
    """Three-valued membership of `name` in the team of `scope` at the operation (policy P3/P4).
    Returns (value, evidence items)."""
    if not current_state_only:
        rows = tool.query(f"SELECT id, ts, summary FROM {log} WHERE {scope_col} = ? AND summary IN (?, ?) "
                          f"AND json_extract(data, '$.User') = ? ORDER BY ts, id", (scope, ADDED, REMOVED, name))["rows"]
        before = [r for r in rows if (_ts(r["ts"]), r["id"]) < key]
        after = [r for r in rows if (_ts(r["ts"]), r["id"]) > key]
        if before and (closed_world or _covers(cov, log, _ts(before[-1]["ts"]), t)):
            state = before[-1]["summary"] == ADDED
            return state, [{"source": log, "id": before[-1]["id"],
                            "role": "supports_authorization" if state else "supports_violation"}]
        if after and (closed_world or _covers(cov, log, t, _ts(after[0]["ts"]))):
            state = after[0]["summary"] == REMOVED
            return state, [{"source": log, "id": after[0]["id"],
                            "role": "supports_authorization" if state else "supports_violation"}]
        if after or not (closed_world or _covers(cov, log, t, export)):
            return None, []
    present = bool(tool.query(f"SELECT 1 AS p FROM {team_table} WHERE {scope_col} = ? AND user_name = ?",
                              (scope, name))["rows"])
    return present, [{"source": team_table, scope_col: scope, "user_name": name, "present": present,
                      "role": "supports_authorization" if present else "supports_violation"}]


def investigate(tool, closed_world=False, current_state_only=False):
    a = tool.alert["alert"]
    out = {"verdict": "insufficient_evidence", "evidence": [], "principal": None, "unresolved": []}
    seed = tool.query("SELECT * FROM audit_log WHERE id = ?", (a["audit_log_id"],))["rows"]
    if not seed:
        out["unresolved"].append("audited change not found")
        return out
    seed = seed[0]
    t = _ts(seed["ts"])
    key = (t, seed["id"])
    principal, event_id = seed["user_id"], seed["event_id"]
    out["principal"] = principal
    out["evidence"].append({"source": "audit_log", "id": seed["id"], "role": "context"})
    cov = {r["source"]: (r["covered_from"], r["covered_to"])
           for r in tool.query("SELECT source, covered_from, covered_to FROM source_coverage")["rows"]}
    export = _ts(cov["event_team"][0]) if "event_team" in cov else None
    info = tool.query("SELECT e.category_id AS category_id, u.full_name AS name FROM events e, users u "
                      "WHERE e.id = ? AND u.id = ?", (event_id, principal))["rows"]
    if not info or export is None:
        out["unresolved"].append("event category or actor name not found")
        return out
    category_id, name = info[0]["category_id"], info[0]["name"]
    va, ea = _team(tool, cov, key, t, "audit_log", "event_id", event_id, name, "event_team", export,
                   closed_world, current_state_only)
    vb, eb = _team(tool, cov, key, t, "category_log", "category_id", category_id, name, "category_team", export,
                   closed_world, current_state_only)
    if va is None:
        out["unresolved"].append("event team membership at the time of the change not determinable")
    if vb is None:
        out["unresolved"].append("category team membership at the time of the change not determinable")
    if va is True or vb is True:
        out["verdict"] = "authorized"
        out["evidence"] += ea if va is True else eb
    elif va is False and vb is False:
        out["verdict"] = "unauthorized"
        out["evidence"] += ea + eb
    return out
