# Submission copies

Word, PowerPoint and PDF copies made from the Markdown sources for uploading to the course. **The Markdown files in the repository are the source of truth.** These copies were made before the September 2026 review and are out of date until they are regenerated.

| Copy | Source | Regenerate |
|---|---|---|
| `Alert_Fatigue_Final_Report.docx` | `FINAL_REPORT.md` | `node tools/md2docx.js FINAL_REPORT.md submission/Alert_Fatigue_Final_Report.docx "" <folder with figure PNGs>` |
| `Alert_Fatigue_Design_Specification.docx` | `design_specification.md` | `node tools/md2docx.js design_specification.md submission/Alert_Fatigue_Design_Specification.docx ""` |
| `Alert_Fatigue_Project_Plan.docx` | `project_plan.md` | `node tools/md2docx.js project_plan.md submission/Alert_Fatigue_Project_Plan.docx ""` |
| `Use_Your_Data_Guide.docx` | `USE_YOUR_DATA.md` | `node tools/md2docx.js USE_YOUR_DATA.md submission/Use_Your_Data_Guide.docx ""` |
| `Alert_Fatigue_Reduction_Proposal.docx`, `Project_Proposal.docx`, `Project_Proposal.pdf` | `PROJECT_PROPOSAL.md` (as submitted) | Keep as submitted |
| `Alert_Fatigue_Proposal_Rehearsal.pptx` | `tools/build_rehearsal.py` | `python tools/build_rehearsal.py` (needs `pip install python-pptx`) |

`md2docx.js` needs Node and `npm install docx`. It does not draw Mermaid diagrams; export them from GitHub or mermaid.live as images first.
