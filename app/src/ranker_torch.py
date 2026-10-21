"""Tiny pairwise ranker. Training needs torch. Scoring is numpy, from the saved matrices."""
from __future__ import annotations

import numpy as np


def train_mlp(
    features: np.ndarray,
    labels: np.ndarray,
    groups: np.ndarray,
    *,
    hidden: int,
    epochs: int,
    seed: int,
    device: str,
) -> dict[str, np.ndarray]:
    """Pairwise softplus loss inside each day. Returns ``w1, b1, w2, b2`` as float32."""
    import torch
    from torch import nn

    torch.manual_seed(seed)
    x = torch.from_numpy(np.ascontiguousarray(features, dtype=np.float32)).to(device)
    width = int(x.shape[1])
    layer1 = nn.Linear(width, hidden).to(device)
    layer2 = nn.Linear(hidden, 1).to(device)
    opt = torch.optim.Adam(list(layer1.parameters()) + list(layer2.parameters()), lr=1e-2)

    def score(rows: torch.Tensor) -> torch.Tensor:
        return layer2(torch.relu(layer1(rows))).squeeze(-1)

    # A handful of (fault, not-fault) pairs per day. Days with one class contribute nothing.
    pairs: list[tuple[np.ndarray, np.ndarray]] = []
    rng = np.random.default_rng(seed)
    for day in np.unique(groups):
        rows = np.flatnonzero(groups == day)
        pos = rows[labels[rows] == 1]
        neg = rows[labels[rows] == 0]
        if len(pos) == 0 or len(neg) == 0:
            continue
        n_pairs = min(32, len(pos) * len(neg))
        left = rng.choice(pos, size=n_pairs, replace=True)
        right = rng.choice(neg, size=n_pairs, replace=True)
        pairs.append((left, right))
    if not pairs:
        raise ValueError("torch ranker needs a day with both a fault item and a false item")
    left = torch.from_numpy(np.concatenate([pair[0] for pair in pairs])).to(device)
    right = torch.from_numpy(np.concatenate([pair[1] for pair in pairs])).to(device)
    for _epoch in range(max(1, epochs)):
        opt.zero_grad(set_to_none=True)
        # softplus(-(s_pos - s_neg)) pushes fault items above the others in that day.
        loss = torch.nn.functional.softplus(-(score(x[left]) - score(x[right]))).mean()
        loss.backward()
        opt.step()
    return {
        "w1": np.ascontiguousarray(layer1.weight.detach().cpu().numpy(), dtype=np.float32),
        "b1": np.ascontiguousarray(layer1.bias.detach().cpu().numpy(), dtype=np.float32),
        "w2": np.ascontiguousarray(layer2.weight.detach().cpu().numpy(), dtype=np.float32),
        "b2": np.ascontiguousarray(layer2.bias.detach().cpu().numpy(), dtype=np.float32),
    }
