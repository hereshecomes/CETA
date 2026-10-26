"""Expert rules R for the Weblate project-token policy: event-time, coverage-qualified evaluation written
by hand from the public documents (audit_rules/weblate/) only, before any test case of the application
was inspected. Uses the shared SQLTool and budget.

Flags give the two weak-rule variants: closed-world negation and current-state-only membership.
"""
from datetime import datetime, timezone

ADD, REMOVE = "team-add", "team-remove"


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


def investigate(tool, closed_world=False, current_state_only=False):
    a = tool.alert["alert"]
    out = {"verdict": "insufficient_evidence", "evidence": [], "principal": None, "unresolved": []}
    seed = tool.query("SELECT * FROM audit_log WHERE id = ?", (a["audit_log_id"],))["rows"]
    if not seed:
        out["unresolved"].append("seed operation not found")
        return out
    seed = seed[0]
    t = _ts(seed["ts"])
    out["evidence"].append({"source": "audit_log", "id": seed["id"], "role": "context"})
    tok = tool.query("SELECT * FROM tokens WHERE user_id = ?", (seed["user_id"],))["rows"]
    if not tok:
        out["unresolved"].append("seed operation is not a project-token action")
        return out
    team = tok[0]["team"]
    cov = {r["source"]: (r["covered_from"], r["covered_to"])
           for r in tool.query("SELECT source, covered_from, covered_to FROM source_coverage")["rows"]}

    # (a) token validity at t
    val = tool.query("SELECT * FROM token_validity WHERE user_id = ?", (seed["user_id"],))["rows"]
    valid = None
    if val:
        f, e = _ts(val[0]["valid_from"]), _ts(val[0]["valid_to"])
        valid = f <= t and (e is None or t <= e)
        out["evidence"].append({"source": "token_validity", "id": seed["user_id"],
                                "role": "supports_authorization" if valid else "supports_violation"})
        if not valid and not closed_world and not _covers(cov, "token_validity", t, t):
            valid = None  # the listed period does not cover t but the table is not complete for t
    elif closed_world:
        valid = False
    if valid is None:
        out["unresolved"].append("token validity at event time not determinable")

    # (b) creator's membership in the token's team at t
    created = tool.query(
        "SELECT id, ts, actor_username FROM account_log WHERE activity = 'token-created' AND user_id = ? ORDER BY ts, id",
        (seed["user_id"],))["rows"]
    member = None
    if not created or created[0]["actor_username"] is None:
        if closed_world:
            member = False
        out["unresolved"].append("creator: token creation record not in account-log coverage")
    else:
        creator = created[0]["actor_username"]
        out["principal"] = creator
        out["evidence"].append({"source": "account_log", "id": created[0]["id"], "role": "context"})
        if current_state_only:
            cur = tool.query("SELECT 1 FROM team_members WHERE team = ? AND username = ?", (team, creator))["rows"]
            member = bool(cur)
            out["evidence"].append({"source": "team_members", "team": team, "username": creator, "present": member})
        else:
            changes = tool.query(
                "SELECT id, ts, activity FROM account_log WHERE username = ? AND team = ? AND activity IN (?, ?) "
                "ORDER BY ts, id", (creator, team, ADD, REMOVE))["rows"]
            key = (seed["ts"], seed["id"])
            before = [c for c in changes if (c["ts"], c["id"]) < key]
            after = [c for c in changes if (c["ts"], c["id"]) > key]
            if before:
                member = before[-1]["activity"] == ADD
                out["evidence"].append({"source": "account_log", "id": before[-1]["id"],
                                        "role": "supports_authorization" if member else "supports_violation"})
            elif after:
                member = after[0]["activity"] == REMOVE
                out["evidence"].append({"source": "account_log", "id": after[0]["id"],
                                        "role": "supports_authorization" if member else "supports_violation"})
            else:
                cur = tool.query("SELECT 1 FROM team_members WHERE team = ? AND username = ?", (team, creator))["rows"]
                member = bool(cur)
                out["evidence"].append({"source": "team_members", "team": team, "username": creator, "present": member})

    # Three-valued combination (policy P2): unauthorized if either requirement is shown false.
    if valid is False or member is False:
        out["verdict"] = "unauthorized"
    elif valid is True and member is True:
        out["verdict"] = "authorized"
    return out
