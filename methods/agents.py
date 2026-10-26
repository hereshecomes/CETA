"""LLM investigators G1 and G2. All share the public documents, the alert, the read-only SQLTool, the
query budget, the evidence cap and the model backbone.

sql_agent (G1): plain iterative SQL investigation in the style of ReAct.
hypothesis_verify (G2): plans competing hypotheses, runs the same iterative investigation, and ends with
one verification pass over its transcript (no extra queries).

The `_informed` variants add the evaluator's semantics, stated in words, to the briefing. The
application-specific strings and schemas (domain phrase, principal field, evidence-item schema, briefing
example, semantics text, documents directory) come from app_profile; the method logic is shared.
"""
import json

import app_profile
from llm import InfraError, parse_json
from sqltool import BudgetExhausted

EVIDENCE_CAP = 8
MAX_TURNS = 16


def docs():
    return app_profile.current()["docs"]


def finish_fields():
    p = app_profile.current()
    return {
        "verdict": {"enum": ["authorized", "unauthorized", "insufficient_evidence"]},
        "evidence": {"type": "array", "items": p["evidence_item"], "maxItems": EVIDENCE_CAP},
        p["principal_field"]: p["principal_schema"],
        "unresolved": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    }


def action_schema():
    return {"type": "object",
            "properties": {"action": {"enum": ["query", "finish"]}, "sql": {"type": "string"}, **finish_fields()},
            "required": ["action"]}


def final_schema():
    return {"type": "object", "properties": finish_fields(), "required": ["verdict", "evidence"]}


PLAN_SCHEMA = {"type": "object", "properties": {
    "hypotheses": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    "facts_needed": {"type": "array", "items": {"type": "object", "properties": {
        "fact": {"type": "string"}, "how_to_check": {"type": "string"}}, "required": ["fact", "how_to_check"]},
        "maxItems": 6}}, "required": ["hypotheses", "facts_needed"]}


def system():
    return (f"You are a security investigator auditing API operations in {app_profile.current()['domain']}. "
            "You can read a SQLite database only through SQL queries. Apply the organization's policy exactly "
            "as written. Every turn, reply with exactly one JSON object.")


def state_semantics():
    return app_profile.current()["state_semantics"]


def briefing(tool, budget, informed=False):
    p = app_profile.current()
    extra = ("\n\n" + state_semantics()) if informed else ""
    return (f"{(docs() / 'policy.md').read_text()}{extra}\n\n{(docs() / 'schema.md').read_text()}\n\n"
            f"## Alert\n{json.dumps(tool.alert, indent=1)}\n\n"
            f"## Budget and actions\nYou may run at most {budget} SQL queries (SQLite dialect, read-only, one "
            f"statement each, at most {tool.max_rows} rows returned). JSON functions such as "
            f"{p['json_example']} are available.\n"
            '- To query: {"action": "query", "sql": "SELECT ..."}\n'
            f"- To finish: {p['finish_example']}\nCite at most {EVIDENCE_CAP} evidence items.")


def _finish_from(obj):
    return {k: obj.get(k) for k in ("verdict", "evidence", app_profile.current()["principal_field"], "unresolved")}


def investigate_loop(llm, tool, messages, trace):
    """Shared iterative loop. Returns a finish dict or None (no valid finish)."""
    parse_failures = 0
    for turn in range(MAX_TURNS):
        forced = tool.queries >= tool.max_queries
        if forced:
            messages.append({"role": "user", "content": "Query budget exhausted. Finish now with your best answer."})
        out = llm.chat(messages, schema=final_schema() if forced else action_schema())
        trace.append({"turn": turn, "content": out["content"], "reasoning_chars": len(out["reasoning"]),
                      "finish_reason": out["finish_reason"]})
        messages.append({"role": "assistant", "content": out["content"]})
        try:
            obj = parse_json(out["content"])
        except ValueError:
            parse_failures += 1
            trace[-1]["parse_error"] = True
            if parse_failures >= 3:
                return None
            messages.append({"role": "user", "content": "Your reply was not a valid JSON object. Reply with one JSON object."})
            continue
        if forced or obj.get("action") == "finish":
            return _finish_from(obj)
        if obj.get("action") == "query" and isinstance(obj.get("sql"), str):
            try:
                res = tool.query(obj["sql"])
            except BudgetExhausted:
                continue
            messages.append({"role": "user", "content": json.dumps({"query_result": res}, default=str)})
        else:
            messages.append({"role": "user", "content": "Unknown action. Use action query or finish."})
    return None


def sql_agent(llm, tool, informed=False):
    trace = []
    messages = [{"role": "system", "content": system()},
                {"role": "user", "content": briefing(tool, tool.max_queries, informed)}]
    final = investigate_loop(llm, tool, messages, trace)
    return final, trace


def verify_text():
    return ("You are a skeptical reviewer of an authorization investigation. Using only the policy, the alert, "
            "and the query transcript below, verify the draft answer. For every condition the policy requires, "
            "check that a cited record actually establishes it at the time of the operation, not at some other "
            "time. Check that any conclusion drawn from the absence of a record respects the coverage of that "
            "source. Consider the competing explanations. Then output the final answer as one JSON object with "
            f"verdict, evidence, {app_profile.current()['principal_field']}, unresolved. You may change the verdict "
            "or the evidence, but you may only cite records that appear in the transcript.")


def hypothesis_verify(llm, tool, informed=False):
    trace = []
    brief = briefing(tool, tool.max_queries, informed)
    plan_msgs = [{"role": "system", "content": system()},
                 {"role": "user", "content": brief + "\n\nBefore querying, list the competing hypotheses for this "
                  "alert and the facts you must establish to decide between them, with how to check each. "
                  'Reply as {"hypotheses": [...], "facts_needed": [{"fact": ..., "how_to_check": ...}]}.'}]
    plan = llm.chat(plan_msgs, schema=PLAN_SCHEMA)
    trace.append({"phase": "plan", "content": plan["content"]})
    messages = [{"role": "system", "content": system()},
                {"role": "user", "content": brief + "\n\n## Your investigation plan\n" + plan["content"] +
                 "\n\nInvestigate each needed fact, and look for records that would refute your leading hypothesis."}]
    draft = investigate_loop(llm, tool, messages, trace)
    if draft is None:
        return None, trace
    transcript = [{"sql": e["sql"], "ok": e["ok"]} for e in tool.log]
    results = [m["content"] for m in messages if m["role"] == "user" and m["content"].startswith('{"query_result"')]
    for t, r in zip(transcript, results):
        t["result"] = json.loads(r)["query_result"]
    vmsgs = [{"role": "system", "content": system()},
             {"role": "user", "content": f"{verify_text()}\n\n{(docs() / 'policy.md').read_text()}"
              + (("\n\n" + state_semantics()) if informed else "") + "\n\n## Alert\n"
              f"{json.dumps(tool.alert)}\n\n## Transcript\n{json.dumps(transcript, default=str)}\n\n"
              f"## Draft answer\n{json.dumps(draft)}"}]
    v = llm.chat(vmsgs, schema=final_schema())
    trace.append({"phase": "verify", "content": v["content"]})
    try:
        final = _finish_from(parse_json(v["content"]))
    except ValueError:
        trace.append({"phase": "verify", "parse_error": True})
        final = draft
    return final, trace


METHODS = {
    "sql_agent": sql_agent,
    "hypothesis_verify": hypothesis_verify,
    "sql_agent_informed": lambda llm, tool: sql_agent(llm, tool, informed=True),
    "hypothesis_verify_informed": lambda llm, tool: hypothesis_verify(llm, tool, informed=True),
}
