"""One message-passing layer that scores a service inside a call graph.

Imported only when torch is installed. CPU CI does not import this module.
PyTorch Geometric is used when it imports and the caller asked for it (or for
`auto`). Otherwise a plain torch layer sends each node's hidden state across
the recorded edges. Weights leave as a float32 numpy state dict.
"""
from __future__ import annotations

import numpy as np

from .trace_graph import CallGraph


def resolve_torch_device(requested: str) -> tuple[str, bool]:
    """Return ``(device, fell_back)``. ``cuda`` falls back to ``cpu`` when torch has no device."""
    import torch

    want = (requested or "cpu").strip().lower()
    if want.startswith("cuda") and bool(torch.cuda.is_available()):
        index = want.removeprefix("cuda").removeprefix(":")
        device = "cuda" if index in ("", "0") else f"cuda:{index}"
        return device, False
    return "cpu", want.startswith("cuda")


def resolve_gnn_backend(requested: str) -> tuple[str, bool]:
    """``plain``, or ``pyg`` when Geometric imports.

    ``auto`` uses Geometric when the import works. Asking for ``pyg`` when it
    does not import is a fallback. ``auto`` landing on the plain layer is the
    supported path, not a failed request.
    """
    want = (requested or "auto").strip().lower()
    if want == "plain":
        return "plain", False
    try:
        import torch_geometric.nn  # noqa: F401
    except ImportError:
        return "plain", want == "pyg"
    if want in {"auto", "pyg"}:
        return "pyg", False
    return "plain", False


def _plain(n_node: int, n_edge: int, hidden: int):
    import torch
    from torch import nn

    class _PlainMP(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.node = nn.Linear(n_node, hidden)
            self.edge = nn.Linear(hidden + n_edge, hidden)
            self.self_update = nn.Linear(hidden, hidden)
            self.out = nn.Linear(hidden, 1)

        def forward(self, x: torch.Tensor, edge_index: torch.Tensor, edge_attr: torch.Tensor) -> torch.Tensor:
            hidden_state = nn.functional.relu(self.node(x))
            size = hidden_state.shape[0]
            aggregated = hidden_state.new_zeros((size, hidden_state.shape[1]))
            if edge_index.numel() and edge_attr.numel():
                source = edge_index[0]
                dest = edge_index[1]
                message = nn.functional.relu(self.edge(torch.cat([hidden_state[source], edge_attr], dim=-1)))
                aggregated = aggregated.index_add(0, dest, message)
                counts = hidden_state.new_zeros((size, 1))
                counts = counts.index_add(0, dest, torch.ones(dest.shape[0], 1, device=dest.device))
                aggregated = aggregated / counts.clamp(min=1.0)
            updated = nn.functional.relu(self.self_update(hidden_state) + aggregated)
            return self.out(updated).squeeze(-1)

    return _PlainMP()


def _pyg(n_node: int, hidden: int):
    import torch
    from torch import nn
    from torch_geometric.nn import SAGEConv

    class _SAGE(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.node = nn.Linear(n_node, hidden)
            self.conv = SAGEConv(hidden, hidden)
            self.out = nn.Linear(hidden, 1)

        def forward(self, x: torch.Tensor, edge_index: torch.Tensor, edge_attr: torch.Tensor) -> torch.Tensor:
            del edge_attr  # Geometric SAGE uses the adjacency. Edge numbers stay in the node jumps.
            hidden_state = nn.functional.relu(self.node(x))
            hidden_state = nn.functional.relu(self.conv(hidden_state, edge_index))
            return self.out(hidden_state).squeeze(-1)

    return _SAGE()


def _build(backend: str, n_node: int, n_edge: int, hidden: int):
    if backend == "pyg":
        return _pyg(n_node, hidden)
    return _plain(n_node, n_edge, hidden)


def _tensors(graph: CallGraph, device: str):
    import torch

    node = torch.from_numpy(np.ascontiguousarray(graph.node_x, dtype=np.float32)).to(device)
    if graph.edge_index.size:
        edges = torch.from_numpy(np.ascontiguousarray(graph.edge_index, dtype=np.int64)).to(device)
        attr = torch.from_numpy(np.ascontiguousarray(graph.edge_attr, dtype=np.float32)).to(device)
    else:
        edges = torch.zeros((2, 0), dtype=torch.long, device=device)
        attr = torch.zeros((0, 3), dtype=torch.float32, device=device)
    return node, edges, attr


def train_state(
    graphs: list[CallGraph],
    labels: list[int],
    *,
    hidden: int,
    epochs: int,
    seed: int,
    device: str,
    backend: str,
) -> dict[str, np.ndarray]:
    """Cross-entropy on the root-cause node. One graph at a time."""
    import torch
    from torch import nn

    if not graphs:
        raise ValueError("no graphs")
    from .torch_determinism import apply_cuda_determinism, epoch_order

    switches = apply_cuda_determinism(torch, device, seed)
    print(f"trace determinism {' '.join(switches)}", flush=True)
    n_node = int(graphs[0].node_x.shape[1])
    n_edge = 3
    model = _build(backend, n_node, n_edge, hidden).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-2)
    loss_fn = nn.CrossEntropyLoss()
    model.train()
    for epoch in range(max(1, epochs)):
        order = epoch_order(len(graphs), seed, epoch)
        for index in order:
            graph = graphs[index]
            if graph.node_x.shape[0] == 0:
                continue
            node, edges, attr = _tensors(graph, device)
            opt.zero_grad(set_to_none=True)
            logits = model(node, edges, attr).view(1, -1)
            target = torch.tensor([labels[index]], device=device)
            loss = loss_fn(logits, target)
            loss.backward()
            opt.step()
    model.eval()
    state: dict[str, np.ndarray] = {}
    for name, value in model.state_dict().items():
        state[name] = np.ascontiguousarray(value.detach().cpu().numpy(), dtype=np.float32)
    return state


def score_graph(graph: CallGraph, state: dict[str, np.ndarray], *, hidden: int, device: str, backend: str) -> np.ndarray:
    """One score per service, in ``graph.services`` order."""
    import torch

    if graph.node_x.shape[0] == 0:
        return np.zeros((0,), dtype=np.float64)
    model = _build(backend, int(graph.node_x.shape[1]), 3, hidden)
    tensors = {name: torch.from_numpy(np.asarray(value)).to(device) for name, value in state.items()}
    model.load_state_dict(tensors)
    model.to(device)
    model.eval()
    node, edges, attr = _tensors(graph, device)
    with torch.no_grad():
        logits = model(node, edges, attr).detach().cpu().numpy()
    return np.asarray(logits, dtype=np.float64)
