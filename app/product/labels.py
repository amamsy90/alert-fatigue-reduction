"""Labels from on-call marks, and where a batch's labels came from."""
from __future__ import annotations

from collections import defaultdict

import pandas as pd

from src.entities import Alert, Fault

from .store import QueueStore

FEEDBACK_PREFIX = "fb_"


def apply_feedback(alerts: list[Alert], store: QueueStore) -> dict[str, int]:
    """Label alerts from on-call marks: inside a Real window is a fault, and an
    alert marked Noise is noise even inside a window. Existing labels are kept."""
    faults = [
        Fault(
            fault_id=f["fault_id"],
            start=pd.Timestamp(f["start"]),
            end=pd.Timestamp(f["end"]),
            component=f["component"],
        )
        for f in store.feedback_faults()
    ]
    noise = store.feedback_noise_alerts()
    labelled = overridden = 0
    by_service: dict[str, list[Fault]] = defaultdict(list)
    for fault in faults:
        by_service[fault.component].append(fault)
    for alert in alerts:
        if (alert.source, alert.timestamp.value) in noise:
            if alert.fault_id is not None:
                overridden += 1
            alert.fault_id = None
            continue
        if alert.fault_id is None:
            for fault in by_service.get(alert.source, []):
                if fault.start <= alert.timestamp <= fault.end:
                    alert.fault_id = fault.fault_id
                    labelled += 1
                    break
    return {"labelled_by_real_marks": labelled, "cleared_by_noise_marks": overridden}


def label_source(alerts: list[Alert]) -> str:
    kinds = set()
    for alert in alerts:
        if alert.fault_id is None:
            continue
        if alert.fault_id == "weak":
            kinds.add("weak_severity")
        elif str(alert.fault_id).startswith(FEEDBACK_PREFIX):
            kinds.add("feedback")
        else:
            kinds.add("real")
    if "weak_severity" in kinds:
        return "weak_severity"
    return "+".join(sorted(kinds)) or "none"
