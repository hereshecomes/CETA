"""Application profiles of Wagtail and Indico, registered into app_profile.PROFILES at import time.
Every entry has the same shape as the pretix and Weblate entries and was written from the application's
public documents only, before any LLM run on that application.
"""
import app_profile
from app_profile import ROOT

PROFILES_EXT = {
    "wagtail": {
        # Written from the public documents only, before any LLM run on this application. The policy's principal is a column of the operation row
        # (the publisher's user id); evidence items are audit_log rows and the coverage record of audit_log.
        "docs": ROOT / "audit_rules" / "wagtail",
        "cases": ROOT / "cases" / "wagtail",
        "scorer": "score_generic",
        "rules_module": "rules_wagtail",
        "domain": "a content management system",
        "principal_field": "principal",
        "principal_schema": {"type": ["integer", "null"]},
        "evidence_item": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "id": {"type": "integer"},
                "for": {"type": "string"},
                "role": {"enum": ["supports_violation", "supports_authorization", "context"]},
            },
            "required": ["source", "role"],
        },
        "json_example": "json_extract(data, '$.comment')",
        "finish_example": (
            '{"action": "finish", "verdict": "authorized|unauthorized|insufficient_evidence", '
            '"evidence": [{"source": "audit_log", "id": 57, "role": "supports_violation|supports_authorization|context"}, '
            '{"source": "source_coverage", "for": "audit_log", "role": "..."}], '
            '"principal": 5 or null, "unresolved": ["..."]}'),
        "state_semantics": (
            "## Evaluation semantics\\nThese are the same semantics the reference evaluator implements. The "
            "accountable principal is read from the operation record itself. A prior-event requirement (here: a "
            "submission of the page by someone other than the principal; an approval of the published revision) "
            "holds at time t if a record of the required kind on the required object exists before t and not older "
            "than the stated limit (and, where required, by an actor other than the principal); it fails if no such "
            "record exists and the source is complete for the whole period from t minus the limit up to t according "
            "to its coverage window; otherwise it is unknown. The operation is authorized only if every requirement "
            "holds, unauthorized if any requirement fails, and otherwise the answer is insufficient_evidence."),
        "pool_sql": {
            "seed_operation": "SELECT * FROM audit_log WHERE id = {seed}",
            "page_records": ("SELECT * FROM audit_log WHERE page_id = (SELECT page_id FROM audit_log WHERE id = {seed}) "
                             "ORDER BY ts, id"),
            "revision_records": ("SELECT * FROM audit_log WHERE revision_id = (SELECT revision_id FROM audit_log "
                                 "WHERE id = {seed}) ORDER BY ts, id"),
            "page": "SELECT * FROM pages WHERE id = (SELECT page_id FROM audit_log WHERE id = {seed})",
            "page_revisions": ("SELECT * FROM revisions WHERE page_id = (SELECT page_id FROM audit_log WHERE id = {seed}) "
                               "ORDER BY id"),
            "users": "SELECT * FROM users ORDER BY id",
            "group_members": "SELECT * FROM group_members ORDER BY group_name, username",
            "workflow_tasks": "SELECT * FROM workflow_tasks ORDER BY workflow_id, sort_order",
            "source_coverage": "SELECT * FROM source_coverage",
        },
        "pool_audit_keys": ("seed_operation", "page_records", "revision_records"),
        "pool_tables": ["audit_log", "pages", "revisions", "users", "group_members", "workflow_tasks", "source_coverage"],
    },
    "indico": {
        # Written from the public documents only, before any LLM run on this application. The policy's principal is a column of the operation row (the
        # acting user's id); team membership comes from ACL change rows (audit_log for the event team,
        # category_log for the category team) or from the current team tables.
        "docs": ROOT / "audit_rules" / "indico",
        "cases": ROOT / "cases" / "indico",
        "scorer": "score_generic",
        "rules_module": "rules_indico",
        "domain": "an event management system",
        "principal_field": "principal",
        "principal_schema": {"type": ["integer", "null"]},
        "evidence_item": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "id": {"type": "integer"},
                "event_id": {"type": "integer"},
                "category_id": {"type": "integer"},
                "user_name": {"type": "string"},
                "present": {"type": "boolean"},
                "for": {"type": "string"},
                "role": {"enum": ["supports_violation", "supports_authorization", "context"]},
            },
            "required": ["source", "role"],
        },
        "json_example": "json_extract(data, '$.User')",
        "finish_example": (
            '{"action": "finish", "verdict": "authorized|unauthorized|insufficient_evidence", '
            '"evidence": [{"source": "audit_log", "id": 57, "role": "supports_violation|supports_authorization|context"}, '
            '{"source": "category_log", "id": 12, "role": "..."}, '
            '{"source": "event_team", "event_id": 4, "user_name": "Ada Lovelace", "present": false, "role": "..."}], '
            '"principal": 5 or null, "unresolved": ["..."]}'),
        "state_semantics": (
            "## Evaluation semantics\nThese are the same semantics the reference evaluator implements. The "
            "accountable principal is read from the operation record itself. A membership relation at time t (here: "
            "the principal on the event's team; the principal on the team of the event's category) comes from "
            "grant/revoke change records: use the last change at or before t; if there is none, use the first change "
            "after t (a revoke after t means the relation held at t; a grant after t means it did not); if there is "
            "no change after t either, use the current-state table. Each of these readings is usable only if the "
            "source's coverage window contains the period it relies on (from the change to t, from t to the change, "
            "or from t to the snapshot); otherwise the relation is unknown. The operation is authorized if any "
            "alternative (here: either team) holds, unauthorized if every alternative fails, and otherwise the "
            "answer is insufficient_evidence."),
        "pool_sql": {
            "seed_operation": "SELECT * FROM audit_log WHERE id = {seed}",
            "event_records": ("SELECT * FROM audit_log WHERE event_id = (SELECT event_id FROM audit_log WHERE id = {seed}) "
                              "ORDER BY ts, id"),
            "event": "SELECT * FROM events WHERE id = (SELECT event_id FROM audit_log WHERE id = {seed})",
            "category_records": ("SELECT * FROM category_log WHERE category_id = (SELECT category_id FROM events WHERE "
                                 "id = (SELECT event_id FROM audit_log WHERE id = {seed})) ORDER BY ts, id"),
            "actor_category_records": ("SELECT * FROM category_log WHERE json_extract(data, '$.User') = (SELECT full_name "
                                       "FROM users WHERE id = (SELECT user_id FROM audit_log WHERE id = {seed})) "
                                       "ORDER BY ts, id"),
            "users": "SELECT * FROM users ORDER BY id",
            "categories": "SELECT * FROM categories ORDER BY id",
            "event_team": "SELECT * FROM event_team ORDER BY event_id, user_name",
            "category_team": "SELECT * FROM category_team ORDER BY category_id, user_name",
            "source_coverage": "SELECT * FROM source_coverage",
        },
        "pool_audit_keys": ("seed_operation", "event_records"),
        "pool_tables": ["audit_log", "category_log", "events", "users", "categories", "event_team", "category_team",
                        "source_coverage"],
    },
}

app_profile.PROFILES.update(PROFILES_EXT)
