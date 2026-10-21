"""InfoNCE training for the contrastive template encoder.

Imported only when torch is installed. The state dict is numpy so the
decision-time encoder does not import torch.
"""
from __future__ import annotations

import numpy as np

from .log_encoder import N_FEATURES, featurize


def resolve_torch_device(requested: str) -> tuple[str, bool]:
    import torch

    want = (requested or "cpu").strip().lower()
    if want.startswith("cuda") and bool(torch.cuda.is_available()):
        index = want.removeprefix("cuda").removeprefix(":")
        device = "cuda" if index in ("", "0") else f"cuda:{index}"
        return device, False
    return "cpu", want.startswith("cuda")


def _model(dim: int, hidden: int):
    import torch
    from torch import nn

    class _Encoder(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.fc1 = nn.Linear(N_FEATURES, hidden)
            self.fc2 = nn.Linear(hidden, dim)

        def forward(self, features: torch.Tensor) -> torch.Tensor:
            hidden_state = torch.relu(self.fc1(features))
            return torch.tanh(self.fc2(hidden_state))

    return _Encoder()


def pair_matrix(groups: list[tuple[str, str]]) -> tuple[np.ndarray, np.ndarray]:
    """Two views of each template. The group id is an integer, not a feature."""
    features: list[np.ndarray] = []
    labels: list[int] = []
    keys: dict[str, int] = {}
    for template, group in groups:
        if group not in keys:
            keys[group] = len(keys)
        label = keys[group]
        features.append(featurize(template))
        features.append(featurize(template, salt="view2"))
        labels.extend((label, label))
    if not features:
        return np.zeros((0, N_FEATURES), dtype=np.float32), np.zeros((0,), dtype=np.int64)
    return np.stack(features).astype(np.float32), np.asarray(labels, dtype=np.int64)


def info_nce(embeddings, labels, temperature: float) -> object:
    """Supervised InfoNCE. Anchors with no other view of their group are skipped."""
    import torch
    from torch import nn

    if embeddings.shape[0] < 2:
        return embeddings.sum() * 0.0
    sim = embeddings @ embeddings.T / temperature
    sim = sim - sim.max(dim=1, keepdim=True).values.detach()
    self_mask = torch.eye(sim.shape[0], dtype=torch.bool, device=sim.device)
    sim = sim.masked_fill(self_mask, -1e9)
    log_prob = nn.functional.log_softmax(sim, dim=1)
    same = labels.unsqueeze(0) == labels.unsqueeze(1)
    positive = same & ~self_mask
    loss = embeddings.new_zeros(())
    counted = 0
    for index in range(sim.shape[0]):
        mask = positive[index]
        if not bool(mask.any()):
            continue
        loss = loss - log_prob[index][mask].mean()
        counted += 1
    if counted == 0:
        return embeddings.sum() * 0.0
    return loss / counted


def train_state(
    groups: list[tuple[str, str]],
    *,
    dim: int,
    hidden: int = 32,
    epochs: int = 20,
    seed: int = 42,
    device: str = "cpu",
    temperature: float = 0.2,
) -> dict[str, np.ndarray]:
    import torch

    features, labels = pair_matrix(groups)
    if len(labels) < 4 or len(set(labels.tolist())) < 2:
        raise ValueError("need at least two fault windows with a template")
    torch.manual_seed(seed)
    model = _model(dim, hidden).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-2)
    inputs = torch.from_numpy(features).to(device)
    targets = torch.from_numpy(labels).to(device)
    model.train()
    for _epoch in range(max(1, epochs)):
        opt.zero_grad(set_to_none=True)
        loss = info_nce(model(inputs), targets, temperature)
        loss.backward()
        opt.step()
    model.eval()
    fc1 = model.fc1
    fc2 = model.fc2
    return {
        "w1": np.ascontiguousarray(fc1.weight.detach().cpu().numpy(), dtype=np.float32),
        "b1": np.ascontiguousarray(fc1.bias.detach().cpu().numpy(), dtype=np.float32),
        "w2": np.ascontiguousarray(fc2.weight.detach().cpu().numpy(), dtype=np.float32),
        "b2": np.ascontiguousarray(fc2.bias.detach().cpu().numpy(), dtype=np.float32),
    }
