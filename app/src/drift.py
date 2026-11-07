"""Input drift: is what the queue scores now unlike what its model learned from?

A saved model is only as good as the data it saw. These checks compare a
reference window (the alerts the model trained on) with the current window (the
alerts being scored) using three standard, model-free-to-explain tests:

* PSI (population stability index) per feature: how far the share of alerts in
  each severity, source and alert type moved, and how far message length moved.
  By convention below 0.1 is stable, 0.1 to 0.25 a small shift, above 0.25 a large one.
* Two-sample Kolmogorov-Smirnov test on alerts per hour: did the arrival rate change?
* A classifier two-sample test on the message text: fit a logistic regression to
  tell reference messages from current ones and measure its cross-validated AUC.
  AUC near 0.5 means the two windows are indistinguishable; near 1.0 means the
  wording changed, and the terms with the largest weights show which.

Nothing here reads a label or a later window. `reference` is always earlier than
`current`, and no function looks at a fault, so a drift check cannot leak the
answer into the queue. It reports; it never changes a decision.
"""
from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

import numpy as np
from scipy.stats import ks_2samp
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict

from .entities import Alert
from .features import normalize_message

PSI_WARN = 0.25          # conventional "large shift" level
PSI_WATCH = 0.10         # conventional "small shift" level
TEXT_AUC_WARN = 0.75     # a classifier this good at telling the windows apart means the wording moved
KS_P_WARN = 0.01         # arrival-rate change this unlikely under no change
MIN_ALERTS = 30          # per window; fewer is not judged
MIN_TEXT_ALERTS = 40     # per window for the text test (it needs a cross-validation fit)
EPS = 1e-4


def psi_from_shares(reference: dict[str, float], current: dict[str, float]) -> float:
    """PSI between two share tables over the union of their categories."""
    total = 0.0
    for key in set(reference) | set(current):
        ref = max(reference.get(key, 0.0), EPS)
        cur = max(current.get(key, 0.0), EPS)
        total += (cur - ref) * math.log(cur / ref)
    return total


def category_psi(reference: Sequence[str], current: Sequence[str]) -> float:
    """PSI of a categorical feature (severity, source, alert type)."""
    def shares(values: Sequence[str]) -> dict[str, float]:
        counts = Counter(values)
        n = len(values)
        return {k: v / n for k, v in counts.items()}

    return psi_from_shares(shares(reference), shares(current))


def numeric_psi(reference: Sequence[float], current: Sequence[float], bins: int = 10) -> float:
    """PSI of a numeric feature. Bin edges are the reference deciles, so the
    reference puts about a tenth of its mass in each bin and the score measures
    how much of the current window left that shape."""
    ref = np.asarray(reference, dtype=float)
    cur = np.asarray(current, dtype=float)
    edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)[1:-1]))
    if edges.size == 0:  # a constant reference: any other value is a shift
        return 0.0 if np.all(cur == ref[0]) else math.log(1 / EPS)
    ref_idx = np.searchsorted(edges, ref, side="right")
    cur_idx = np.searchsorted(edges, cur, side="right")
    width = edges.size + 1
    ref_share = np.bincount(ref_idx, minlength=width) / ref.size
    cur_share = np.bincount(cur_idx, minlength=width) / cur.size
    return psi_from_shares(
        {str(i): float(s) for i, s in enumerate(ref_share)}, {str(i): float(s) for i, s in enumerate(cur_share)}
    )


def hourly_counts(alerts: Sequence[Alert]) -> np.ndarray:
    """Alerts per clock hour between the first and last alert (empty hours count as zero)."""
    if not alerts:
        return np.zeros(0)
    hours = [a.timestamp.floor("h") for a in alerts]
    start, end = min(hours), max(hours)
    n = int((end - start) / np.timedelta64(1, "h")) + 1
    counts = np.zeros(n)
    for h in hours:
        counts[int((h - start) / np.timedelta64(1, "h"))] += 1
    return counts


