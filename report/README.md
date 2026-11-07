# Four-page final report

`final_report.tex` is the source of `final_report.pdf`, the report for the final-project brief: A4, 2.5 cm margins, Times New Roman 12 pt justified, line spacing 1.15, 6 pt after paragraphs, five numbered sections, a main body of four pages, and appendices after it.

The long report in `../FINAL_REPORT.md` is the detailed record behind it; every number in the short report is taken from it and from `../app/results/`.

## Build

Needs XeLaTeX (MiKTeX or TeX Live) and the Times New Roman and Courier New fonts installed.

```bash
cd report
xelatex final_report.tex
xelatex final_report.tex
```

Before submitting, replace `COMMIT_HASH` in section 4 with the output of `git rev-parse HEAD` for the commit you submit, rebuild, and commit the PDF. A commit cannot contain its own hash, so submit the hash of the last commit before the one that adds the final PDF, and make sure nothing but `report/` changes in between.
