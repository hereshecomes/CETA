"""Scorer for pretix: independent scoring from the private truth (execution ledger), never from a
method's own checker.

On a decidable case, success = correct verdict AND sufficient observed evidence; on an undecidable case,
success = abstention, and any determination is an unsupported determination. Evidence items may name any
table as `source`; an item that cites a row by `id` counts only if the row was observed, any other item
counts only if its table was observed. A current-state claim may carry its flag as `member` or `present`.
"""

VERDICTS = ("authorized", "unauthorized", "insufficient_evidence")


def _cited_rows(evidence):
    out = set()
    for e in evidence:
        if isinstance(e, dict) and e.get("source") == "audit_log":
            try:
                out.add(int(e.get("id")))
            except (TypeError, ValueError):
                pass
    return out


def _claim_flag(e):
    for k in ("member", "present"):
        if k in e:
            return bool(e[k])
    return None


def _proof_satisfied(truth, evidence):
    rows = _cited_rows(evidence)
    for p in truth["membership_proofs"]:
        if "row_ids" in p and set(p["row_ids"]) <= rows:
            return True
        if "current_membership" in p:
            cm = p["current_membership"]
            for e in evidence:
                if (isinstance(e, dict) and e.get("source") == "team_members"
                        and e.get("team_id") == cm["team_id"] and e.get("user_id") == cm["user_id"]
                        and _claim_flag(e) == cm["member"]):
                    return True
    return False


def _observed_only(evidence, observed_ids, observed_tables):
    kept, dropped = [], 0
    for e in evidence:
        if not isinstance(e, dict) or not isinstance(e.get("source"), str):
            dropped += 1
        elif e.get("source") == "audit_log":
            try:
                ok = int(e.get("id")) in observed_ids
            except (TypeError, ValueError):
                ok = False
            (kept.append(e) if ok else None)
            dropped += 0 if ok else 1
        else:
            ok = e["source"] in observed_tables
            (kept.append(e) if ok else None)
            dropped += 0 if ok else 1
    return kept, dropped


def score(truth, submission, public_row_ids, observed_ids=None, observed_tables=None):
    """observed_ids / observed_tables: what the method actually saw. Evidence it never observed
    does not count toward sufficiency (and is reported)."""
    sub = submission if isinstance(submission, dict) else {}
    verdict = sub.get("verdict") if sub.get("verdict") in VERDICTS else "invalid"
    evidence = sub.get("evidence") if isinstance(sub.get("evidence"), list) else []
    n_unobserved = 0
    if observed_ids is not None:
        evidence, n_unobserved = _observed_only(evidence, set(observed_ids), set(observed_tables or ()))
    cited = _cited_rows(evidence)
    hallucinated = sorted(cited - set(public_row_ids))
    rec = {
        "case_id": truth["case_id"], "split": truth["split"], "base_id": truth["base_id"],
        "pattern": truth["pattern"], "variant": truth["variant"], "answerable": truth["answerable"],
        "latent_truth": truth["latent_truth"], "expected": truth["expected_verdict"], "verdict": verdict,
        "verdict_correct": verdict == truth["expected_verdict"],
        "n_cited_rows": len(cited), "n_hallucinated_rows": len(hallucinated),
        "n_unobserved_citations": n_unobserved,
    }
    if truth["answerable"]:
        binding = truth["creation_row_id"] in cited
        proof = _proof_satisfied(truth, evidence)
        rec.update(issuer_binding_cited=binding, membership_proof_cited=proof,
                   evidence_sufficient=binding and proof,
                   success=rec["verdict_correct"] and binding and proof,
                   abstained=verdict == "insufficient_evidence",
                   false_escalation=truth["latent_truth"] == "authorized" and verdict == "unauthorized")
    else:
        rec.update(success=verdict == "insufficient_evidence",
                   unsupported_determination=verdict in ("authorized", "unauthorized"))
    return rec
