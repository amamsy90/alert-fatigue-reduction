"""Run-table clocks are China time and must be stored as UTC."""
import pandas as pd

from src.gaia_loader import parse_run_table


def test_injection_clock_is_shifted_from_china_to_utc():
    frame = pd.DataFrame(
        [
            {
                "datetime": "2021-07-10",
                "service": "dbservice1",
                "message": (
                    "2021-07-10 10:37:32,000 | WARNING | 0.0.0.4 | 172.17.0.3 | "
                    "dbservice1 | [memory_anomalies] trigger a high memory program, "
                    "start at 2021-07-10 10:37:32.000000 and lasts 600 seconds and use 1g memory"
                ),
            }
        ]
    )
    alerts, faults = parse_run_table(frame)
    assert alerts == []
    assert faults[0].start == pd.Timestamp("2021-07-10 02:37:32", tz="UTC")
    assert faults[0].end == faults[0].start + pd.Timedelta(seconds=600)
