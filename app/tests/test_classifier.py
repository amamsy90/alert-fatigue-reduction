"""Classifier beats the threshold baseline on data where faults look different."""
from pathlib import Path

from src.baseline import baseline_decisions
from src.classifier import AlertClassifier, classifier_decisions
from src.config import Config
from src.ingestion import align_alerts_to_faults, load_alerts, load_faults, time_split
from src.metrics import classification_pr
from src.sample_data import generate


def test_classifier_beats_baseline_on_sample(tmp_path: Path):
    generate(tmp_path, seed=42, days=14)
    alerts = load_alerts(tmp_path / "alerts.csv")
    faults = load_faults(tmp_path / "faults.csv")
    alerts, faults = align_alerts_to_faults(alerts, faults)
    config = Config(budget_k=20)
    train_alerts, test_alerts = time_split(alerts, config.split_day)
    _, test_faults = time_split(faults, config.split_day)

    model = AlertClassifier(config).fit(train_alerts)
    learned = classifier_decisions(test_alerts, model.predict_proba(test_alerts), config)
    base = baseline_decisions(test_alerts, config)

    learned_pr = classification_pr(learned, test_alerts)
    base_pr = classification_pr(base, test_alerts)
    assert learned_pr["recall"] > base_pr["recall"]
    assert learned_pr["precision"] >= base_pr["precision"]
    assert test_faults  # the held-out week contains injected faults
