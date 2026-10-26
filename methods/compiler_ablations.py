"""Compiler ablations E6-E10. The module reuses the compiler's pieces (compiler.py: preamble, repair
message, output schema, MAX_REPAIRS; evaluator.py: SPEC_LANGUAGE, validate_spec; llm.py: parse_json) and
the earlier flat output schema (spec_schema_flat.json). The draw loop below is the loop of
compiler.compile_spec with the schema, the prompt text and the repair budget as parameters; with the
default parameters the prompt equals compiler.compile_prompt(docs) and the schema is compiler.SPEC_SCHEMA.

Variants (everything not named is the setting of the full compiler: K = 5, T = 0.6, top-p 0.95, 8192
tokens, thinking on, 2 repair rounds, manual with the abstract example, per-kind anyOf schema):
  E6 flat schema      output schema = the flat schema (requires only `kind` / `table`; no per-kind anyOf)
  E7 no repair loop   max_repairs = 0 (a draw that fails the validator is invalid)
  E8 no example       the language manual without its "Abstract example" section
  E9 K = 1            a single draw (selection = that draw if valid)
  E10 T = 0           temperature 0
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "methods"))
import compiler as cv  # noqa: E402
import evaluator as ev  # noqa: E402
from llm import parse_json  # noqa: E402

FLAT_SCHEMA = ROOT / "methods" / "spec_schema_flat.json"

VARIANTS = {
    "E6": {"name": "flat_schema", "schema": "flat", "k": 5, "max_repairs": cv.MAX_REPAIRS, "example": True, "temperature": 0.6},
    "E7": {"name": "no_repair", "schema": "per_kind", "k": 5, "max_repairs": 0, "example": True, "temperature": 0.6},
    "E8": {"name": "no_example", "schema": "per_kind", "k": 5, "max_repairs": cv.MAX_REPAIRS, "example": False, "temperature": 0.6},
    "E9": {"name": "k1", "schema": "per_kind", "k": 1, "max_repairs": cv.MAX_REPAIRS, "example": True, "temperature": 0.6},
    "E10": {"name": "t0", "schema": "per_kind", "k": 5, "max_repairs": cv.MAX_REPAIRS, "example": True, "temperature": 0.0},
}
PREDICTED = {"E6": "fewer valid draws", "E7": "fewer valid draws", "E8": "fewer valid draws",
             "E9": "reliability = single-draw hit rate", "E10": "reported whatever it is (not predicted)"}


def flat_schema():
    """The earlier output schema, which required only `kind` and `table` for each part (ablation E6)."""
    return json.loads(FLAT_SCHEMA.read_text())


def schema_for(variant):
    return flat_schema() if VARIANTS[variant]["schema"] == "flat" else cv.SPEC_SCHEMA


def language_text(with_example=True):
    if with_example:
        return ev.SPEC_LANGUAGE
    txt = ev.SPEC_LANGUAGE
    i, j = txt.index("### Abstract example"), txt.index("### Output")
    return txt[:i] + txt[j:]


def compile_prompt(docs_dir, with_example=True):
    """compiler.compile_prompt with the manual text as a parameter (identical text when with_example)."""
    docs = Path(docs_dir)
    return (cv.COMPILE_PREAMBLE + "\n" + language_text(with_example) + "\n\n# Application documents\n\n" +
            (docs / "policy.md").read_text() + "\n\n" + (docs / "schema.md").read_text())


def compile_spec(llm, prompt, schema, max_repairs):
    """The loop of compiler.compile_spec (same messages, same repair text, same records) with parameters."""
    msgs = [{"role": "system", "content": "You configure software precisely. Reply with one JSON object."},
            {"role": "user", "content": prompt}]
    attempts, spec, out = [], None, None
    for rnd in range(max_repairs + 1):
        out = llm.chat(msgs, schema=schema)
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
            msgs.append({"role": "user", "content": cv.REPAIR.format(error=str(exc)[:300])})
    return spec, out, attempts, False


def identity_checks(docs_dir):
    """Recorded facts: the default-parameter prompt equals the compiler's prompt; the default schema is the
    compiler's schema object; the flat schema differs from it; the no-example manual is shorter."""
    prompt = cv.compile_prompt(docs_dir)
    return {"default_prompt_identical": compile_prompt(docs_dir, True) == prompt,
            "no_example_chars_removed": len(prompt) - len(compile_prompt(docs_dir, False)),
            "default_schema_is_compiler_schema": schema_for("E7") is cv.SPEC_SCHEMA,
            "flat_schema_differs": flat_schema() != cv.SPEC_SCHEMA}
