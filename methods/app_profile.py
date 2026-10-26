"""Per-application profile for the shared method code.

The method logic in agents.py / pool_judge.py / run_method.py is application-independent; only a few
strings and schemas depend on the application: the domain phrase in the system prompt, the name of the
principal field in the answer, the evidence-item schema (which tables a citation may name and which keys
a current-state claim carries), the worked example in the briefing, the semantics text of the informed
variants, the fixed evidence pool of pool_judge, the documents and case directories, the expert rule
module and the scorer. Those live here (pretix, Weblate) and in app_profile_ext.py (Wagtail, Indico).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROFILES = {
    "pretix": {
        "docs": ROOT / "audit_rules" / "pretix",
        "cases": ROOT / "cases",
        "scorer": "score",
        "rules_module": "rules",
        "domain": "a ticketing back office",
        "principal_field": "issuer_user_id",
        "principal_schema": {"type": ["integer", "null"]},
        "evidence_item": {
            "type": "object",
            "properties": {
                "source": {"enum": ["audit_log", "team_members"]},
                "id": {"type": "integer"},
                "team_id": {"type": "integer"},
                "user_id": {"type": "integer"},
                "member": {"type": "boolean"},
                "role": {"enum": ["supports_violation", "supports_authorization", "context"]},
            },
            "required": ["source", "role"],
        },
        "json_example": "json_extract(data, '$.user')",
        "finish_example": (
            '{"action": "finish", "verdict": "authorized|unauthorized|insufficient_evidence", '
            '"evidence": [{"source": "audit_log", "id": 123, "role": "supports_violation|supports_authorization|context"}, '
            '{"source": "team_members", "team_id": 1, "user_id": 2, "member": true, "role": "..."}], '
            '"issuer_user_id": 5 or null, "unresolved": ["..."]}'),
        "state_semantics": (
            "## Evaluation semantics\nThese are the same semantics the reference evaluator implements. The holder "
            "of a credential comes only from its binding record (here: the token creation record). The relation at "
            "time t (here: the issuer's membership in the token's team) comes from grant/revoke change records: use "
            "the last change at or before t; if there is none, use the first change after t (a revoke after t means "
            "the relation held at t; a grant after t means it did not); if there is no change after t either, use "
            "the current-state table. A missing binding record means the answer is insufficient_evidence."),
        "pool_sql": {
            "seed_operation": "SELECT * FROM audit_log WHERE id = {seed}",
            "team_tokens": "SELECT * FROM team_tokens ORDER BY id",
            "token_and_membership_records": ("SELECT * FROM audit_log WHERE action IN ('pretix.team.token.created', "
                                             "'pretix.team.token.deleted', 'pretix.team.member.added', "
                                             "'pretix.team.member.removed') ORDER BY ts, id"),
            "team_members_current": "SELECT * FROM team_members ORDER BY team_id, user_id",
            "teams": "SELECT id, name FROM teams ORDER BY id",
            "users": "SELECT * FROM users ORDER BY id",
            "source_coverage": "SELECT * FROM source_coverage",
        },
        "pool_audit_keys": ("seed_operation", "token_and_membership_records"),
        "pool_tables": ["team_members"],  # tables counted as observed besides audit_log rows
    },
    "weblate": {
        "docs": ROOT / "audit_rules" / "weblate",
        "cases": ROOT / "cases" / "weblate",
        "scorer": "score_generic",
        "rules_module": "rules_weblate",
        "domain": "a translation platform",
        "principal_field": "principal",
        "principal_schema": {"type": ["string", "null"]},
        "evidence_item": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "id": {"type": "integer"},
                "team": {"type": "string"},
                "username": {"type": "string"},
                "present": {"type": "boolean"},
                "for": {"type": "string"},
                "role": {"enum": ["supports_violation", "supports_authorization", "context"]},
            },
            "required": ["source", "role"],
        },
        "json_example": "json_extract(details, '$.username')",
        "finish_example": (
            '{"action": "finish", "verdict": "authorized|unauthorized|insufficient_evidence", '
            '"evidence": [{"source": "account_log", "id": 12, "role": "supports_violation|supports_authorization|context"}, '
            '{"source": "token_validity", "id": 15, "role": "..."}, '
            '{"source": "team_members", "team": "Administration", "username": "jdoe", "present": true, "role": "..."}], '
            '"principal": "jdoe" or null, "unresolved": ["..."]}'),
        "state_semantics": (
            "## Evaluation semantics\nThese are the same semantics the reference evaluator implements. The holder "
            "of a credential comes only from its binding record (here: the token's creation record, which names its "
            "creator). A validity-period relation (here: the token's validity period) holds at time t if a recorded "
            "period covers t; it fails if no recorded period covers t and the source is complete for t according to "
            "its coverage window; otherwise it is unknown. A membership relation at time t (here: the creator's "
            "membership in the token's team) comes from grant/revoke change records: use the last change at or "
            "before t; if there is none, use the first change after t (a revoke after t means the relation held at "
            "t; a grant after t means it did not); if there is no change after t either, use the current-state "
            "table. The operation is authorized only if every required relation holds, unauthorized if any required "
            "relation fails, and otherwise the answer is insufficient_evidence. A missing binding record leaves the "
            "membership relation unknown."),
        "pool_sql": {
            "seed_operation": "SELECT * FROM audit_log WHERE id = {seed}",
            "tokens": "SELECT * FROM tokens ORDER BY user_id",
            "token_validity": "SELECT * FROM token_validity ORDER BY user_id",
            "token_and_membership_records": ("SELECT * FROM account_log WHERE activity IN ('token-created', "
                                             "'team-add', 'team-remove', 'disabled-expiry') ORDER BY ts, id"),
            "team_members_current": "SELECT * FROM team_members ORDER BY team, username",
            "teams": "SELECT id, name FROM teams ORDER BY id",
            "users": "SELECT * FROM users ORDER BY id",
            "source_coverage": "SELECT * FROM source_coverage",
        },
        "pool_audit_keys": ("seed_operation",),
        "pool_tables": ["audit_log", "account_log", "tokens", "token_validity", "team_members", "teams", "users",
                        "source_coverage"],
    },
}

_current = {"name": "pretix"}


def select(name):
    if name not in PROFILES:
        raise KeyError(f"unknown application profile {name!r}")
    _current["name"] = name
    return PROFILES[name]


def current():
    return PROFILES[_current["name"]]


def name():
    return _current["name"]
