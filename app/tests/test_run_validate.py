"""run_validate reads the numbers it checks from the report itself."""
from __future__ import annotations

import pytest

import run_validate as v


def test_rounding_matches_what_the_report_prints():
    assert v.matches(0.6958, "69.6%")
    assert not v.matches(0.6958, "70.2%")
    assert v.matches(0.7425, "74.3%")  # half-way rounds either way
    assert v.matches(198.0, "198") and not v.matches(191.0, "198")
    assert v.matches(0.72, "0.72") and v.matches(0.0083, "1%")


def test_report_table_is_parsed(tmp_path):
    report = tmp_path / "R.md"
    report.write_text(
        "# R\n\n## Checked numbers\n\n| Key | Value | Where |\n|---|---|---|\n"
        "| `queue.K20.proposed` | 69.6% | §5 |\n| `misses.K20.budget_full` | 198 | §5 |\n\n## Next\n| `x` | 1 | |\n",
        encoding="utf-8",
    )
    assert v.report_numbers(report) == {"queue.K20.proposed": "69.6%", "misses.K20.budget_full": "198"}


def test_report_without_the_table_fails(tmp_path):
    report = tmp_path / "R.md"
    report.write_text("# R\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Checked numbers"):
        v.report_numbers(report)
