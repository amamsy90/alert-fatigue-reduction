from pptx import Presentation

prs = Presentation("Alert_Fatigue_Proposal_Rehearsal.pptx")
for i, slide in enumerate(prs.slides, 1):
    print(f"===== SLIDE {i} layout={slide.slide_layout.name}")
    for shape in slide.shapes:
        kind = shape.shape_type
        print(f"--- shape name={shape.name!r} type={kind} has_text={shape.has_text_frame}")
        if shape.has_text_frame:
            for p in shape.text_frame.paragraphs:
                text = "".join(r.text for r in p.runs)
                if text.strip():
                    print(f"   [lvl{p.level}] {text}")
        if getattr(shape, "has_table", False) and shape.has_table:
            for row in shape.table.rows:
                print("   | " + " | ".join(c.text for c in row.cells))
    if slide.has_notes_slide:
        notes = slide.notes_slide.notes_text_frame.text
        print(f"   NOTES: {notes[:120]!r}...")
