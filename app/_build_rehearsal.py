"""Build the 5-slide, 7-minute proposal rehearsal deck."""
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

NAVY = RGBColor(0x1C, 0x3A, 0x5C)
GOLD = RGBColor(0xB0, 0x89, 0x4B)
INK = RGBColor(0x2B, 0x31, 0x38)
MUTED = RGBColor(0x5C, 0x65, 0x6D)
LINE = RGBColor(0xD7, 0xDD, 0xE3)
PANEL = RGBColor(0xF6, 0xF4, 0xEF)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

W, H = Inches(13.333), Inches(7.5)
OUT = Path(__file__).resolve().parents[1] / "Alert_Fatigue_Proposal_Rehearsal.pptx"


def add_text(slide, text, x, y, w, h, size, color, bold=False, align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.name = "Calibri"
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    return box


def add_bar(slide, y=Inches(0.0)):
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, y, W, Inches(0.08))
    bar.fill.solid()
    bar.fill.fore_color.rgb = GOLD
    bar.line.fill.background()


def add_notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


def bullets(slide, items, x, y, w, h, size=18):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.level = 0
        p.space_after = Pt(8)
        run = p.add_run()
        run.text = item
        run.font.name = "Calibri"
        run.font.size = Pt(size)
        run.font.color.rgb = INK
    return box


def panel(slide, x, y, w, h):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = PANEL
    shape.line.color.rgb = LINE
    return shape


def blank():
    prs = Presentation()
    prs.slide_width = W
    prs.slide_height = H
    return prs, prs.slides.add_slide(prs.slide_layouts[6])


