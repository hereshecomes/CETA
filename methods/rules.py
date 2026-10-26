"""Expert rules R for pretix: event-time, coverage-qualified evaluation of the public token policy.

Written by hand from the public documents (audit_rules/pretix/) only, before any test case was inspected,
under the evaluator's semantics. Uses the shared SQLTool and the same budget as every other method. The
flags give the two weak-rule variants: closed-world negation and current-state-only membership.
"""

ADDED, REMOVED = "pretix.team.member.added", "pretix.team.member.removed"


def investigate(tool, closed_world=False, current_state_only=False):
    a = tool.alert["alert"]
    out = {"verdict": "insufficient_evidence", "evidence": [], "issuer_user_id": None, "unresolved": []}
    seed = tool.query("SELECT * FROM audit_log WHERE id = ?", (a["audit_log_id"],))["rows"]
    if not seed or seed[0]["actor_token_id"] is None:
        out["unresolved"].append("seed operation is not a team-token action")
        return out
    seed = seed[0]
    out["evidence"].append({"source": "audit_log", "id": seed["id"], "role": "context"})
    tok = tool.query("SELECT * FROM team_tokens WHERE id = ?", (seed["actor_token_id"],))["rows"]
    if not tok:
        out["unresolved"].append("token team unknown")
        return out
    team_id = tok[0]["team_id"]
    created = tool.query(
        "SELECT id, ts, actor_user_id FROM audit_log WHERE action = 'pretix.team.token.created' "
        "AND object_type = 'team' AND object_id = ? AND json_extract(data, '$.id') = ? ORDER BY id",
        (team_id, seed["actor_token_id"]))["rows"]
    if not created or created[0]["actor_user_id"] is None:
        if closed_world:
            out["verdict"] = "unauthorized"
        out["unresolved"].append("issuer: token creation record not in audit coverage")
        return out
    issuer = created[0]["actor_user_id"]
    out["issuer_user_id"] = issuer
    out["evidence"].append({"source": "audit_log", "id": created[0]["id"], "role": "context"})
    if current_state_only:
        cur = tool.query("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, issuer))["rows"]
        member = bool(cur)
        out["evidence"].append({"source": "team_members", "team_id": team_id, "user_id": issuer, "member": member})
    else:
        changes = tool.query(
            "SELECT id, ts, action FROM audit_log WHERE object_type = 'team' AND object_id = ? "
            "AND action IN (?, ?) AND json_extract(data, '$.user') = ? ORDER BY ts, id",
            (team_id, ADDED, REMOVED, issuer))["rows"]
        key = (seed["ts"], seed["id"])
        before = [c for c in changes if (c["ts"], c["id"]) < key]
        after = [c for c in changes if (c["ts"], c["id"]) > key]
        if before:
            member = before[-1]["action"] == ADDED
            out["evidence"].append({"source": "audit_log", "id": before[-1]["id"],
                                    "role": "supports_authorization" if member else "supports_violation"})
        elif after:
            member = after[0]["action"] == REMOVED
            out["evidence"].append({"source": "audit_log", "id": after[0]["id"],
                                    "role": "supports_authorization" if member else "supports_violation"})
        else:
            cur = tool.query("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?",
                             (team_id, issuer))["rows"]
            member = bool(cur)
            out["evidence"].append({"source": "team_members", "team_id": team_id, "user_id": issuer,
                                    "member": member})
    out["verdict"] = "authorized" if member else "unauthorized"
    return out