def text_drift(
    reference: Sequence[str], current: Sequence[str], *, seed: int = 0, folds: int = 3, top: int = 5
) -> dict | None:
    """Classifier two-sample test on message text. None when a window is too small.

    Returns the cross-validated AUC of a logistic regression that separates the
    windows and the terms that point most strongly at the current one.
    """
    if len(reference) < MIN_TEXT_ALERTS or len(current) < MIN_TEXT_ALERTS:
        return None
    texts = [normalize_message(t) for t in (*reference, *current)]
    labels = np.array([0] * len(reference) + [1] * len(current))
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)
    try:
        matrix = vec.fit_transform(texts)
    except ValueError:  # every term appears once: nothing to compare
        return {"auc": 0.5, "top_terms": []}
    clf = LogisticRegression(max_iter=200, class_weight="balanced", random_state=seed)
    cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=seed)
    # The vocabulary is fit on both windows (no labels involved), so the AUC is not
    # inflated by the label; only the classifier weights are fit inside each fold.
    scores = cross_val_predict(clf, matrix, labels, cv=cv, method="predict_proba")[:, 1]
    auc = float(roc_auc_score(labels, scores))
    clf.fit(matrix, labels)
    names = np.array(vec.get_feature_names_out())
    order = np.argsort(clf.coef_[0])[::-1][:top]
    terms = [str(names[i]) for i in order if clf.coef_[0][i] > 0]
    return {"auc": round(auc, 4), "top_terms": terms}


def drift_report(reference: Sequence[Alert], current: Sequence[Alert], *, seed: int = 0) -> dict:
    """Compare two alert windows. Status is ok, warn, or n/a when a window is too small.

    The dict is JSON-safe so a job can store it as it is.
    """
    if len(reference) < MIN_ALERTS or len(current) < MIN_ALERTS:
        return {"status": "n/a", "reason": f"needs at least {MIN_ALERTS} alerts in each window",
                "reference_alerts": len(reference), "current_alerts": len(current)}
    psi = {
        "severity": category_psi([a.severity for a in reference], [a.severity for a in current]),
        "source": category_psi([a.source for a in reference], [a.source for a in current]),
        "alert_type": category_psi([a.alert_type for a in reference], [a.alert_type for a in current]),
        "message_length": numeric_psi([len(a.message) for a in reference], [len(a.message) for a in current]),
    }
    ref_rate, cur_rate = hourly_counts(reference), hourly_counts(current)
    ks_p: float | None = None
    ks_stat: float | None = None
    if ref_rate.size >= 3 and cur_rate.size >= 3:
        result = ks_2samp(ref_rate, cur_rate)
        ks_stat, ks_p = float(result.statistic), float(result.pvalue)
    text = text_drift([a.message for a in reference], [a.message for a in current], seed=seed)

    reasons: list[str] = []
    for name, value in psi.items():
        if value >= PSI_WARN:
            reasons.append(f"{name} shifted (PSI {value:.2f} >= {PSI_WARN})")
    if ks_p is not None and ks_p < KS_P_WARN:
        reasons.append(f"alert arrival rate changed (KS p = {ks_p:.3g})")
    if text is not None and text["auc"] >= TEXT_AUC_WARN:
        reasons.append(f"message wording changed (classifier AUC {text['auc']:.2f} >= {TEXT_AUC_WARN})")
    return {
        "status": "warn" if reasons else "ok",
        "reasons": reasons,
        "reference_alerts": len(reference),
        "current_alerts": len(current),
        "psi": {k: round(v, 4) for k, v in psi.items()},
        "psi_max": round(max(psi.values()), 4),
        "rate_ks_statistic": None if ks_stat is None else round(ks_stat, 4),
        "rate_ks_p": None if ks_p is None else round(ks_p, 6),
        "text_auc": None if text is None else text["auc"],
        "text_top_terms": [] if text is None else text["top_terms"],
    }