def build():
    prs = Presentation()
    prs.slide_width = W
    prs.slide_height = H
    blank_layout = prs.slide_layouts[6]

    # --- Slide 1 Motivation ---
    s = prs.slides.add_slide(blank_layout)
    add_bar(s)
    add_text(s, "1  ·  MOTIVATION", Inches(0.6), Inches(0.25), Inches(6), Inches(0.35), 14, GOLD, True)
    add_text(
        s,
        "Alert fatigue on a cloud microservice platform",
        Inches(0.6),
        Inches(0.55),
        Inches(12),
        Inches(0.7),
        32,
        NAVY,
        True,
    )
    bullets(
        s,
        [
            "One online platform: many small services (login, database, cache, web).",
            "Thousands of alerts a day. Most need no action. Some are many symptoms of one fault.",
            "The on-call person can check only a few items. Real faults are missed because attention ran out.",
            "Today’s rule: fixed severity + delete exact copies. It does not read the alert, see a memory jump, or respect the budget.",
            "Goal: under a daily budget K, show more real faults and a smaller pile.",
        ],
        Inches(0.7),
        Inches(1.5),
        Inches(12),
        Inches(5.2),
        20,
    )
    add_notes(
        s,
        "Good morning. My proposal is Alert Fatigue Reduction for cloud microservice operations — "
        "the on-call queue behind an online platform made of many small services.\n\n"
        "When the person on duty can check only a few warnings, the system should show the real "
        "problem and keep the noise out of the way.\n\n"
        "Those services do not pause. One of them can raise thousands of alerts in a day, and most "
        "need no action. The person on duty can read only a handful in an hour. That is alert fatigue. "
        "Users of the platform pay that delay.\n\n"
        "Today’s rule is fixed severity plus deleting exact copies. It does not see that a memory jump "
        "and the logs around it are one incident, and it does not change when five slots are free or fifty.\n\n"
        "The goal is to show more real faults than those rules, and to hand the on-call person a smaller pile.",
    )

    # --- Slide 2 Impact ---
    s = prs.slides.add_slide(blank_layout)
    add_bar(s)
    add_text(s, "2  ·  IMPACT", Inches(0.6), Inches(0.25), Inches(6), Inches(0.35), 14, GOLD, True)
    add_text(
        s,
        "One sector. The cost is paid by users.",
        Inches(0.6),
        Inches(0.55),
        Inches(12),
        Inches(0.7),
        32,
        NAVY,
        True,
    )
    for i, (title, body) in enumerate(
        (
            ("Who", "On-call engineers on a cloud microservice platform — login, database, cache, web."),
            ("What happens", "A real fault stays in the queue. Users wait, or the service goes down."),
            ("Why it lasts", "A night of false alarms teaches people not to trust the queue."),
            ("My test", "At this budget, these faults were shown to a person. A high score alone is not success."),
        )
    ):
        x = Inches(0.6 + (i % 2) * 6.3)
        y = Inches(1.55 + (i // 2) * 2.5)
        panel(s, x, y, Inches(6.0), Inches(2.25))
        add_text(s, title, x + Inches(0.25), y + Inches(0.2), Inches(5.5), Inches(0.4), 16, GOLD, True)
        add_text(s, body, x + Inches(0.25), y + Inches(0.7), Inches(5.5), Inches(1.3), 18, INK)
    add_notes(
        s,
        "This is about one sector: cloud microservice platforms — login, database, cache, web — "
        "not banks and hospitals as separate industries. When a warning is buried in that queue, "
        "the people using the online service wait longer, or the service goes down.\n\n"
        "A night of false alarms also teaches the on-call engineer to stop trusting the queue.\n\n"
        "For me, the point is the decision, not the model. I want one sentence I can stand behind: "
        "at this budget, these faults on this platform were shown to a person, and these were not.",
    )

    # --- Slide 3 Implementation ---
    s = prs.slides.add_slide(blank_layout)
    add_bar(s)
    add_text(s, "3  ·  IMPLEMENTATION AND DATA", Inches(0.6), Inches(0.25), Inches(8), Inches(0.35), 14, GOLD, True)
    add_text(
        s,
        "GAIA MicroSS, then four steps, then a company’s own labels",
        Inches(0.6),
        Inches(0.55),
        Inches(12.2),
        Inches(0.6),
        26,
        NAVY,
        True,
    )
    panel(s, Inches(0.5), Inches(1.3), Inches(12.3), Inches(1.55))
    add_text(
        s,
        "Data  ·  GAIA MicroSS (CloudWise). ~2 months of microservice logs + container memory. "
        "34,863 alerts, 1,367 injected memory faults. Learn week 1 (3,630 / 141). Test the next 55 days "
        "(31,233 / 1,226). Logs are China time; memory is UTC — both aligned on load.",
        Inches(0.7),
        Inches(1.45),
        Inches(11.9),
        Inches(1.3),
        16,
        INK,
    )
    steps = (
        ("1  Align", "Match each alert to the injected fault on the same service."),
        ("2  Classify", "Miss a fault = 10× the cost of one extra alert."),
        ("3  Memory jump", "Rise > 0.5 GB above the last 15 minutes, at least 2 points. Size chosen on week 1 only."),
        ("4  Budget K", "One list per day. Biggest jumps first. Then escalate / ask / hide."),
    )
    for i, (title, body) in enumerate(steps):
        x = Inches(0.5 + i * 3.2)
        panel(s, x, Inches(3.05), Inches(3.05), Inches(2.55))
        add_text(s, title, x + Inches(0.15), Inches(3.2), Inches(2.75), Inches(0.45), 16, GOLD, True)
        add_text(s, body, x + Inches(0.15), Inches(3.7), Inches(2.75), Inches(1.7), 14, INK)
    add_text(
        s,
        "A company cannot use the GAIA model as is. Shadow mode → Real / Noise marks → retrain on their data.",
        Inches(0.6),
        Inches(5.75),
        Inches(12.2),
        Inches(0.7),
        16,
        NAVY,
        True,
    )
    add_notes(
        s,
        "I will build this in Python. The data matches the sector.\n\n"
        "GAIA MicroSS: about two months of microservice logs plus container memory. Every fault was "
        "injected — extra memory for about ten minutes on one service — so I know the right answer. "
        "Learn on the first week. Test on the fifty-five days after it.\n\n"
        "Four steps. Align. Classify, with a miss weighing ten times one extra alert. "
        "A jump is memory rising more than half a gigabyte above its own level in the last fifteen minutes, "
        "for at least two points. The size was chosen on week one only. "
        "One list of K items a day: biggest jumps first, then send, ask, or hide.\n\n"
        "A company cannot drop in the GAIA-trained model. Run in shadow mode. Mark Real or Noise. Retrain.",
    )

    # --- Slide 4 Evaluation ---
    s = prs.slides.add_slide(blank_layout)
    add_bar(s)
    add_text(s, "4  ·  EVALUATION  ·  TEST PERIOD, 1,226 FAULTS", Inches(0.6), Inches(0.25), Inches(11), Inches(0.35), 14, GOLD, True)
    add_text(
        s,
        "Same split as the baseline. Numbers, not wishes.",
        Inches(0.6),
        Inches(0.55),
        Inches(12),
        Inches(0.5),
        26,
        NAVY,
        True,
    )

    rows = (
        ("Queue at 20 items / day", "Faults caught", "Items / day", "False / day"),
        ("Severity baseline", "0%", "0.8", "0.8"),
        ("Learned alerts only", "12.9%", "5.8", "0.6"),
        ("Shared queue — jumps first", "72.8%", "18", "1.3"),
    )
    table = s.shapes.add_table(len(rows), 4, Inches(0.55), Inches(1.25), Inches(12.2), Inches(2.35)).table
    widths = (Inches(5.4), Inches(2.3), Inches(2.25), Inches(2.25))
    for j, w in enumerate(widths):
        table.columns[j].width = w
    for i, row in enumerate(rows):
        for j, cell_text in enumerate(row):
            cell = table.cell(i, j)
            cell.text = cell_text
            for p in cell.text_frame.paragraphs:
                p.alignment = PP_ALIGN.LEFT if j == 0 else PP_ALIGN.CENTER
                for run in p.runs:
                    run.font.name = "Calibri"
                    run.font.size = Pt(16)
                    run.font.bold = i == 0 or (i == 3)
                    run.font.color.rgb = WHITE if i == 0 else NAVY if i == 3 else INK
            fill = cell.fill
            fill.solid()
            fill.fore_color.rgb = NAVY if i == 0 else PANEL if i == 3 else WHITE

    add_text(
        s,
        "Memory jumps overlap 86.1% of faults (1,056 / 1,226; 256 with no fault). Old rule (mean + 3σ): 53.8%. "
        "At 5 / 10 / 50 items a day the shared queue catches 19% / 39% / 86%.",
        Inches(0.6),
        Inches(3.8),
        Inches(12.2),
        Inches(0.85),
        16,
        INK,
    )
    add_text(
        s,
        "Two cautions: every GAIA fault is a memory program — say “on this dataset.” "
        "A jump is confirmed at its second point, so about two minutes, not 0.2. "
        "The 95% target is not met at K=20: most faults leave no log line.",
        Inches(0.6),
        Inches(4.7),
        Inches(12.2),
        Inches(1.4),
        16,
        MUTED,
    )
    add_notes(
        s,
        "Success is four checks, fixed before I look at the scores. Learn on week one. "
        "Test on the fifty-five days after it. I will not shuffle the days.\n\n"
        "A memory jump overlaps 86 percent of the 1,226 test faults. The old rule reached 54 percent.\n\n"
        "At twenty items a day the shared queue — jumps first — catches 73 percent. "
        "The severity baseline catches none, because the lines inside a fault are warnings. "
        "Alerts alone catch 13 percent. About 18 items a day, of which about 1.3 are false.\n\n"
        "Two cautions. Every GAIA fault is a memory program, so this dataset favours the memory rule. "
        "Say “on this dataset.” And detection takes about two minutes.\n\n"
        "The 95 percent target is not met at twenty a day. Most injected faults leave no log line.",
    )

    # --- Slide 5 Deliverables ---
    s = prs.slides.add_slide(blank_layout)
    add_bar(s)
    add_text(s, "5  ·  DELIVERABLES", Inches(0.6), Inches(0.25), Inches(6), Inches(0.35), 14, GOLD, True)
    add_text(
        s,
        "What you can look at, and what is already running",
        Inches(0.6),
        Inches(0.55),
        Inches(12),
        Inches(0.55),
        28,
        NAVY,
        True,
    )
    items = (
        ("Pipeline + baseline", "Same train/test split. Severity rule catches 0% of these faults."),
        ("Classifier", "Miss costs 10× a false alarm. Alerts alone catch 13% at K=20."),
        ("Memory-jump detector", "86.1% overlap on injected faults. Size chosen on week 1 only."),
        ("Budget policy + app", "Jumps first, then escalate / ask / hide. Shadow mode, Real / Noise, retrain."),
    )
    for i, (title, body) in enumerate(items):
        x = Inches(0.5 + (i % 2) * 6.4)
        y = Inches(1.35 + (i // 2) * 2.15)
        panel(s, x, y, Inches(6.15), Inches(2.0))
        add_text(s, title, x + Inches(0.25), y + Inches(0.2), Inches(5.7), Inches(0.4), 18, GOLD, True)
        add_text(s, body, x + Inches(0.25), y + Inches(0.7), Inches(5.7), Inches(1.1), 16, INK)
    add_text(
        s,
        "Scripts: run_memory.py, run_queue_eval.py, run_m1–m4. App: http://127.0.0.1:8080  ·  Thank you.",
        Inches(0.6),
        Inches(5.8),
        Inches(12.2),
        Inches(0.6),
        16,
        MUTED,
    )
    add_notes(
        s,
        "Four things you can look at. A pipeline with the baseline numbers. A classifier compared at the "
        "same budget. A memory-jump detector scored on the injected faults. And a budget policy — send, "
        "ask, or hide — with the scripts to rerun the test period.\n\n"
        "Those pieces are already running, plus a local on-call web app in shadow mode: Real and Noise "
        "buttons, a saved model, and a reminder to retrain after seven days or when new Real marks arrive.\n\n"
        "That is the proposal: alert fatigue reduction for cloud microservice on-call, on public data "
        "with injected faults, tested after week one. Thank you. I am happy to take questions.",
    )

    prs.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()
