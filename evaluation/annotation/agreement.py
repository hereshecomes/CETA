"""Agreement among the four annotators (A1-A4): full agreement, Fleiss' kappa, Krippendorff's alpha (nominal) and
pairwise Cohen's kappa, for the world verdicts, the view verdicts and the decidability of the views.

Reads audit_results/human_annotation/annotations_{worlds,views}.csv and writes agreement.json next to them.
"""
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd

D = Path(__file__).resolve().parents[2] / "audit_results" / "human_annotation"


def fleiss(table):
    """table: items x categories counts (each row sums to n raters)."""
    t = np.asarray(table, float)
    n = t.sum(1)[0]
    p_j = t.sum(0) / t.sum()
    P_i = ((t * (t - 1)).sum(1)) / (n * (n - 1))
    Pbar, Pe = P_i.mean(), (p_j ** 2).sum()
    return (Pbar - Pe) / (1 - Pe) if Pe < 1 else 1.0


def cohen(a, b):
    cats = sorted(set(a) | set(b))
    a, b = np.asarray(a), np.asarray(b)
    po = (a == b).mean()
    pe = sum((a == c).mean() * (b == c).mean() for c in cats)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def kripp_nominal(units):
    """units: list of lists of labels (all present). Krippendorff alpha, nominal."""
    cats = sorted({x for u in units for x in u})
    idx = {c: i for i, c in enumerate(cats)}
    o = np.zeros((len(cats), len(cats)))
    for u in units:
        m = len(u)
        if m < 2:
            continue
        for i, j in itertools.permutations(range(m), 2):
            o[idx[u[i]], idx[u[j]]] += 1 / (m - 1)
    n_c = o.sum(1)
    n = n_c.sum()
    do = (o.sum() - np.trace(o)) / n
    de = (n * n - (n_c ** 2).sum()) / (n * (n - 1))
    return 1 - do / de if de > 0 else 1.0


def agree(df, key, cats):
    piv = df.pivot_table(index=key, columns="annotator", values="verdict", aggfunc="first")
    labels = piv.values.tolist()
    counts = np.array([[row.count(c) for c in cats] for row in labels])
    full = float(np.mean([len(set(r)) == 1 for r in labels]))
    pairs = {f"{a}-{b}": cohen(piv[a], piv[b]) for a, b in itertools.combinations(piv.columns, 2)}
    return dict(n=len(piv), full_agreement=full, fleiss=fleiss(counts), alpha=kripp_nominal(labels),
                cohen_min=min(pairs.values()), cohen_max=max(pairs.values()), pairs=pairs)


def main():
    w = pd.read_csv(D / "annotations_worlds.csv")
    v = pd.read_csv(D / "annotations_views.csv")
    v["dec"] = np.where(v.verdict == "insufficient_evidence", "und", "dec")
    out = {}
    for app in ["all", "pretix", "weblate", "wagtail", "indico"]:
        ww = w if app == "all" else w[w.app == app]
        vv = v if app == "all" else v[v.app == app]
        out[app] = dict(
            world_verdict=agree(ww, ["app", "world_id"], ["authorized", "unauthorized"]),
            view_verdict=agree(vv, ["app", "world_id", "view"], ["authorized", "unauthorized", "insufficient_evidence"]),
            view_decidability=agree(vv.assign(verdict=vv.dec), ["app", "world_id", "view"], ["dec", "und"]),
        )
    (D / "agreement.json").write_text(json.dumps(out, indent=1))
    for app, r in out.items():
        print(app, {k: (round(x["full_agreement"], 4), round(x["fleiss"], 4), round(x["alpha"], 4), x["n"])
                    for k, x in r.items()})


if __name__ == "__main__":
    main()
