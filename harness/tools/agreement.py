"""Tool 10: agreement between two labelers (Cohen's kappa with a bootstrap CI)."""

from __future__ import annotations

import math
from collections import Counter

import numpy as np
from sklearn.metrics import cohen_kappa_score

ORDER = {"no": 0, "partial": 1, "yes": 2}


def _kappa(a: list[str], b: list[str], weights):
    if len(set(a) | set(b)) < 2:
        return float("nan")  # both raters used a single identical label: kappa undefined
    return float(cohen_kappa_score(a, b, weights=weights, labels=sorted(set(a) | set(b), key=lambda x: ORDER.get(x, 9))))


def compute_agreement(labels_a: dict, labels_b: dict, ordinal: bool = False, n_boot: int = 1000, seed: int = 0) -> dict:
    """Measure how much two labelers agree, correcting for chance.

    When to use: human vs human (does the rule make sense), judge vs human
    consensus (is the judge trustworthy), judge run vs judge run (is it stable),
    simulator A vs simulator B scores.
    When NOT to use: for free-text notes, or for items labeled by only one side.

    Args:
        labels_a: {item_id: label}. Labels such as "yes" | "partial" | "no".
        labels_b: same item ids; items missing from either side, or "na" on
            either side, are dropped and counted in "n_dropped".
        ordinal: True for weighted kappa when labels are ordered (no < partial < yes).
        n_boot: bootstrap resamples for the 95% interval.

    Returns:
        {"ok": true, "n": int, "n_dropped": int, "raw_agreement": float,
         "kappa": float | null, "ci95": [lo, hi] | null, "confusion": {...},
         "label_counts": {...}, "warnings": [...]}
    """
    if not isinstance(labels_a, dict) or not isinstance(labels_b, dict):
        return {"ok": False, "error": "labels_a and labels_b must be {item_id: label} objects"}
    keys = sorted(set(labels_a) | set(labels_b))
    pairs = [(labels_a[k], labels_b[k]) for k in keys if k in labels_a and k in labels_b and "na" not in (labels_a[k], labels_b[k])]
    n_dropped = len(keys) - len(pairs)
    warnings = []
    if not pairs:
        return {"ok": True, "n": 0, "n_dropped": n_dropped, "raw_agreement": None, "kappa": None, "ci95": None,
                "confusion": {}, "label_counts": {}, "warnings": ["no overlapping labeled items"]}
    a, b = [p[0] for p in pairs], [p[1] for p in pairs]
    weights = "linear" if ordinal else None
    k = _kappa(a, b, weights)
    raw = sum(x == y for x, y in pairs) / len(pairs)
    rng = np.random.default_rng(seed)
    boots = []
    idx = np.arange(len(pairs))
    for _ in range(n_boot):
        s = rng.choice(idx, size=len(idx), replace=True)
        v = _kappa([a[i] for i in s], [b[i] for i in s], weights)
        if not math.isnan(v):
            boots.append(v)
    ci = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))] if len(boots) >= 20 else None
    counts = Counter(a + b)
    if len(pairs) < 30:
        warnings.append("n < 30: interval is wide")
    if counts and max(counts.values()) / sum(counts.values()) > 0.85:
        warnings.append("label prevalence skewed; kappa may be misleadingly low")
    if math.isnan(k):
        warnings.append("kappa undefined: only one label used")
    confusion = {f"{x}->{y}": c for (x, y), c in Counter(pairs).items()}
    return {
        "ok": True, "n": len(pairs), "n_dropped": n_dropped, "raw_agreement": round(raw, 3),
        "kappa": None if math.isnan(k) else round(k, 3), "ci95": [round(c, 3) for c in ci] if ci else None,
        "confusion": confusion, "label_counts": dict(counts), "warnings": warnings,
    }
