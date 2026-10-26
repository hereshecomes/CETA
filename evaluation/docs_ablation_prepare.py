"""Document ablations E11 / E12 / E14 for pretix and Weblate. Writes the modified public documents under
runs/docs_ablation/<app>/<variant>/{policy.md, schema.md} with a provenance.json each (source files and the
transformation). The original documents (audit_rules/<app>/) are read, never written.

  E11 raw_schema           policy.md unchanged; schema.md = the export's raw DDL (`sql` of sqlite_master, ordered by
                           type and name, each statement terminated by ';') of the first public case of the application;
                           checked identical over every public case of the application.
  E12 paraphrased_policy   policy.md rewritten once by Qwen3-32B (T = 0.6, top-p 0.95, 8192 tokens, thinking on) with the
                           fixed prompt PARAPHRASE_PROMPT into a colloquial version without table or column names;
                           schema.md unchanged. Generation g<n>/ keeps prompt, raw reply and cost. The accepted generation
                           is copied to policy.md only after a hand check against the original rule list (check.md);
                           if a rule is lost, one regeneration is allowed and both are recorded.
  E14 docs_minus_semantics policy.md with every application-semantics passage removed: from the literal marker
                           "Application semantics:" to the end of its paragraph; schema.md unchanged; policy.diff records
                           the removal.

Usage: docs_ablation_prepare.py static                 (E11 + E14 for both applications)
       docs_ablation_prepare.py gen-e12 --app A --gen N    (one LLM generation into g<N>/)
       docs_ablation_prepare.py accept-e12 --app A --gen N (copies g<N>/policy.md to policy.md; requires check.md)
"""
import argparse
import difflib
import json
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "methods"))
OUT = ROOT / "runs" / "docs_ablation"
APPS = {"pretix": {"docs": ROOT / "audit_rules" / "pretix", "cases": ROOT / "cases"},
        "weblate": {"docs": ROOT / "audit_rules" / "weblate", "cases": ROOT / "cases" / "weblate"}}
VARIANT_DIRS = {"E11": "E11_raw_schema", "E12": "E12_paraphrased_policy", "E14": "E14_docs_minus_semantics"}
SEM_MARK = "Application semantics:"

