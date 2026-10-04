# Submission copies

Word and PDF copies made from the Markdown sources for uploading to the course. **The Markdown files in the repository are the source of truth.** The Word files below were regenerated on 30 September 2026.

| Copy | Source | Regenerate |
|---|---|---|
| `Alert_Fatigue_Final_Report.docx` | `FINAL_REPORT.md` | `node tools/md2docx.js FINAL_REPORT.md submission/Alert_Fatigue_Final_Report.docx "" figures` |
| `Alert_Fatigue_Design_Specification.docx` | `design_specification.md` | `node tools/md2docx.js design_specification.md submission/Alert_Fatigue_Design_Specification.docx ""` |
| `Alert_Fatigue_Project_Plan.docx` | `project_plan.md` | `node tools/md2docx.js project_plan.md submission/Alert_Fatigue_Project_Plan.docx ""` |
| `Use_Your_Data_Guide.docx` | `USE_YOUR_DATA.md` | `node tools/md2docx.js USE_YOUR_DATA.md submission/Use_Your_Data_Guide.docx ""` |
| `Alert_Fatigue_Reduction_Proposal.docx`, `Project_Proposal.docx` | `proposal.md` | `node tools/md2docx.js proposal.md submission/Project_Proposal.docx ""` |
| `Project_Proposal.pdf` | `Project_Proposal.docx` | Still the earlier PDF. Word opened for the export and did not finish, so this file was not replaced. |
| `Alert_Fatigue_Proposal_Rehearsal.pptx` | `tools/build_rehearsal.py` | `python tools/build_rehearsal.py` (needs `pip install python-pptx`) |

`md2docx.js` needs Node and `npm install` in this folder (`docx` is listed in `package.json`). It does not draw Mermaid diagrams; export them from GitHub or mermaid.live as images first. The report embeds `figures/detection_vs_k.png` and `figures/walkforward.png`.
