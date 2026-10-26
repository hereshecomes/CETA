# CETA: Compiled Event-Time Audits for Web Applications

Anonymous artifact for the paper *Permitted Is Not Authorized: Compiled Event-Time Audits for Web
Applications*. It contains the core code of CETA and of every baseline, the test bed that executes the four
applications, the audit rules (policies, schemas and every compiled specification) and the audit results
reported in the paper.

CETA splits a post hoc authorization audit into two steps. Once per application, a language model reads the
organization's `policy.md` and `schema.md` and compiles them into a specification in a small language of
temporal memberships, credential bindings and prior approvals. A deterministic evaluator then decides every
alert from the application's records under event-time, coverage-bounded three-valued semantics, returning
`authorized`, `unauthorized` or `insufficient_evidence` together with the rows it relied on.

## Repository layout

```
methods/                         CETA, the baselines and the shared runner
  evaluator.py                   specification language (SPEC_LANGUAGE), validator, three-valued evaluator
  evaluator_ablations.py         evaluator with the ablation switches E3-E5 (E1/E2 are flags of evaluator.py)
  compiler.py                    LLM compiler: prompt, per-kind JSON output schema, repair loop
  compiler_ablations.py          compiler ablations E6-E10
  spec_schema_flat.json          flat output schema used by ablation E6
  sqltool.py                     shared read-only SQL tool (budget, row cap, observation log)
  llm.py                         client for an OpenAI-compatible vLLM server
  backbones.json                 model profiles (served name, thinking flag, template arguments)
  app_profile.py, app_profile_ext.py   per-application strings, schemas and paths
  agents.py                      G1 SQL agent, G2 hypothesis agent (+ informed variants)
  agents_reflect_timeline.py     G3 explore-reflect agent, G4 timeline agent (+ informed variants)
  pool_judge.py                  J evidence-pool judge (+ informed variant)
  codegen.py, codegen_runner.py  P program synthesis and its restricted runner
  codegen_sens.py                sensitivity variants S1 / S2 of P
  rules.py, rules_weblate.py, rules_wagtail.py, rules_indico.py   expert rules R and their weak variants
  run_method.py                  runs one method on one split and scores every case
  run_method_docs.py             run_method.py with replaced documents (ablations E11 / E12 / E14)
evaluation/
  score.py, score_generic.py     scorers (correct-and-grounded, unsupported verdicts)
  analyze.py                     metrics and paired cluster bootstrap over base events
  compile_and_select.py          K draws + development selection of CETA for one application and model
  compile_ablation_run.py        compiler ablations E6-E10
  docs_ablation_prepare.py       modified documents for E11 / E12 / E14
  compile_docs_ablation_run.py   compile-and-select on modified documents
  codegen_run.py                 generation, selection and test runs of P
  coverage_sweep.py              coverage sweep of RQ5
  gate0_checks*.py               validity checks G0.4-G0.7 of the four test beds
  annotation/make_packets.py     blinded annotation packets
  annotation/agreement.py        inter-annotator agreement
harness/                         test bed: executes worlds through each application and exports cases
  worlds.py, run_world.py, export_case.py, build_cases.py, export_all.py, pretix_env.py   pretix
  weblate/, wagtail/, indico/    the same for Weblate, Wagtail and Indico
audit_rules/<app>/               policy.md, schema.md, compiled specifications, candidate table
audit_results/                   results of the paper (see below)
cases/                           four sample test cases per application (the full sets are built by harness/)
```

## Requirements

- Python 3.12. The method and evaluation code needs `requests`; `evaluation/annotation/agreement.py` also
  needs `numpy` and `pandas` (`pip install -r requirements.txt`).
- Models are served locally with vLLM through its OpenAI-compatible API; `methods/backbones.json` lists the
  seven models, their served names, ports and template arguments. The deterministic methods (CETA's evaluator,
  the expert rules and their variants) need no model.
