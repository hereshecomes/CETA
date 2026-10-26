"""LLM compiler of CETA: maps an application's public policy and schema documents onto the evaluator's
specification language (evaluator.SPEC_LANGUAGE). Documents only, no case data.

The structured-output schema enforces the required fields of each principal kind, scope form and
condition kind (anyOf per kind); a draw that fails the validator is returned to the model with the
validator's message for at most MAX_REPAIRS repair rounds (counted in cost). A draw that is still invalid
afterwards is invalid and scores zero on every case.

Usage: compiler.py RUN_NAME K [--docs DIR]   (draws K independent specifications)
Selection (evaluation/compile_and_select.py), fixed before any test case is run: highest development c&g,
ties -> lowest index. All K specifications, every attempt and their development results are kept.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "methods"))
import evaluator as ev  # noqa: E402
from llm import LLM, parse_json  # noqa: E402

MAX_REPAIRS = 2
S = {"type": "string"}


def _obj(props, required):
    return {"type": "object", "properties": props, "required": required}


def _kind(k):
    return {"const": k}


CHANGE_LOG = _obj({"kind": _kind("change_log"), "table": S, "source": S, "action_column": S, "grant_value": S,
                   "revoke_value": S, "scope_column": S, "scope_ref": S, "subject_expr": S, "subject_ref": S,
                   "time_column": S, "id_column": S, "current_state_table": S, "current_state_scope_column": S,
                   "current_state_subject_column": S, "current_state_source": S},
                  ["kind", "table", "source", "action_column", "grant_value", "revoke_value", "scope_column",
                   "scope_ref", "subject_expr", "subject_ref", "time_column", "id_column"])
INTERVAL = _obj({"kind": _kind("interval"), "table": S, "source": S, "subject_column": S, "subject_ref": S,
                 "scope_column": S, "scope_ref": S, "from_column": S, "to_column": S, "id_column": S},
                ["kind", "table", "source", "subject_column", "subject_ref", "from_column", "to_column", "id_column"])
PRIOR_EVENT = _obj({"kind": _kind("prior_event"), "table": S, "source": S, "action_column": S, "action_value": S,
                    "object_column": S, "object_ref": S, "time_column": S, "id_column": S, "actor_column": S,
                    "actor_must_differ": {"type": "boolean"}, "max_age_minutes": {"type": "integer"}},
                   ["kind", "table", "source", "action_column", "action_value", "object_column", "object_ref",
                    "time_column", "id_column"])
CONDITION_SCHEMA = {"anyOf": [CHANGE_LOG, INTERVAL, PRIOR_EVENT]}

PRINCIPAL_SCHEMA = {"anyOf": [
    _obj({"kind": _kind("column"), "column": S}, ["kind", "column"]),
    _obj({"kind": _kind("binding_event"), "credential_column": S, "table": S, "action_column": S, "action_value": S,
          "credential_expr": S, "holder_column": S, "time_column": S, "id_column": S, "source": S,
          "scope_column": S, "scope_ref": S},
         ["kind", "credential_column", "table", "action_column", "action_value", "credential_expr", "holder_column",
          "time_column", "id_column", "source"]),
    _obj({"kind": _kind("binding_table"), "credential_column": S, "table": S, "credential_table_column": S,
          "holder_column": S, "source": S},
         ["kind", "credential_column", "table", "credential_table_column", "holder_column", "source"]),
]}
SCOPE_SCHEMA = {"anyOf": [
    _obj({"name": S, "ref": S}, ["name", "ref"]),
    _obj({"name": S, "lookup_table": S, "lookup_key_expr": S, "lookup_key_ref": S, "lookup_value_expr": S, "source": S},
         ["name", "lookup_table", "lookup_key_expr", "lookup_key_ref", "lookup_value_expr"]),
]}
SPEC_SCHEMA = _obj({
    "operation": _obj({"table": S, "id_column": S, "time_column": S}, ["table", "id_column", "time_column"]),
    "principal": PRINCIPAL_SCHEMA,
    "scopes": {"type": "array", "items": SCOPE_SCHEMA},
    "paths": {"type": "array", "minItems": 1, "items": _obj(
        {"name": S, "conditions": {"type": "array", "minItems": 1, "items": CONDITION_SCHEMA}}, ["name", "conditions"])},
    "coverage": _obj({"table": S, "source_column": S, "from_column": S, "to_column": S},
                     ["table", "source_column", "from_column", "to_column"]),
}, ["operation", "principal", "scopes", "paths", "coverage"])

COMPILE_PREAMBLE = """You are configuring a generic authorization-audit evaluator for an application. The evaluator
decides, for one recorded operation at time t, whether it was authorized under the organization's
written policy, using only records in a read-only database. Its semantics are fixed and described
below; you cannot change them. Your job is to express the organization's policy as a specification
in the evaluator's language, using only the tables and columns in the application's schema
document. Express every way the policy allows the operation to be authorized as a path; express
each policy requirement as a condition of the fitting kind. Do not invent tables, columns or
action values. Reply with one JSON object and nothing else.
"""

REPAIR = ("The specification failed validation: {error}\n"
          "Correct it and reply with the complete corrected specification as one JSON object.")


def compile_prompt(docs_dir):
    docs = Path(docs_dir)
    return (COMPILE_PREAMBLE + "\n" + ev.SPEC_LANGUAGE + "\n\n# Application documents\n\n" +
            (docs / "policy.md").read_text() + "\n\n" + (docs / "schema.md").read_text())


def compile_spec(llm, docs_dir, max_repairs=MAX_REPAIRS):
    """Returns (spec_or_None, last_raw_reply, attempts, valid)."""
    msgs = [{"role": "system", "content": "You configure software precisely. Reply with one JSON object."},
            {"role": "user", "content": compile_prompt(docs_dir)}]
    attempts, spec, out = [], None, None
    for rnd in range(max_repairs + 1):
        out = llm.chat(msgs, schema=SPEC_SCHEMA)
        try:
            spec = parse_json(out["content"])
            ev.validate_spec(spec)
            attempts.append({"round": rnd, "valid": True, "finish_reason": out["finish_reason"],
                             "reasoning_chars": len(out["reasoning"])})
            return spec, out, attempts, True
        except ValueError as exc:  # SpecError and JSON parse errors
            attempts.append({"round": rnd, "valid": False, "error": str(exc)[:300], "finish_reason": out["finish_reason"],
                             "reasoning_chars": len(out["reasoning"])})
            msgs.append({"role": "assistant", "content": out["content"]})
            msgs.append({"role": "user", "content": REPAIR.format(error=str(exc)[:300])})
    return spec, out, attempts, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_name")
    ap.add_argument("k", type=int)
    ap.add_argument("--docs", default=str(ROOT / "audit_rules" / "pretix"))
    ap.add_argument("--base-url", default="http://127.0.0.1:8100")
    ap.add_argument("--model", default="qwen3-32b")
    ap.add_argument("--max-repairs", type=int, default=MAX_REPAIRS)
    args = ap.parse_args()
    out = ROOT / "runs" / args.run_name / "specs"
    out.mkdir(parents=True, exist_ok=True)
    (out / "compile_prompt.txt").write_text(compile_prompt(args.docs))
    for i in range(args.k):
        llm = LLM(args.base_url, args.model, temperature=0.6)
        rec = {"index": i, "docs": args.docs, "max_repairs": args.max_repairs}
        try:
            spec, raw, attempts, valid = compile_spec(llm, args.docs, max_repairs=args.max_repairs)
            rec.update(ok=True, valid=valid, spec=spec, attempts=attempts, repairs_used=len(attempts) - 1,
                       cost=llm.cost(), finish_reason=raw["finish_reason"])
            if not valid:
                rec["validation_error"] = attempts[-1].get("error")
            if spec is not None:
                (out / f"spec_{i}.json").write_text(json.dumps(spec, indent=1))
        except Exception as exc:
            rec.update(ok=False, valid=False, error=f"{type(exc).__name__}: {exc}"[:300], cost=llm.cost())
        (out / f"compile_{i}.json").write_text(json.dumps(rec, indent=1))
        print(json.dumps({"index": i, "ok": rec["ok"], "valid": rec.get("valid"), "repairs_used": rec.get("repairs_used")}))


if __name__ == "__main__":
    main()
