"""Small causal TCN for one-step metric forecasts.

Imported only when torch is installed. The module is optional: CPU CI does not
import it. Weights leave this file as a numpy state dict and are stored with
safetensors, not pickle.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np


def resolve_torch_device(requested: str) -> tuple[str, bool]:
    """Return ``(device, fell_back)``. ``cuda`` falls back to ``cpu`` when torch has no device."""
    import torch

    want = (requested or "cpu").strip().lower()
    if want.startswith("cuda") and bool(torch.cuda.is_available()):
        index = want.removeprefix("cuda").removeprefix(":")
        device = "cuda" if index in ("", "0") else f"cuda:{index}"
        return device, False
    return "cpu", want.startswith("cuda")


def _build(n_channels: int, hidden: int, kernel: int, dilations: tuple[int, ...]):
    import torch
    from torch import nn

    class _CausalConv1d(nn.Module):
        def __init__(self, in_channels: int, out_channels: int, kernel_size: int, dilation: int) -> None:
            super().__init__()
            self.pad = (kernel_size - 1) * dilation
            self.conv = nn.Conv1d(in_channels, out_channels, kernel_size, dilation=dilation)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            return self.conv(nn.functional.pad(x, (self.pad, 0)))

    class _Block(nn.Module):
        def __init__(self, channels: int, kernel_size: int, dilation: int) -> None:
            super().__init__()
            self.conv1 = _CausalConv1d(channels, channels, kernel_size, dilation)
            self.conv2 = _CausalConv1d(channels, channels, kernel_size, dilation)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            y = nn.functional.relu(self.conv1(x))
            return nn.functional.relu(x + self.conv2(y))

    class _MetricTCN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.proj = nn.Conv1d(n_channels, hidden, 1)
            self.blocks = nn.ModuleList([_Block(hidden, kernel, dilation) for dilation in dilations])
            self.head = nn.Linear(hidden, n_channels)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            hidden_state = self.proj(x)
            for block in self.blocks:
                hidden_state = block(hidden_state)
            # The last step is the newest context point. Nothing after it is read.
            return self.head(hidden_state[:, :, -1])

    return _MetricTCN()


def train_state(
    contexts: np.ndarray,
    targets: np.ndarray,
    *,
    n_channels: int,
    hidden: int,
    kernel: int,
    dilations: tuple[int, ...],
    epochs: int,
    seed: int,
    device: str,
) -> dict[str, np.ndarray]:
    """contexts ``(N, C, W)``, targets ``(N, C)``. Returns a float32 state dict."""
    import torch
    from torch import nn

    torch.manual_seed(seed)
    model = _build(n_channels, hidden, kernel, dilations).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.MSELoss()
    inputs = torch.from_numpy(np.ascontiguousarray(contexts, dtype=np.float32)).to(device)
    goals = torch.from_numpy(np.ascontiguousarray(targets, dtype=np.float32)).to(device)
    batch = 64
    model.train()
    for _epoch in range(max(1, epochs)):
        order = torch.randperm(inputs.shape[0], device=device)
        for start in range(0, inputs.shape[0], batch):
            rows = order[start : start + batch]
            opt.zero_grad(set_to_none=True)
            loss = loss_fn(model(inputs[rows]), goals[rows])
            loss.backward()
            opt.step()
    model.eval()
    state: dict[str, np.ndarray] = {}
    for name, value in model.state_dict().items():
        state[name] = np.ascontiguousarray(value.detach().cpu().numpy(), dtype=np.float32)
    return state


class TorchForecaster:
    """Callable ``(N, C, W) -> (N, C)`` used by the numpy scoring loop."""

    def __init__(self, model: object, device: str) -> None:
        self.model = model
        self.device = device

    def __call__(self, batch: np.ndarray) -> np.ndarray:
        import torch

        tensor = torch.from_numpy(np.ascontiguousarray(batch, dtype=np.float32)).to(self.device)
        module = self.model
        module.eval()  # type: ignore[attr-defined]
        with torch.no_grad():
            predicted = module(tensor).detach().cpu().numpy()  # type: ignore[operator]
        return np.asarray(predicted, dtype=np.float64)


def make_forecaster(
    state: dict[str, np.ndarray],
    *,
    n_channels: int,
    hidden: int,
    kernel: int,
    dilations: tuple[int, ...],
    device: str,
) -> Callable[[np.ndarray], np.ndarray]:
    import torch

    model = _build(n_channels, hidden, kernel, dilations)
    tensors = {name: torch.from_numpy(np.asarray(value)).to(device) for name, value in state.items()}
    model.load_state_dict(tensors)
    model.to(device)
    model.eval()
    return TorchForecaster(model, device)
