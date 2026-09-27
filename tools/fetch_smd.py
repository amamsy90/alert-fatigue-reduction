"""Download the Server Machine Dataset (SMD, from the OmniAnomaly paper, KDD 2019)
into app/data/smd/<machine>/ in the generic format.

SMD is five weeks of one-minute server metrics from 28 machines at a large
internet company: 38 metrics per machine (CPU, memory, disk, network, TCP,
...), with labelled real incidents and, for each incident, which metrics
moved. It is the second benchmark: other fault types than GAIA's 1 GB memory
program, on real servers rather than containers.

    python tools/fetch_smd.py                 # all 28 machines (about 240 MB)
    python tools/fetch_smd.py machine-1-1     # one machine

Source: https://github.com/NetManAIOps/OmniAnomaly (ServerMachineDataset, test part).
Timestamps are not in the files; one-minute samples are placed from 2019-01-01.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

RAW = "https://raw.githubusercontent.com/NetManAIOps/OmniAnomaly/master/ServerMachineDataset"
OUT = Path(__file__).resolve().parents[1] / "app" / "data" / "smd"
MACHINES = [f"machine-{g}-{i}" for g, n in ((1, 8), (2, 9), (3, 11)) for i in range(1, n + 1)]
T0 = pd.Timestamp("2019-01-01", tz="UTC")


def _get(path: str) -> str:
    with urlopen(f"{RAW}/{path}", timeout=120) as resp:
        return resp.read().decode("utf-8")


def fetch(machine: str) -> Path:
    folder = OUT / machine
    folder.mkdir(parents=True, exist_ok=True)
    values = pd.read_csv(pd.io.common.StringIO(_get(f"test/{machine}.txt")), header=None)
    values.columns = [f"m{i + 1}" for i in range(values.shape[1])]
    values.insert(0, "timestamp", [T0 + pd.Timedelta(minutes=i) for i in range(len(values))])
    values.to_csv(folder / "metrics.csv", index=False)
    rows = []
    for line in _get(f"interpretation_label/{machine}.txt").splitlines():
        span, dims = line.strip().split(":")
        a, b = (int(x) for x in span.split("-"))
        for d in dims.split(","):
            rows.append({"start": T0 + pd.Timedelta(minutes=a), "end": T0 + pd.Timedelta(minutes=b),
                         "series": f"m{int(d)}", "incident": f"{machine}:{a}-{b}", "metrics_moved": len(dims.split(","))})
    pd.DataFrame(rows).to_csv(folder / "incidents.csv", index=False)
    return folder


if __name__ == "__main__":
    for m in (sys.argv[1:] or MACHINES):
        print(f"{m}: {fetch(m)}")