- The test bed runs each application in process, each in its own virtual environment: pretix 2026.8.0 (SQLite),
  Weblate 2026.10.1 (PostgreSQL through `pgserver`), Wagtail 8.0.1 (SQLite) and Indico 3.3.14 (PostgreSQL and
  Redis). Executed worlds and the environments of Weblate, Wagtail and Indico live under a data directory set by
  `CETA_DATA` (default `./data`), with `envs/<app>/` for the virtual environments and `worlds/<app>/` for the
  executed worlds; `CETA_TMP` (default `/tmp/ceta`) holds regenerable databases. pretix uses `app_env/venv` and the
  migrated template `app_env/template/template.sqlite3`.

## Quick start (no model needed)

The sample cases in `cases/` let the deterministic methods run immediately:

```bash
# expert rules R on the four sample test cases of Indico
python methods/run_method.py rules test demo --app indico

# CETA with the specification that Qwen3-32B compiled and the development cases selected
python methods/run_method.py compiled test demo --app indico \
    --spec audit_rules/indico/specifications/qwen3-32b/spec_1.json

# an evaluator ablation (E3: no reasoning from the first change after t)
python methods/run_method.py compiled_no_after test demo --app indico \
    --spec audit_rules/indico/specifications/qwen3-32b/spec_1.json

python evaluation/analyze.py runs/demo/*/results.jsonl
```

Each run writes `runs/<run>/<method>_rep0/results.jsonl` (one scored record per case) and one trajectory file per
case. The selected specification of every model and application is marked in `audit_rules/<app>/candidates.json`.

## Reproducing the experiments

1. **Build the test bed.** `python harness/build_cases.py` (pretix) and `python harness/<app>/build_cases.py`
   (Weblate, Wagtail, Indico) generate the world specifications, execute every world through the application's
   own API, and export the public view (`cases/.../public/<case>/episode.sqlite`, `alert.json`) and the private
   truth from the execution ledger (`cases/.../private/<case>.json`, `index.json`) for the windows V0, V1a and
   V1b. Weblate and Indico first need a template database (`harness/weblate/migrate_template.py`,
   `harness/indico/prepare_template.py`). `evaluation/gate0_checks*.py` runs the validity checks. Case
   identifiers are opaque random identifiers; a rebuilt test bed receives new ones, so match cases across builds
   by `world_id` and window, which every record in `audit_results/per_case_qwen3-32b.jsonl` carries.
2. **Compile and select.** `python evaluation/compile_and_select.py RUN --app APP --backbone MODEL` draws K = 5
   specifications from the two documents, evaluates every valid one on the development cases and selects the one
   with the highest development c&g (ties: lowest index).
