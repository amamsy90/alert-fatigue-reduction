"""CUDA switches shared by the trace fit and the LoRA fit.

The metric TCN sets the same torch flags. This module adds the cuBLAS
workspace those flags need, a CPU shuffle for the training order, and a
request for PyG's deterministic scatter when that build has one. None of
this makes a CUDA train bit-reproducible. Two runs can still write different
weight hashes. Compare the scores, not the hash.
"""
from __future__ import annotations

import os
import random
from typing import Any

import numpy as np

# Required before cuBLAS is first used if a deterministic GEMM is available.
CUBLAS_WORKSPACE = ":4096:8"


def enable_cublas_workspace() -> str:
    """Set the cuBLAS workspace. Call this before the first CUDA matmul."""
    os.environ["CUBLAS_WORKSPACE_CONFIG"] = CUBLAS_WORKSPACE
    return CUBLAS_WORKSPACE


def epoch_order(n: int, seed: int, epoch: int) -> list[int]:
    """Graph order for one epoch. A CPU generator, not the CUDA RNG.

    The same seed and epoch return the same order on every machine.
    """
    rng = random.Random((int(seed) * 1_000_003) ^ int(epoch))
    order = list(range(n))
    rng.shuffle(order)
    return order


def apply_cuda_determinism(torch_module: Any, device: str, seed: int) -> list[str]:
    """Seed the process and turn on the deterministic CUDA switches.

    ``warn_only`` matches the TCN: a kernel with no deterministic
    implementation must not abort the fit. Returns the switches that were set.
    """
    applied = [enable_cublas_workspace()]
    random.seed(seed)
    np.random.seed(seed % (2**32))
    torch_module.manual_seed(seed)
    applied.append("manual_seed")
    if not str(device).startswith("cuda"):
        return applied
    torch_module.cuda.manual_seed_all(seed)
    backends = torch_module.backends
    backends.cudnn.deterministic = True
    backends.cudnn.benchmark = False
    matmul = getattr(getattr(backends, "cuda", None), "matmul", None)
    if matmul is not None:
        matmul.allow_tf32 = False
    cudnn = backends.cudnn
    if hasattr(cudnn, "allow_tf32"):
        cudnn.allow_tf32 = False
    try:
        torch_module.use_deterministic_algorithms(True, warn_only=True)
        applied.append("deterministic_algorithms_warn_only")
    except TypeError:
        torch_module.use_deterministic_algorithms(True)
        applied.append("deterministic_algorithms")
    applied.append(prefer_deterministic_scatter(torch_module, seed))
    return applied


def prefer_deterministic_scatter(torch_module: Any, seed: int) -> str:
    """Ask PyG to scatter deterministically when this build can.

    PyG follows ``use_deterministic_algorithms`` on versions that implement
    a deterministic scatter. There is no separate switch on a build that
    does not. A missing PyG install is the plain message-passing path.
    """
    try:
        import torch_geometric
    except ImportError:
        return "pyg-unavailable"
    seeder = getattr(torch_geometric, "seed_everything", None)
    if callable(seeder):
        seeder(int(seed))
    checker = getattr(torch_module, "are_deterministic_algorithms_enabled", None)
    if callable(checker) and bool(checker()):
        return "pyg-deterministic-scatter"
    return "pyg-best-effort"
