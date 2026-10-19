"""XGBoost ``rank:ndcg`` fit, converted to numeric trees. Optional; CPU CI does not import xgboost."""
from __future__ import annotations

import json

import numpy as np

from .accel import AccelUnavailable, cuda_available, is_cuda_device, trees_from_xgb_dump


def fit_xgboost_ranker(
    features: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    *,
    seed: int,
    device: str,
) -> tuple[list[np.ndarray], float, str]:
    """Fit LambdaMART-style trees. Rows are sorted by day so group sizes are contiguous.

    ``device="cuda"`` is refused when the saved booster is not ``cuda`` or ``cuda:N``,
    the same check as the alert-classifier GPU path. The caller can retry on CPU.
    """
    import xgboost as xgb

    order = np.argsort(np.asarray(groups), kind="mergesort")
    ordered_x = np.ascontiguousarray(features[order], dtype=np.float32)
    ordered_y = np.asarray(labels[order], dtype=np.float32)
    ordered_groups = np.asarray(groups[order])
    sizes: list[int] = []
    start = 0
    for index in range(1, len(ordered_groups) + 1):
        if index == len(ordered_groups) or ordered_groups[index] != ordered_groups[start]:
            sizes.append(index - start)
            start = index
    want_cuda = device.startswith("cuda") and cuda_available()
    params = {
        "objective": "rank:ndcg",
        "tree_method": "hist",
        "device": "cuda" if want_cuda else "cpu",
        "eta": 0.1,
        "max_depth": 3,
        "seed": seed,
        "verbosity": 0,
    }
    matrix = xgb.DMatrix(ordered_x, label=ordered_y)
    matrix.set_group(sizes)
    booster = xgb.train(params, matrix, num_boost_round=40)
    config = json.loads(booster.save_config())
    got = str(config["learner"]["generic_param"].get("device") or params["device"])
    if want_cuda and not is_cuda_device(got):
        raise AccelUnavailable(f"xgboost device is {got!r}, not cuda")
    dumps = list(booster.get_dump(dump_format="json"))
    raw_base = config["learner"]["learner_model_param"]["base_score"]
    trees, base = trees_from_xgb_dump(dumps, raw_base)
    return trees, float(base), got
