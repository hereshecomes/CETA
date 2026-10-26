"""Aggregate scored runs. Primary endpoint: on answerable cases, success = correct verdict AND
sufficient evidence; computed per latent class (authorized / unauthorized) and averaged with equal
weight. All statuses stay in the denominators (infra failures and invalid outputs count as failures
and are also reported separately). Paired differences use a cluster bootstrap over base events.

Usage: analyze.py RESULTS_JSONL [RESULTS_JSONL ...] [--ref NAME]
"""
import argparse
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


def load(path):
    recs = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    name = Path(path).parent.name
    return name, recs


def rate(xs):
    xs = list(xs)
    return (sum(xs), len(xs))


def fmt(t):
    return f"{t[0]}/{t[1]} ({100.0 * t[0] / t[1]:.1f}%)" if t[1] else "n/a"


def primary(recs):
    ans = [r["score"] for r in recs if r["score"]["answerable"]]
    per = {}
    for lt in ("authorized", "unauthorized"):
        s = [x["success"] for x in ans if x["latent_truth"] == lt]
        per[lt] = sum(s) / len(s) if s else float("nan")
    return (per["authorized"] + per["unauthorized"]) / 2


def summary(recs):
    sc = [r["score"] for r in recs]
    ans = [x for x in sc if x["answerable"]]
    una = [x for x in sc if not x["answerable"]]
    out = {
        "n_cases": len(recs),
        "status": dict(Counter(r["status"] for r in recs)),
        "primary_balanced_success": round(primary(recs), 4),
        "success_authorized": fmt(rate(x["success"] for x in ans if x["latent_truth"] == "authorized")),
        "success_unauthorized": fmt(rate(x["success"] for x in ans if x["latent_truth"] == "unauthorized")),
        "verdict_correct_answerable": fmt(rate(x["verdict_correct"] for x in ans)),
        "evidence_sufficient_answerable": fmt(rate(x["evidence_sufficient"] for x in ans)),
        "abstain_on_answerable": fmt(rate(x["abstained"] for x in ans)),
        "false_escalation_on_authorized": fmt(rate(x["false_escalation"] for x in ans if x["latent_truth"] == "authorized")),
        "unanswerable_correct_abstain": fmt(rate(x["success"] for x in una)),
        "unanswerable_unsupported_determination": fmt(rate(x["unsupported_determination"] for x in una)),
        "hallucinated_citations_cases": fmt(rate(x["n_hallucinated_rows"] > 0 for x in sc)),
        "by_pattern_success": {}, "by_variant_success": {},
    }
    for key, field in (("by_pattern_success", "pattern"), ("by_variant_success", "variant")):
        g = defaultdict(list)
        for x in sc:
            g[x[field]].append(x["success"])
        out[key] = {k: fmt(rate(v)) for k, v in sorted(g.items())}
    llm = [r["llm_cost"] for r in recs if r.get("llm_cost")]
    if llm:
        n = len(llm)
        out["mean_llm_calls"] = round(sum(c["llm_calls"] for c in llm) / n, 2)
        out["mean_prompt_tokens"] = round(sum(c["prompt_tokens"] for c in llm) / n)
        out["mean_completion_tokens"] = round(sum(c["completion_tokens"] for c in llm) / n)
        out["infra_retries_total"] = sum(c["infra_retries"] for c in llm)
    out["mean_queries"] = round(sum(r["tool_cost"]["queries"] for r in recs) / len(recs), 2)
    out["mean_wall_s"] = round(sum(r["wall_s"] for r in recs) / len(recs), 2)
    return out


def unsupported_rate(recs):
    una = [r["score"] for r in recs if not r["score"]["answerable"]]
    return sum(x["unsupported_determination"] for x in una) / len(una) if una else float("nan")


def cluster_bootstrap_diff(a, b, draws=10000, seed=20261020):
    """Differences (a - b) in the primary endpoint and in the unanswerable unsupported-determination
    rate. Resamples base events with replacement; every variant and repetition of a sampled base event
    enters for both methods, so the comparison is paired at the base-event level."""
    ga, gb = defaultdict(list), defaultdict(list)
    for r in a:
        ga[r["score"]["base_id"]].append(r)
    for r in b:
        gb[r["score"]["base_id"]].append(r)
    bases = sorted(set(ga) & set(gb))
    rng = random.Random(seed)
    out = {"clusters": len(bases)}
    for name, fn in (("primary", primary), ("unanswerable_unsupported", unsupported_rate)):
        point = fn([r for k in bases for r in ga[k]]) - fn([r for k in bases for r in gb[k]])
        stats = []
        for _ in range(draws):
            sample = [rng.choice(bases) for _ in bases]
            stats.append(fn([r for k in sample for r in ga[k]]) - fn([r for k in sample for r in gb[k]]))
        stats.sort()
        out[name] = {"diff": round(point, 4),
                     "ci95": [round(stats[int(0.025 * draws)], 4), round(stats[int(0.975 * draws) - 1], 4)]}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--ref", default=None)
    args = ap.parse_args()
    runs = defaultdict(list)
    for p in args.paths:
        name, recs = load(p)
        runs[name.rsplit("_rep", 1)[0]].extend(recs)  # pool repetitions of the same method
    report = {name: summary(recs) for name, recs in runs.items()}
    if args.ref and args.ref in runs:
        report["paired_vs_" + args.ref] = {n: cluster_bootstrap_diff(recs, runs[args.ref])
                                           for n, recs in runs.items() if n != args.ref}
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
