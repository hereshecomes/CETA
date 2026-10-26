"""LLM investigators G3 (explore_reflect) and G4 (timeline_agent).

Both share with sql_agent / hypothesis_verify (agents.py): the public documents, the alert, the
read-only SQLTool and its budget (12 queries / 40 rows / 400 characters), MAX_TURNS = 16 LLM turns per
episode (counted across all phases), EVIDENCE_CAP = 8, the briefing text, the system prompt, the action /
final JSON schemas and the application strings of app_profile. No prompt below describes the evaluator's
semantics (three-valued logic, coverage windows, event-time rules); the only policy text the agent sees is
the public policy.md inside the briefing.

explore_reflect (G3, Reflexion-style scaffold):
  phase 1  explore: the agent may read the table definitions and a few sample rows (every query counts
           against the budget) and ends the phase with {"action": "proceed", "notes": ...}; at most
           EXPLORE_TURNS turns, then the phase ends;
  phase 2  investigate: the shared query / finish loop (as sql_agent) -> draft answer; at most the
           remaining turns minus REFLECT_RESERVE;
  phase 3  reflect: the agent critiques its own draft, may issue further queries while budget and turns
           remain, and finishes again; the phase-3 answer replaces the draft (the draft stands when
           phase 3 yields no valid answer).

timeline_agent (G4, structured scaffold):
  phase 1  the agent investigates and then emits {"action": "timeline", "focus": {...}, "timeline": [...]}:
           a JSON table of the recorded events it considers relevant to the alert's credential /
           principal / scope (it decides which records those are); at most TIMELINE_TURNS turns, then
           the table is forced;
  phase 2  the agent decides from that table (further queries allowed within the remaining budget and
           turns) through the shared query / finish loop.

The prompts were tuned on the development cases of pretix and Weblate only (Qwen3-32B, at most three
iterations per agent) and then fixed before any test case was run.
"""
import json

import app_profile
from agents import EVIDENCE_CAP, MAX_TURNS, action_schema, briefing, final_schema, system, _finish_from
from llm import parse_json
from sqltool import BudgetExhausted


EXPLORE_TURNS = 4       # G3 phase 1: at most 3 exploration queries + the proceed turn
REFLECT_RESERVE = 3     # G3: turns kept back for phase 3
TIMELINE_TURNS = 10     # G4 phase 1: at most 9 queries + the timeline turn
TIMELINE_ROWS = 24


# ---------------------------------------------------------------- schemas (shared fields from agents.py)

def proceed_schema():
    return {"type": "object",
            "properties": {"action": {"enum": ["query", "proceed"]}, "sql": {"type": "string"},
                           "notes": {"type": "string"}},
            "required": ["action"]}


def proceed_only_schema():
    return {"type": "object", "properties": {"action": {"enum": ["proceed"]}, "notes": {"type": "string"}},
            "required": ["action"]}


def critique_field():
    return {"critique": {"type": "array", "items": {"type": "string"}, "maxItems": 6}}


def reflect_action_schema():
    s = action_schema()
    return {**s, "properties": {**s["properties"], **critique_field()}}


def reflect_final_schema():
    s = final_schema()
    return {**s, "properties": {**s["properties"], **critique_field()}}


def timeline_fields():
    return {
        "focus": {"type": "object", "properties": {
            "credential": {"type": ["string", "null"]}, "principal": {"type": ["string", "null"]},
            "scope": {"type": ["string", "null"]}}},
        "timeline": {"type": "array", "maxItems": TIMELINE_ROWS, "items": {
            "type": "object",
            "properties": {"ts": {"type": ["string", "null"]}, "source": {"type": "string"},
                           "id": {"type": ["integer", "null"]}, "event": {"type": "string"},
                           "relevance": {"type": "string"}},
            "required": ["ts", "source", "event"]}},
    }


def timeline_action_schema():
    return {"type": "object",
            "properties": {"action": {"enum": ["query", "timeline"]}, "sql": {"type": "string"}, **timeline_fields()},
            "required": ["action"]}


def timeline_only_schema():
    return {"type": "object", "properties": {"action": {"enum": ["timeline"]}, **timeline_fields()},
            "required": ["action", "timeline"]}


# ---------------------------------------------------------------- shared phase loop

class Turns:
    """Episode-wide turn budget (MAX_TURNS LLM replies across all phases)."""

    def __init__(self, n=MAX_TURNS):
        self.total, self.left = n, n

    @property
    def used(self):
        return self.total - self.left


