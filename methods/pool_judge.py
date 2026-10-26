"""Evidence-pool judge J: one LLM call over a fixed, method-independent evidence pool (no querying).
The fixed pool queries, the principal field name and the prompt strings come from app_profile;
pool_judge_informed adds the evaluator's semantics, stated in words.
"""
import json
import sqlite3

import app_profile
from agents import _finish_from, docs, final_schema, state_semantics, system
from llm import parse_json


def build_pool(tool):
    con = sqlite3.connect(f"file:{tool.case_dir / 'episode.sqlite'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pool = {}
    for k, q in app_profile.current()["pool_sql"].items():
        pool[k] = [dict(r) for r in con.execute(q.format(seed=int(tool.alert["alert"]["audit_log_id"])))]
    con.close()
    return pool


def pool_judge(llm, tool, informed=False):
    p = app_profile.current()
    pool = build_pool(tool)
    extra = ("\n\n" + state_semantics()) if informed else ""
    msgs = [{"role": "system", "content": system()},
            {"role": "user", "content": f"{(docs() / 'policy.md').read_text()}{extra}\n\n{(docs() / 'schema.md').read_text()}\n\n"
             f"## Alert\n{json.dumps(tool.alert)}\n\n## Records retrieved for you (fixed pool; you cannot query)\n"
             f"{json.dumps(pool, default=str)}\n\nDecide and reply with one JSON object with verdict, evidence, "
             f"{p['principal_field']}, unresolved. Cite only records shown above."}]
    out = llm.chat(msgs, schema=final_schema())
    audit_ids = [r["id"] for k in p["pool_audit_keys"] for r in pool[k]]
    trace = [{"phase": "judge", "content": out["content"], "pool_sizes": {k: len(v) for k, v in pool.items()},
              "pool_audit_ids": audit_ids, "pool_tables": list(p["pool_tables"])}]
    try:
        return _finish_from(parse_json(out["content"])), trace
    except ValueError:
        return None, trace
