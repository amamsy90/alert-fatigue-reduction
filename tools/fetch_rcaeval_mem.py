"""Download RCAEval RE1 memory faults and write the generic metric format.

RE1 injects the same five fault types on three microservice applications
(Online Boutique, Sock Shop, Train Ticket). This keeps the memory injections
only: 25 cases per application, five services, five repetitions. Each case is
a separate recording. The CSV lays those recordings end to end so a person can
open one file; that join is not a real timeline, and run_rcaeval.py does not
score it. The runner reads the cached parquet and scores each recording on its
own. Only the memory series are kept. The label is the annotated service's
memory series, from the injection time to the end of that recording.

    python tools/fetch_rcaeval_mem.py

Source: https://huggingface.co/datasets/phamquiluan/RCAEval (RE1, fault mem).
Writes app/data/rcaeval/<application>/metrics.csv and incidents.csv.
"""
from __future__ import annotations

import urllib.request
from pathlib import Path

import pandas as pd

HF = "https://huggingface.co/datasets/phamquiluan/RCAEval/resolve/main"
OUT = Path(__file__).resolve().parents[1] / "app" / "data" / "rcaeval"
RAW = OUT / "_raw"
ORIGIN = pd.Timestamp("2023-01-01", tz="UTC")


def _get(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)


def _cases() -> pd.DataFrame:
    index = RAW / "cases.parquet"
    _get(f"{HF}/cases.parquet", index)
    frame = pd.read_parquet(index)
    mem = frame[(frame["suite"] == "RE1") & (frame["fault"] == "mem")].copy()
    if mem.empty:
        raise SystemExit("RCAEval index has no RE1 memory cases")
    return mem.sort_values(["system_name", "time_start"])


def _application(cases: pd.DataFrame) -> None:
    name = str(cases["system_name"].iloc[0])
    slug = name.lower().replace(" ", "-")
    folder = OUT / slug
    folder.mkdir(parents=True, exist_ok=True)
    pieces: list[pd.DataFrame] = []
    incidents: list[dict] = []
    cursor = ORIGIN
    for rec in cases.itertuples(index=False):
        src = RAW / rec.case / "metrics.parquet"
        _get(f"{HF}/{rec.case}/metrics.parquet", src)
        raw = pd.read_parquet(src)
        mem_cols = [c for c in raw.columns if str(c).endswith("_mem")]
        series = f"{rec.root_cause_service}_mem"
        if "time" not in raw.columns or series not in mem_cols:
            raise SystemExit(f"{rec.case} has no time column or {series}")
        stamps = pd.to_datetime(raw["time"], unit="s", utc=True)
        shifted = cursor + (stamps - stamps.min())
        block = raw[mem_cols].copy()
        block.insert(0, "timestamp", shifted)
        pieces.append(block)
        start = cursor + pd.Timedelta(seconds=int(rec.inject_time - rec.time_start))
        end = cursor + pd.Timedelta(seconds=int(rec.time_end - rec.time_start))
        incidents.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "series": series,
            "incident": rec.case,
            "service": rec.root_cause_service,
        })
        cursor = shifted.max() + pd.Timedelta(seconds=1)
        print(f"  {rec.case}: {series}")
    metrics = pd.concat(pieces, ignore_index=True)
    metrics = metrics.drop_duplicates(["timestamp"]).sort_values("timestamp")
    metrics.to_csv(folder / "metrics.csv", index=False)
    pd.DataFrame(incidents).to_csv(folder / "incidents.csv", index=False)
    print(f"{name}: {len(incidents)} memory faults, {len(metrics):,} rows, {len(mem_cols)} memory series")


def main() -> None:
    cases = _cases()
    print(f"{len(cases)} RE1 memory cases")
    for _, part in cases.groupby("system_name", sort=True):
        _application(part)


if __name__ == "__main__":
    main()
