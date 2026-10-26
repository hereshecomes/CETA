"""Scorer for Weblate, Wagtail and Indico, whose private truth lists the required evidence explicitly.

Same semantics as score.py: on a decidable case success = correct verdict AND sufficient observed
evidence; on an undecidable case success = abstention, and a determination is an unsupported
determination. The only difference is how sufficiency is read from the truth record: score.py hard-codes
the pretix proof structure (creation_row_id + membership_proofs on audit_log / team_members); this module
reads `truth["proof_alternatives"]`, a list of alternatives, each a list of evidence items that must all
be cited. An item is either a row citation {"source": <table>, "id": <row id>} or a current-state claim
{"source": <table>, <key columns...>, "present": true|false}. Evidence the method never observed does not
count (a row citation counts only if the row was observed; any other item counts only if its table was
observed).
"""

VERDICTS = ("authorized", "unauthorized", "insufficient_evidence")


def _norm_id(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return str(v)


def _item_matches(required, e):
    if not isinstance(e, dict) or e.get("source") != required["source"]:
        return False
    if "id" in required:
        return "id" in e and _norm_id(e["id"]) == _norm_id(required["id"])
    for k, v in required.items():
        if k == "source":
            continue
        ev = e.get(k)
        if k == "present":
            ev = e.get("present", e.get("member"))
            if ev is None:
                return False
            ev = bool(ev)
        if str(ev) != str(v):
            return False
    return True


def _cited_rows(evidence, table):
    out = set()
    for e in evidence:
        if isinstance(e, dict) and e.get("source") == table and "id" in e:
            out.add(_norm_id(e["id"]))
    return out


def _observed_only(evidence, observed_ids, observed_tables, log_table):
    kept, dropped = [], 0
    for e in evidence:
        if not isinstance(e, dict) or not isinstance(e.get("source"), str):
            dropped += 1
        elif e.get("source") == log_table and "id" in e:
            ok = _norm_id(e.get("id")) in observed_ids
            kept.append(e) if ok else None
            dropped += 0 if ok else 1
        else:
            ok = e["source"] in observed_tables
            kept.append(e) if ok else None
            dropped += 0 if ok else 1
    return kept, dropped


def score(truth, submission, public_row_ids, observed_ids=None, observed_tables=None, log_table="audit_log"):
    """public_row_ids: ids of the operation/log table rows in the public view (hallucination check).
    observed_ids / observed_tables: what the method actually saw (SQLTool tracks ids for the table
    named audit_log and the set of tables read)."""
    sub = submission if isinstance(submission, dict) else {}
    verdict = sub.get("verdict") if sub.get("verdict") in VERDICTS else "invalid"
    evidence = sub.get("evidence") if isinstance(sub.get("evidence"), list) else []
    n_unobserved = 0
    if observed_ids is not None:
        evidence, n_unobserved = _observed_only(evidence, {_norm_id(i) for i in observed_ids},
                                                set(observed_tables or ()), log_table)
    cited = _cited_rows(evidence, log_table)
    hallucinated = sorted(cited - {_norm_id(i) for i in public_row_ids}, key=str)
    rec = {
        "case_id": truth["case_id"], "split": truth["split"], "base_id": truth["base_id"],
        "pattern": truth["pattern"], "variant": truth["variant"], "answerable": truth["answerable"],
        "latent_truth": truth["latent_truth"], "expected": truth["expected_verdict"], "verdict": verdict,
        "verdict_correct": verdict == truth["expected_verdict"],
        "n_cited_rows": len(cited), "n_hallucinated_rows": len(hallucinated),
        "n_unobserved_citations": n_unobserved,
    }
    if truth["answerable"]:
        sufficient = any(all(any(_item_matches(req, e) for e in evidence) for req in alt)
                         for alt in truth["proof_alternatives"])
        rec.update(evidence_sufficient=sufficient,
                   success=rec["verdict_correct"] and sufficient,
                   abstained=verdict == "insufficient_evidence",
                   false_escalation=truth["latent_truth"] == "authorized" and verdict == "unauthorized")
    else:
        rec.update(success=verdict == "insufficient_evidence",
                   unsupported_determination=verdict in ("authorized", "unauthorized"))
    return rec
