#!/usr/bin/env python3
"""Check that the main report ends by page 4.

Usage: python tools/check_report.py submission/Alert_Fatigue_Final_Report_v1.2.0.pdf

The page where section 5 (Conclusion) ends is the page before Appendix A.
LibreOffice on this machine embeds Liberation Serif when Times New Roman is
absent. That face is metric-compatible. The copy submitted to the course
should be exported from Word, which has Times New Roman.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


def pdftotext(pdf: Path, first: int, last: int) -> str:
    return subprocess.check_output(
        ["pdftotext", "-f", str(first), "-l", str(last), str(pdf), "-"],
        text=True,
        errors="replace",
    )


def main() -> int:
    pdf = Path(sys.argv[1] if len(sys.argv) > 1 else "submission/Alert_Fatigue_Final_Report_v1.2.0.pdf")
    if not pdf.exists():
        print(f"missing {pdf}", file=sys.stderr)
        return 2
    if not shutil.which("pdfinfo") or not shutil.which("pdftotext"):
        print("pdfinfo and pdftotext are required", file=sys.stderr)
        return 2
    info = subprocess.check_output(["pdfinfo", str(pdf)], text=True, errors="replace")
    pages = int(re.search(r"^Pages:\s+(\d+)", info, re.M).group(1))
    size = re.search(r"^Page size:\s+([\d.]+)\s+x\s+([\d.]+)", info, re.M)
    print(info.strip())
    if size:
        width, height = float(size.group(1)), float(size.group(2))
        if abs(width - 595.28) > 5 or abs(height - 841.89) > 5:
            print(f"WARNING: page size {width} x {height} pt is not A4")
    appendix = None
    conclusion = None
    for number in range(1, pages + 1):
        text = pdftotext(pdf, number, number)
        if conclusion is None and re.search(r"5\.?\s+Conclusion", text):
            conclusion = number
        # The heading is "Appendix A." A body mention such as "Appendix A has" is not the start.
        if appendix is None and re.search(r"Appendix A\.|^A\s+Walk-forward results", text, re.M):
            appendix = number
    if conclusion is None or appendix is None:
        print(f"FAIL: could not find Conclusion ({conclusion}) or Appendix A ({appendix})")
        return 1
    # Appendix A starts on its own page, so the conclusion ends on the page before it.
    end = appendix - 1
    print(f"Conclusion starts on page {conclusion} and ends on page {end} (Appendix A starts on page {appendix}).")
    print(f"Total pages: {pages}")
    page1 = pdftotext(pdf, 1, 1)
    for needle in ("AI-Assisted On-Call Queue", "Aishah Mohamed Alyammahi", "Abstract"):
        if needle not in page1:
            print(f"FAIL: page 1 does not contain {needle!r}")
            return 1
    if shutil.which("pdffonts"):
        fonts = subprocess.check_output(["pdffonts", str(pdf)], text=True, errors="replace")
        print(fonts.strip())
        if "TimesNewRoman" not in fonts.replace(" ", "") and "Times New Roman" not in fonts:
            print("NOTE: Times New Roman is not embedded. Export the submitted PDF from Word. Liberation Serif is the metric-compatible substitute used here.")
    if end > 4:
        print(f"FAIL: main report ends on page {end}, which is past the 4-page limit")
        return 1
    print(f"OK: main report ends on page {end}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