PARAPHRASE_SYSTEM = "You rewrite documents faithfully. Reply with the rewritten document only, as Markdown."
PARAPHRASE_PROMPT = """Rewrite the following organization policy as a colloquial explanation for the organization's staff.

Requirements:
- Keep every rule, every condition, every exception and every statement about how the application records things,
  with the same meaning. Do not add rules, examples or interpretations of your own.
- Do not mention any database table name or column name, and do not use code formatting. Describe records and
  fields in everyday words (for example "the activity history" instead of a table name).
- Values that the application writes into its records (such as the names of recorded actions or activities) may
  be quoted exactly as they appear in the policy.
- Keep the final section that says what an answer must contain, also without table or column names.
- Plain prose and simple lists; keep the rule numbers P1, P2, ... so that each rule can be found.

Policy:

"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ddl_of(db):
    rows = sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
    return "\n".join(f"{r[3]};" for r in rows if r[3]) + "\n"


def write_variant(app, variant, policy, schema, prov):
    d = OUT / app / VARIANT_DIRS[variant]
    d.mkdir(parents=True, exist_ok=True)
    (d / "policy.md").write_text(policy)
    (d / "schema.md").write_text(schema)
    src = APPS[app]["docs"]
    prov.update(variant=variant, app=app, written_at=now(),
                source_docs=str(src.relative_to(ROOT)),
                unchanged={f: (src / f).read_bytes() == (d / f).read_bytes() for f in ("policy.md", "schema.md")})
    (d / "provenance.json").write_text(json.dumps(prov, indent=1))
    return d


def remove_semantics(text):
    """Every passage from SEM_MARK to the end of its paragraph (paragraph = block separated by a blank line)."""
    paras = text.split("\n\n")
    removed, out = [], []
    for p in paras:
        if SEM_MARK in p:
            i = p.index(SEM_MARK)
            removed.append(p[i:])
            p = p[:i].rstrip()
            if p.endswith("\n"):
                p = p.rstrip("\n")
        out.append(p)
    return "\n\n".join(out), removed


def static():
    for app, a in APPS.items():
        idx = json.loads((a["cases"] / "private" / "index.json").read_text())
        dbs = [a["cases"] / "public" / e["case_id"] / "episode.sqlite" for e in idx]
        first = ddl_of(dbs[0])
        distinct = {ddl_of(db) for db in dbs}
        policy = (a["docs"] / "policy.md").read_text()
        schema = (a["docs"] / "schema.md").read_text()
        d = write_variant(app, "E11", policy, first, {
            "transformation": "schema.md replaced by the raw DDL (sqlite_master.sql, ORDER BY type, name; ';' after each) of the first public case",
            "ddl_source_case": idx[0]["case_id"], "n_cases_checked": len(dbs),
            "ddl_identical_across_cases": len(distinct) == 1})
        print(app, "E11", d, "identical over", len(dbs), "cases:", len(distinct) == 1)
        new_policy, removed = remove_semantics(policy)
        diff = "".join(difflib.unified_diff(policy.splitlines(True), new_policy.splitlines(True),
                                             "original/policy.md", "E14/policy.md"))
        d = write_variant(app, "E14", new_policy, schema, {
            "transformation": f"every passage from the marker {SEM_MARK!r} to the end of its paragraph removed from policy.md",
            "removed_passages": removed, "n_removed": len(removed),
            "chars_removed": len(policy) - len(new_policy)})
        (d / "policy.diff").write_text(diff)
        print(app, "E14", d, "removed", len(removed), "passages,", len(policy) - len(new_policy), "chars")


def gen_e12(app, gen):
    from run_method import BackboneLLM, BACKBONES
    prof = BACKBONES["qwen3-32b"]
    d = OUT / app / VARIANT_DIRS["E12"]
    g = d / f"g{gen}"
    if (g / "generation.json").exists():
        raise SystemExit(f"{g} exists: a generation is never overwritten")
    g.mkdir(parents=True, exist_ok=True)
    policy = (APPS[app]["docs"] / "policy.md").read_text()
    msgs = [{"role": "system", "content": PARAPHRASE_SYSTEM}, {"role": "user", "content": PARAPHRASE_PROMPT + policy}]
    (g / "prompt.json").write_text(json.dumps(msgs, indent=1))
    llm = BackboneLLM(prof["base_url"], prof["model"], temperature=0.6, thinking=bool(prof["thinking"]),
                extra_kwargs=prof.get("chat_template_kwargs") or {})
    t0 = time.time()
    out = llm.chat(msgs)
    text = out["content"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:]
    (g / "raw_reply.txt").write_text(out["content"])
    (g / "policy.md").write_text(text.strip() + "\n")
    # automatic aid for the hand check: table / column names of the DDL that appear as whole words
    ddl = ddl_of(APPS[app]["cases"] / "public" / json.loads((APPS[app]["cases"] / "private" / "index.json").read_text())[0]["case_id"] / "episode.sqlite")
    names = set(re.findall(r"CREATE TABLE (\w+)", ddl))
    for cols in re.findall(r"\(([^;]*)\);", ddl, flags=re.S):
        names |= {c.strip().split()[0] for c in cols.replace("\n", " ").split(",") if c.strip() and not c.strip().upper().startswith("PRIMARY")}
    found = sorted(n for n in names if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", text) and "_" in n)
    found_plain = sorted(n for n in names if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", text) and "_" not in n)
    rec = {"app": app, "gen": gen, "backbone": "qwen3-32b", "temperature": 0.6, "top_p": 0.95, "max_tokens": 8192,
           "thinking": True,
           "finish_reason": out["finish_reason"], "reasoning_chars": len(out["reasoning"]), "cost": llm.cost(),
           "wall_s": round(time.time() - t0, 1), "generated_at": now(),
           "identifier_scan": {"ddl_names_with_underscore_found": found,
                               "plain_ddl_words_found (may be ordinary English)": found_plain},
           "code_formatting_found": "`" in text}
    (g / "generation.json").write_text(json.dumps(rec, indent=1))
    print(json.dumps(rec, indent=1))


def accept_e12(app, gen):
    d = OUT / app / VARIANT_DIRS["E12"]
    if not (d / "check.md").exists():
        raise SystemExit("check.md (hand check against the rule list) must be written first")
    g = d / f"g{gen}"
    gens = sorted(p.name for p in d.glob("g*") if p.is_dir())
    schema = (APPS[app]["docs"] / "schema.md").read_text()
    write_variant(app, "E12", (g / "policy.md").read_text(), schema, {
        "transformation": "policy.md = the accepted LLM paraphrase (see g<n>/ and check.md)", "accepted_generation": g.name,
        "generations": gens, "generation": json.loads((g / "generation.json").read_text())})
    print(app, "E12 accepted", g.name, "of", gens)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["static", "gen-e12", "accept-e12"])
    ap.add_argument("--app", choices=sorted(APPS))
    ap.add_argument("--gen", type=int, default=1)
    a = ap.parse_args()
    if a.cmd == "static":
        static()
    elif a.cmd == "gen-e12":
        gen_e12(a.app, a.gen)
    else:
        accept_e12(a.app, a.gen)


if __name__ == "__main__":
    raise SystemExit(main())