def phase_loop(llm, tool, messages, trace, turns, phase_cap, phase, act_schema, end_schema, end_action, force_text):
    """One phase of query turns ending in `end_action`. A turn is forced to the end action when the
    episode's turn budget, the phase cap or the query budget is exhausted (same rule as the shared
    investigate_loop: the forced reply uses the end schema). Returns the parsed end-action object or
    None (no valid end within this phase; three unparsable replies also end the phase)."""
    parse_failures, used = 0, 0
    while turns.left > 0 and used < phase_cap:
        forced = turns.left == 1 or used == phase_cap - 1 or tool.queries >= tool.max_queries
        if forced:
            messages.append({"role": "user", "content": force_text})
        out = llm.chat(messages, schema=end_schema if forced else act_schema)
        turns.left -= 1
        used += 1
        trace.append({"phase": phase, "turn": turns.used - 1, "content": out["content"],
                      "reasoning_chars": len(out["reasoning"]), "finish_reason": out["finish_reason"],
                      "forced": forced})
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
        if forced or obj.get("action") == end_action:
            return obj
        if obj.get("action") == "query" and isinstance(obj.get("sql"), str):
            try:
                res = tool.query(obj["sql"])
            except BudgetExhausted:
                continue
            messages.append({"role": "user", "content": json.dumps({"query_result": res}, default=str)})
        else:
            messages.append({"role": "user", "content": f"Unknown action. Use action query or {end_action}."})
    return None


def _left(tool, turns):
    return f"You have {tool.max_queries - tool.queries} queries and {turns.left} turns left."


def _fill(text, tool, turns):
    return text.replace("{retrieval}", RETRIEVAL_TEXT).replace("{left}", _left(tool, turns))


# ---------------------------------------------------------------- G3 explore_reflect

EXPLORE_TEXT = (
    "## How to proceed\n"
    "Work in three phases. Every reply is one JSON object.\n\n"
    "Phase 1 (explore). Before investigating the alert, get oriented: read the table definitions "
    "(`SELECT name, sql FROM sqlite_master WHERE type = 'table'`) and a few sample rows of the tables you "
    "expect to need, so that you know the exact column names, value formats and timestamps you will rely "
    "on; note what each source covers (`source_coverage`) and which tables hold current state. Every query "
    f"counts against your budget, so keep this short (at most {EXPLORE_TURNS - 1} queries). "
    'When you are oriented, reply {"action": "proceed", "notes": "<what you learned that matters for this '
    'investigation>"}.\n'
    "Phase 2 (investigate) and phase 3 (reflect) follow; you will be told when each starts."
)

INVESTIGATE_TEXT = (
    "## Phase 2 (investigate)\n"
    "Now investigate the alert under the policy. For each requirement of the policy, query for the records "
    "that establish or refute it at the time of the operation; use every source the policy and the schema "
    "describe for that requirement (log records, current-state tables, coverage windows) before concluding "
    "that it cannot be established. When you have them, finish with your draft answer "
    '({"action": "finish", ...} as described in the briefing), citing every record your conclusion rests on '
    "in the form the policy describes. {left}"
)

REFLECT_TEXT = (
    "## Phase 3 (reflect)\n"
    "Your draft answer is recorded above. Before it becomes final, critique it as a skeptical reviewer "
    "would, in both directions. (1) For each requirement of the policy, is there a cited record that "
    "actually establishes it at the time of the operation, or did you assume it? Did you consider the other "
    "possible explanations of the records you saw? (2) If the draft abstains, is that justified: is there a "
    "source the policy or the schema describes (log records, current-state tables, the coverage window) that "
    "you did not consult, or did not apply as the policy's application semantics describe, and that could "
    "establish the requirement? Put the weaknesses you find in a \"critique\" list in your reply. If a "
    "weakness can be fixed with a query, query; otherwise finish with your final answer, which may differ "
    "from the draft, citing every record it rests on in the form the policy describes (log rows by id; a "
    "current-state claim in the form the policy shows). {left}"
)

PROCEED_FORCE = ('Exploration budget exhausted. Reply {"action": "proceed", "notes": "..."} now.')
FINISH_FORCE = "Budget exhausted. Finish now with your best answer."

RETRIEVAL_TEXT = (
    "Only records you actually retrieved can be cited: include the id column when you select log rows you "
    "may cite, and query a current-state table before claiming what it contains."
)


