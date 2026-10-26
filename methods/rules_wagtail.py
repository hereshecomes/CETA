"""Expert rules R for the Wagtail content publication policy: event-time, coverage-qualified evaluation
written by hand from the public documents (audit_rules/wagtail/) only, before any test case of the
application was inspected. Uses the shared SQLTool and budget.

Flags give the weak-rule variants: closed-world negation (absence is always informative), no time limit
(any earlier submission/approval counts) and no separation of duty (the publisher's own submission
counts).
"""
from datetime import datetime, timedelta, timezone

START, APPROVE, PUBLISH = "wagtail.workflow.start", "wagtail.workflow.approve", "wagtail.publish"
WINDOW = timedelta(hours=48)


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


def investigate(tool, closed_world=False, no_time_limit=False, no_actor_check=False):
    a = tool.alert["alert"]
    out = {"verdict": "insufficient_evidence", "evidence": [], "principal": None, "unresolved": []}
    seed = tool.query("SELECT * FROM audit_log WHERE id = ?", (a["audit_log_id"],))["rows"]
    if not seed:
        out["unresolved"].append("seed operation not found")
        return out
    seed = seed[0]
    if seed["action"] != PUBLISH:
        out["unresolved"].append("seed operation is not a publication")
        return out
    t = _ts(seed["ts"])
    key = (seed["ts"], seed["id"])
    principal, page_id, revision_id = seed["user_id"], seed["page_id"], seed["revision_id"]
    out["principal"] = principal
    out["evidence"].append({"source": "audit_log", "id": seed["id"], "role": "context"})
    cov = {r["source"]: (r["covered_from"], r["covered_to"])
           for r in tool.query("SELECT source, covered_from, covered_to FROM source_coverage")["rows"]}
    window_start = t - WINDOW
    complete = closed_world or _covers(cov, "audit_log", window_start, t)
    cov_item = {"source": "source_coverage", "for": "audit_log", "role": "supports_violation"}

    def in_window(r):
        return (r["ts"], r["id"]) < key and (no_time_limit or _ts(r["ts"]) >= window_start)

    # (a) submission for moderation by someone other than the publisher within the window
    starts = tool.query("SELECT id, ts, user_id FROM audit_log WHERE action = ? AND page_id = ? ORDER BY ts, id",
                        (START, page_id))["rows"]
    qual_a = [r for r in starts if in_window(r) and (no_actor_check or r["user_id"] != principal)]
    if qual_a:
        cond_a = True
        out["evidence"].append({"source": "audit_log", "id": qual_a[-1]["id"], "role": "supports_authorization"})
    elif complete and not no_time_limit:
        cond_a = False
        out["evidence"].append(cov_item)
    elif closed_world:
        cond_a = False
        out["evidence"].append(cov_item)
    else:
        cond_a = None
        out["unresolved"].append("submission by another user in the 48 h window not determinable")

    # (b) approval of the published revision within the window
    approves = tool.query("SELECT id, ts, user_id FROM audit_log WHERE action = ? AND revision_id = ? ORDER BY ts, id",
                          (APPROVE, revision_id))["rows"]
    qual_b = [r for r in approves if in_window(r)]
    if qual_b:
        cond_b = True
        out["evidence"].append({"source": "audit_log", "id": qual_b[-1]["id"], "role": "supports_authorization"})
    elif complete and not no_time_limit:
        cond_b = False
        if cov_item not in out["evidence"]:
            out["evidence"].append(cov_item)
    elif closed_world:
        cond_b = False
        if cov_item not in out["evidence"]:
            out["evidence"].append(cov_item)
    else:
        cond_b = None
        out["unresolved"].append("approval of the published revision in the 48 h window not determinable")

    # Three-valued combination (policy P2): unauthorized if either requirement is shown false.
    if cond_a is False or cond_b is False:
        out["verdict"] = "unauthorized"
    elif cond_a is True and cond_b is True:
        out["verdict"] = "authorized"
    return out
