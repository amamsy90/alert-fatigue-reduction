"""Download NAB (Numenta Anomaly Benchmark, MIT licence) into app/data/nab/ in the
generic format (metrics.csv + incidents.csv per category).

    python tools/fetch_nab.py                 # all real categories
    python tools/fetch_nab.py realTweets      # just one

The web app does the same from "Evaluate my data" -> "Download".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from product.datasets import NAB_CATEGORIES, fetch_nab  # noqa: E402

if __name__ == "__main__":
    for cat in fetch_nab(sys.argv[1:] or list(NAB_CATEGORIES)):
        print(f"{cat}: done")
