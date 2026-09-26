"""Ready-made datasets for the "Evaluate my data" page, so anyone can try the tool
before exporting their own.

  shop            synthetic online shop: orders per region + notification stream (non-IT)
  microservice    synthetic memory metrics + alerts (one day, the quick-start example)
  nab:<category>  Numenta Anomaly Benchmark (MIT licence, real data, many domains),
                  downloaded on request into data/nab/
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.request import urlopen

import pandas as pd

APP_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = APP_ROOT / "examples"
NAB_DIR = APP_ROOT / "data" / "nab"
NAB_BASE = "https://raw.githubusercontent.com/numenta/NAB/master"
NAB_CATEGORIES = {
    "realAWSCloudwatch": "Cloud servers (AWS CloudWatch: CPU, network, disk)",
    "realTweets": "Twitter mention volume of companies (a notification-like stream)",
    "realAdExchange": "Online advertising (clicks and cost per click)",
    "realTraffic": "Road traffic (speed and travel time sensors)",
    "realKnownCause": "Mixed: machine temperature, NYC taxi demand, CPU, EC2 latency",
}


def _shop_files() -> dict:
    folder = EXAMPLES / "shop"
    if not (folder / "orders.csv").exists():
        import importlib.util

        spec = importlib.util.spec_from_file_location("make_business_example", EXAMPLES / "make_business_example.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        module.main()
    return {"metrics": folder / "orders.csv", "incidents": folder / "incidents.csv",
            "events": folder / "notifications.csv"}


def list_datasets() -> list[dict]:
    items = [
        {"id": "shop", "name": "Online shop: orders per region + notifications", "domain": "e-commerce (synthetic)",
         "available": True, "labels": True},
        {"id": "microservice", "name": "Microservice memory + alerts (one day)", "domain": "IT (synthetic)",
         "available": (EXAMPLES / "metrics.csv").exists(), "labels": False},
    ]
    for cat, desc in NAB_CATEGORIES.items():
        items.append({"id": f"nab:{cat}", "name": f"NAB {cat}", "domain": desc,
                      "available": (NAB_DIR / cat / "metrics.csv").exists(), "labels": True,
                      "source": "https://github.com/numenta/NAB (MIT)"})
    return items


def dataset_files(dataset_id: str) -> dict:
    if dataset_id == "shop":
        return _shop_files()
    if dataset_id == "microservice":
        return {"metrics": EXAMPLES / "metrics.csv", "events": EXAMPLES / "alerts.csv"}
    if dataset_id.startswith("nab:"):
        cat = dataset_id.split(":", 1)[1]
        folder = NAB_DIR / cat
        if not (folder / "metrics.csv").exists():
            fetch_nab([cat])
        return {"metrics": folder / "metrics.csv", "incidents": folder / "incidents.csv"}
    raise KeyError(f"Unknown dataset {dataset_id}")


def _get(path: str) -> bytes:
    with urlopen(f"{NAB_BASE}/{path}", timeout=60) as resp:
        return resp.read()


def fetch_nab(categories: list[str] | None = None) -> list[str]:
    """Download NAB categories and convert them to metrics.csv + incidents.csv."""
    categories = categories or list(NAB_CATEGORIES)
    windows = json.loads(_get("labels/combined_windows.json"))
    done = []
    for category in categories:
        if category not in NAB_CATEGORIES:
            raise KeyError(f"Unknown NAB category {category}")
        metrics, incidents = [], []
        for key in sorted(k for k in windows if k.startswith(category + "/")):
            name = Path(key).stem
            frame = pd.read_csv(io.BytesIO(_get(f"data/{key}")))
            frame["series"] = name
            metrics.append(frame[["timestamp", "value", "series"]])
            incidents += [{"start": a, "end": b, "series": name} for a, b in windows[key]]
        folder = NAB_DIR / category
        folder.mkdir(parents=True, exist_ok=True)
        pd.concat(metrics).to_csv(folder / "metrics.csv", index=False)
        pd.DataFrame(incidents, columns=["start", "end", "series"]).to_csv(folder / "incidents.csv", index=False)
        done.append(category)
    return done