def explore_reflect(llm, tool, informed=False):
    trace = []
    turns = Turns()
    messages = [{"role": "system", "content": system()},
                {"role": "user", "content": briefing(tool, tool.max_queries, informed) + "\n\n" + EXPLORE_TEXT}]
    explored = phase_loop(llm, tool, messages, trace, turns, EXPLORE_TURNS, "explore",
                          proceed_schema(), proceed_only_schema(), "proceed", PROCEED_FORCE)
    trace.append({"phase": "explore_done", "notes": (explored or {}).get("notes"), "queries": tool.queries})
    messages.append({"role": "user", "content": _fill(INVESTIGATE_TEXT, tool, turns)})
    draft = phase_loop(llm, tool, messages, trace, turns, max(turns.left - REFLECT_RESERVE, 1), "investigate",
                       action_schema(), final_schema(), "finish", FINISH_FORCE)
    draft = _finish_from(draft) if draft is not None else None
    trace.append({"phase": "draft", "content": json.dumps(draft), "queries": tool.queries})
    if turns.left <= 0:
        return draft, trace
    messages.append({"role": "user", "content": _fill(REFLECT_TEXT, tool, turns)})
    final = phase_loop(llm, tool, messages, trace, turns, turns.left, "reflect",
                       reflect_action_schema(), reflect_final_schema(), "finish", FINISH_FORCE)
    critiques = []
    for t in trace:
        if t.get("phase") == "reflect" and not t.get("parse_error"):
            try:
                critiques += parse_json(t["content"]).get("critique") or []
            except (ValueError, AttributeError):
                pass
    if final is None:
        trace.append({"phase": "reflect_done", "kept_draft": True, "critique": critiques})
        return draft, trace
    trace.append({"phase": "reflect_done", "kept_draft": False, "critique": critiques,
                  "changed_verdict": (draft or {}).get("verdict") != final.get("verdict")})
    return _finish_from(final), trace


# ---------------------------------------------------------------- G4 timeline_agent

TIMELINE_TEXT = (
    "## How to proceed\n"
    "Work in two phases. Every reply is one JSON object.\n\n"
    "Phase 1 (reconstruct the timeline). Decide which credential the operation used, which principal is "
    "accountable for it under the policy and which scope (team, project, object) the policy's "
    "requirements refer to; then collect, with queries, the recorded facts about them that bear on the "
    "policy and put them into one table ordered by time: the operation itself; how the credential came to "
    "exist; every recorded change, inside the coverage window, to the principal's standing in that scope "
    "and to the credential's validity; the coverage window of each source you used (rows at its start and "
    "end); and, as the last rows, what the current-state tables say at export time about those relations. "
    "When you have them, reply\n"
    '{"action": "timeline", "focus": {"credential": "...", "principal": "...", "scope": "..."}, '
    '"timeline": [{"ts": "<ISO-8601 time>", "source": "<table>", "id": <row id or null>, '
    '"event": "<what happened>", "relevance": "<why it matters for the policy>"}, ...]}\n'
    f"with the rows ordered by time and the operation itself as one of them (at most {TIMELINE_ROWS} rows, "
    f"at most {TIMELINE_TURNS - 1} queries in this phase). Phase 2 (decide) follows."
)

DECIDE_TEXT = (
    "## Phase 2 (decide)\n"
    "Your timeline table is recorded above. Decide the verdict from it: for each requirement of the policy, "
    "read its state at the time of the operation off the table, applying the application semantics the "
    "policy states (what a recorded change implies about the state before and after it, what the "
    "current-state tables show, where the absence of a record is informative), and name the rows that "
    "establish it. If the table lacks a record you need, you may query and extend it. Then finish "
    '({"action": "finish", ...} as described in the briefing), citing every record the verdict rests on in '
    "the form the policy describes (log rows by id; a current-state claim in the form the policy shows). {left}"
)

TIMELINE_FORCE = ('Budget for this phase exhausted. Reply {"action": "timeline", "focus": {...}, "timeline": [...]} '
                  "now with the events you have collected.")


def timeline_agent(llm, tool, informed=False):
    trace = []
    turns = Turns()
    messages = [{"role": "system", "content": system()},
                {"role": "user", "content": briefing(tool, tool.max_queries, informed) + "\n\n" + TIMELINE_TEXT}]
    tl = phase_loop(llm, tool, messages, trace, turns, TIMELINE_TURNS, "timeline",
                    timeline_action_schema(), timeline_only_schema(), "timeline", TIMELINE_FORCE)
    trace.append({"phase": "timeline_done", "focus": (tl or {}).get("focus"), "timeline": (tl or {}).get("timeline"),
                  "n_rows": len((tl or {}).get("timeline") or []), "queries": tool.queries})
    if turns.left <= 0:
        return None, trace
    messages.append({"role": "user", "content": _fill(DECIDE_TEXT, tool, turns)})
    final = phase_loop(llm, tool, messages, trace, turns, turns.left, "decide",
                       action_schema(), final_schema(), "finish", FINISH_FORCE)
    return (_finish_from(final) if final is not None else None), trace


METHODS = {
    "explore_reflect": explore_reflect,
    "timeline_agent": timeline_agent,
    "explore_reflect_informed": lambda llm, tool: explore_reflect(llm, tool, informed=True),
    "timeline_agent_informed": lambda llm, tool: timeline_agent(llm, tool, informed=True),
}
