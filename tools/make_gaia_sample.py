"""Cut a small, real slice of GAIA MicroSS to commit as the quick reproduction sample.

Keeps the first N days of the full data, untouched: every log line and every
memory point from the origin to origin + N days (default 10: the 7 training days
plus 3 test days). Nothing is generated or resampled, so the sample is real GAIA
and the same detector code runs on it. Output is gzipped csv.

    cd app
    python ../tools/make_gaia_sample.py            # reads data/gaia_*, writes examples/gaia_sample/

The sample is only a smoke-scale check. The published numbers use all 62 days.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from src.gaia_loader import LOG_HEADER_RE, LOG_TZ  # noqa: E402
from src.memory_spikes import read_metric_csv, service_in_name  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--run-dir", default="data/gaia_raw/run/run")
    parser.add_argument("--metrics-dir", default="data/gaia_metrics")
    parser.add_argument("--out", default="examples/gaia_sample")
    args = parser.parse_args()

    out = Path(args.out)
    (out / "run").mkdir(parents=True, exist_ok=True)
    (out / "metric").mkdir(parents=True, exist_ok=True)

    tables = [pd.read_csv(f) for f in sorted(Path(args.run_dir).glob("run_table_*.csv"))]
    runs = pd.concat(tables, ignore_index=True)
    header = runs["message"].astype(str).str.strip().str.extract(LOG_HEADER_RE)
    when = pd.to_datetime(header["ts"]).dt.tz_localize(LOG_TZ).dt.tz_convert("UTC")
    origin = when.min()
    end = origin + pd.Timedelta(days=args.days)
    kept = runs[when.notna() & (when < end)]
    kept.to_csv(out / "run" / "run_table_sample.csv.gz", index=False)
    print(f"origin {origin}, end {end}: kept {len(kept)} of {len(runs)} log rows")

    total = 0
    for path in sorted(Path(args.metrics_dir).rglob("*.csv")):
        service = service_in_name(path.name)
        if service is None:
            continue
        frame = read_metric_csv(path, service)
        part = frame[frame["timestamp"] < end]
        if part.empty:
            continue
        raw = pd.read_csv(path).loc[part.index]
        name = path.name.replace(".csv", f"_first{args.days}d.csv.gz")
        raw.to_csv(out / "metric" / name, index=False)
        total += len(raw)
    print(f"kept {total} memory points; wrote {out}")


if __name__ == "__main__":
    main()
