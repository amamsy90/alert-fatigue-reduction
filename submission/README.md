# Submission copies

Word and PDF copies made from the Markdown sources for uploading to the course. **The Markdown files in the repository are the source of truth.** The report copies (Word and PDF) were last regenerated on 11 October 2026 for release 1.0.4 (301 tests, 195 checked numbers); the design specification on 11 October 2026; the other generated copies on 10 October 2026.

Project title on every copy: *AI-Assisted On-Call Queue: Budgeted Incident Detection for Cloud Microservices*, subtitle *Alert Fatigue Reduction in Cloud Microservice Operations*. The file names keep the older `Alert_Fatigue_` prefix so that earlier links still work.

| Copy | Source | Regenerate |
|---|---|---|
| `Alert_Fatigue_Final_Report.docx` | `FINAL_REPORT.md` | `node tools/md2docx.js FINAL_REPORT.md submission/Alert_Fatigue_Final_Report.docx "" figures` |
| `Alert_Fatigue_Final_Report.pdf` | `Alert_Fatigue_Final_Report.docx` | Export to PDF from Word, or `soffice --headless --convert-to pdf --outdir submission submission/Alert_Fatigue_Final_Report.docx` (this copy: Word) |
| `Alert_Fatigue_Design_Specification.docx` | `design_specification.md` | `node tools/md2docx.js design_specification.md submission/Alert_Fatigue_Design_Specification.docx ""` |
| `Alert_Fatigue_Project_Plan.docx` | `project_plan.md` | `node tools/md2docx.js project_plan.md submission/Alert_Fatigue_Project_Plan.docx ""` |
| `Use_Your_Data_Guide.docx` | `USE_YOUR_DATA.md` | `node tools/md2docx.js USE_YOUR_DATA.md submission/Use_Your_Data_Guide.docx ""` |
| `Project_Proposal.docx`, `Project_Proposal.pdf` | `proposal.md` (the 30 September draft) | `node tools/md2docx.js proposal.md submission/Project_Proposal.docx ""`, then export to PDF as above |
| `Project_Proposal_200010549_Aishah_Alyammahi.pdf` | `Project_Proposal_200010549_Aishah_Alyammahi.docx` | **The proposal as submitted on 5 October 2026.** Edited in Word, not generated from Markdown; do not regenerate. `queue_flowchart.png` is its figure |
| `Alert_Fatigue_Proposal_Rehearsal.pptx` | `tools/build_rehearsal.py` | `python tools/build_rehearsal.py` (needs `pip install python-pptx`) |

`md2docx.js` needs Node and `npm install` (or `npm ci`) in the repository root (`docx` is listed in `package.json`). It does not draw Mermaid diagrams; export them from GitHub or mermaid.live as images first. The report embeds `figures/detection_vs_k.png` and `figures/walkforward.png`.

Earlier drafts and duplicates (`Project_Proposal_to_edit.docx`, the first 5 October proposal draft, and `Alert_Fatigue_Reduction_Proposal.docx`, a copy of `Project_Proposal.docx`) are kept locally in `submission/archive/`, which git ignores.
