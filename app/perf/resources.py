"""Resources one scoring job needs: wall time, peak memory, disk per day.

Runs the product's own job (`run_job`, the GAIA month, shadow mode) against a
throw-away database, model folder and settings in a temporary directory, so it
touches nothing the app uses and cannot page anyone (no paging keys exist
there). Needs the GAIA data (`python ../tools/fetch_gaia.py`).

    python perf/resources.py            # writes results/resources_gaia.json

Feeds the operating-cost section of FINAL_REPORT.md (section 7).
"""
from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path


def peak_rss_bytes() -> int:
    """Peak resident memory of this process (Linux, macOS, Windows)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        psapi = ctypes.WinDLL("psapi")  # type: ignore[attr-defined]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)  # Linux reports KiB


def folder_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> None:
    out = Path("results") / "resources_gaia.json"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ.update({
            "QUEUE_DB": str(root / "queue.db"),
            "MODEL_PATH": str(root / "models" / "model.pkl"),
            "OPS_PATH": str(root / "ops.json"),
            "SETTINGS_PATH": str(root / "settings.json"),
            "SECRETS_DIR": str(root / "secrets"),
            "AUDIT_LOG_PATH": "off",
            "SHADOW_MODE": "1",
        })
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from product.pipeline import run_job
        from product.store import QueueStore

        store = QueueStore(root / "queue.db")
        rss_before = peak_rss_bytes()
        start = time.perf_counter()
        result = run_job(store, mode="gaia", budget_k=20, shadow_mode=True)
        seconds = time.perf_counter() - start
        days = len(result["days"])
        db_bytes = sum(f.stat().st_size for f in root.glob("queue.db*"))
        report = {
            "job": "GAIA month, product run_job, shadow mode, K=20",
            "machine": {"system": platform.system(), "machine": platform.machine(),
                        "processor": platform.processor(), "cpus": os.cpu_count(),
                        "python": platform.python_version()},
            "alerts": result["meta"].get("alert_count"),
            "days": days,
            "wall_seconds": round(seconds, 1),
            "peak_rss_mb_before_job": round(rss_before / 2**20),  # imports and Python itself
            "peak_rss_mb": round(peak_rss_bytes() / 2**20),
            "db_mb": round(db_bytes / 2**20, 1),
            "db_kb_per_day": round(db_bytes / 1024 / max(days, 1)),
            "models_mb": round(folder_bytes(root / "models") / 2**20, 2) if (root / "models").exists() else 0.0,
        }
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