3. **Run the methods on the test split.**
   `python methods/run_method.py METHOD test RUN --app APP --backbone MODEL [--spec FILE | --code FILE]`.

   | Method name | Paper |
   |---|---|
   | `compiled` | CETA (C) |
   | `rules`, `rules_closed_world`, `rules_current_state` | R and its closed-world / current-state variants |
   | `rules_no_time_limit`, `rules_no_separation_of_duty` | weak rule variants on Wagtail |
   | `sql_agent`, `hypothesis_verify`, `explore_reflect`, `timeline_agent` | G1, G2, G3, G4 |
   | `<agent>_informed` | G* informed (the evaluator's semantics added in words) |
   | `pool_judge`, `pool_judge_informed` | J, J informed |
   | `codegen` (with `--code`) | P |
   | `compiled_closed_world`, `compiled_current_state`, `compiled_no_after`, `compiled_no_fallback`, `compiled_no_coverage` | E1-E5 |

   The strongest agent G* was fixed on the development cases: G4 on pretix and Weblate, G3 on Wagtail, G1 on Indico.
4. **Analyze.** `python evaluation/analyze.py RUN_DIR/*/results.jsonl --ref compiled` reports c&g, the unsupported
   rate and paired differences with 95% intervals from a bootstrap clustered by base event (10,000 draws, seed
   20261020).
5. **Ablations and analyses.**
   - compiler (E6-E10): `python evaluation/compile_ablation_run.py RUN --app APP --variant E6`;
   - documents (E11 / E12 / E14): `python evaluation/docs_ablation_prepare.py static` (E11, E14) or `gen-e12` /
     `accept-e12`, then `evaluation/compile_docs_ablation_run.py` and `methods/run_method_docs.py --docs DIR ...`;
   - budget (E15): `--max-queries 6|12|24`;
   - program synthesis: `python evaluation/codegen_run.py RUN --app APP`, variants with `methods/codegen_sens.py`;
   - coverage sweep (RQ5): `python evaluation/coverage_sweep.py --app pretix`, then any method with
     `--cases runs/coverage_sweep/pretix/offset_<o>`.
6. **Human annotation.** `evaluation/annotation/make_packets.py` builds the blinded packets;
   `evaluation/annotation/agreement.py` recomputes the agreement statistics from the released annotations.

## Audit rules (`audit_rules/<app>/`)

| File | Content |
|---|---|
| `policy.md`, `schema.md` | the organization's policy and the schema documentation, the only inputs of the compiler, the agents, the judge and P |
| `specifications/<model>/spec_<i>.json` | every valid specification compiled by each of the seven models (K = 5 per model; 122 of 140 candidates are valid) |
| `candidates.json` | all 35 candidates of the application: validity, repairs, development and test c&g, unsupported verdicts, the selected candidate |
| `reference_spec.json` | hand-written reference specification used only by the validity check G0.6 (Weblate, Wagtail, Indico) |

The expert rules are code: `methods/rules*.py`.

## Audit results (`audit_results/`)

| File | Content |
|---|---|
| `main_results.json`, `main_results.csv` | every method on every application with all seven models: c&g, unsupported rate, class counts, terminal statuses, correct outcomes by pattern and window, cost, and the paired difference to CETA with 95% intervals |
| `per_case_qwen3-32b.jsonl` | 12,108 scored test cases with Qwen3-32B: method, case, world, window, pattern, truth, verdict, grading, cost |
| `confirmatory_tests.json` | the confirmatory tests Ha and Hb per application, the core / test-only splits and the two earlier protocols |
| `paired_contrasts.json` | contrasts between methods that share the same knowledge (investigation, inference, language) |
| `evaluator_ablations.json` | E1-E5 and the weak rule variants, with the changed cases on Wagtail and Indico |
| `compiler_ablations.json` | E6-E10: development c&g of every candidate, selection and test c&g |
| `document_budget_ablations.json` | E11, E12, E14 and the query budgets of E15 on pretix and Weblate |
| `coverage_sweep.csv` | RQ5: decidable share, abstention, c&g and unsupported verdicts per coverage start |
| `query_traces.json` | query traces of the four agents (proof availability per query, stopping) |
| `human_annotation/` | the annotation guide, the labels of the four annotators A1-A4 for all 284 worlds and 852 views, agreement statistics, the comparison with the execution ledger, and the Weblate results under the annotators' majority |

Method codes in `per_case_qwen3-32b.jsonl`: C (CETA), R, Rcw (closed world), Rcs (current state), Rnt / Rns (Wagtail
weak rules), G1-G4, G*inf (G* informed), J, Jinf (J informed), P, E1-E5. c&g averages the success rates of the
authorized and the unauthorized decidable cases; a decidable case succeeds when the verdict is correct and the
method observed and cited the record that identifies the principal and a set of proving rows. The unsupported
rate is the share of undecidable cases that receive `authorized` or `unauthorized`.

The annotation files are keyed by the packet labels (for example `IN-001`, view `A`), under which the annotators
saw the worlds; `ledger_agreement.json` gives their agreement with the execution ledger per application.
