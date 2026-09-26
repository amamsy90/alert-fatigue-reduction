"""Build the 5-slide, 7-minute rehearsal deck (numbers from app/results, see FINAL_REPORT.md)."""
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
            "Today’s rule: fixed log severity + delete exact copies. On our data 82% of faults write no log line at all.",
            "Goal: under a daily budget K, in real time, catch more faults than the rules teams already run — with fewer false items.",
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
        "Today’s rule is fixed severity plus deleting exact copies. It only reads logs — and on the data I use, "
        "82 percent of faults never write a log line.\n\n"
        "The goal is to catch more real faults than the rules teams already run, in real time, and to hand the "
        "on-call person a smaller, cleaner pile.",
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
        "34,863 alerts, 1,367 injected memory faults (all 1 GB for 10 min). Learn week 1 (3,630 / 144). Test the next 55 days "
        "(31,233 / 1,223). Logs are China time; memory is UTC — both aligned on load.",
        Inches(0.7),
        Inches(1.45),
        Inches(11.9),
        Inches(1.3),
        16,
        INK,
    )
    steps = (
        ("1  Detect", "Jump: > 0.5 GB over the last 15 min, 2 points. Leak: over the last 3 h. Gap: no data 10 min."),
        ("2  Threshold", "Show a jump only if its rise when confirmed passes a week-1 cost threshold (miss = 10×)."),
        ("3  Alerts", "Classifier ranks log alerts; they get ≤ 25% of slots. Alerts inside a shown jump ride on it."),
        ("4  Budget K", "One list per day, first come first served. Nothing chosen with hindsight."),
    )
    for i, (title, body) in enumerate(steps):
        x = Inches(0.5 + i * 3.2)
        panel(s, x, Inches(3.05), Inches(3.05), Inches(2.55))
        add_text(s, title, x + Inches(0.15), Inches(3.2), Inches(2.75), Inches(0.45), 16, GOLD, True)
        add_text(s, body, x + Inches(0.15), Inches(3.7), Inches(2.75), Inches(1.7), 14, INK)
    add_text(
        s,
        "Works with no labels on day one. Shadow mode → Real / Noise marks → retrain on the team’s own data.",
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
        "Four steps. Detect: a jump is memory more than half a gigabyte above its own last fifteen minutes; "
        "a leak is the same over three hours; a gap is a service that stopped sending data. "
        "Threshold: a jump is shown only if its rise when confirmed passes a threshold learned on week one. "
        "Alerts: a classifier ranks log alerts, and they may use a quarter of the slots. "
        "Budget: one list of K items a day, first come first served — nothing is chosen with hindsight.\n\n"
        "It works with no labels on day one. Run in shadow mode, mark Real or Noise, retrain on your own data.",
    )

    # --- Slide 4 Evaluation ---
    s = prs.slides.add_slide(blank_layout)
    add_bar(s)
    add_text(s, "4  ·  EVALUATION  ·  TEST PERIOD, 1,223 FAULTS", Inches(0.6), Inches(0.25), Inches(11), Inches(0.35), 14, GOLD, True)
    add_text(
        s,
        "Against the rules teams already run, in real time",
        Inches(0.6),
        Inches(0.55),
        Inches(12),
        Inches(0.5),
        26,
        NAVY,
        True,
    )

    rows = (
        ("At 20 items / day", "Faults caught", "Items / day", "False / day"),
        ("Log severity + duplicates", "0%", "0.8", "0.8"),
        ("Static rule: memory > p99", "57.2%", "16.0", "3.0"),
        ("Prometheus rate rule: delta(mem[5m]) > 0.5 GB", "68.9%", "17.4", "2.1"),
        ("Jump rule + week-1 threshold", "72.6%", "17.1", "1.0"),
        ("Full queue: jumps + leaks + gaps + alerts", "70.2%", "17.6", "1.6"),
    )
    table = s.shapes.add_table(len(rows), 4, Inches(0.55), Inches(1.2), Inches(12.2), Inches(2.9)).table
    widths = (Inches(6.4), Inches(2.0), Inches(1.9), Inches(1.9))
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
                    run.font.size = Pt(15)
                    run.font.bold = i == 0 or i >= 4
                    run.font.color.rgb = WHITE if i == 0 else NAVY if i >= 4 else INK
            fill = cell.fill
            fill.solid()
            fill.fore_color.rgb = NAVY if i == 0 else PANEL if i >= 4 else WHITE

    add_text(
        s,
        "Best possible at K=20: 84.9% (22 faults a day). Misses: 46% metric outages, 52% full budget, 2% detector. "
        "Walk-forward (re-learned each week): detector beats the rate rule 8/8 weeks, 74.8% vs 70.7%.",
        Inches(0.6),
        Inches(4.25),
        Inches(12.2),
        Inches(0.85),
        16,
        INK,
    )
    add_text(
        s,
        "Caution: every GAIA fault is 1 GB for 10 minutes — say “on this dataset.” Synthetic replay on the real series: "
        "0.5–2 GB caught ≥ 98%, 0.25 GB not; slow leaks need the leak rule (73–99%). Confirmed in 0.7 min.",
        Inches(0.6),
        Inches(5.1),
        Inches(12.2),
        Inches(1.2),
        16,
        MUTED,
    )
    add_notes(
        s,
        "Success is fixed before I look at the scores. Learn on the first week. Test on the fifty-five days after it — "
        "1,223 injected faults. I never shuffle the days, and nothing may use information from later in the day.\n\n"
        "I compare against rules a team already runs, through the same queue. At twenty a day, log severity catches none. "
        "A static memory threshold catches 57 percent. A Prometheus rate rule catches 69 percent with about two false items a day.\n\n"
        "My jump rule with a week-one threshold catches 73 percent with one false item a day. The full queue, which also shows "
        "slow leaks, gaps and a few alerts, catches 70 percent. The best possible is 85 percent, because GAIA injects 22 faults a day.\n\n"
        "Of the misses, almost half are metric outages and half are days with more faults than slots; two percent are the detector. "
        "Re-learned every week, the detector beats the rate rule in all eight weeks.\n\n"
        "Caution: every GAIA fault is the same one-gigabyte program. On the real series, half a gigabyte to two are caught, "
        "a quarter is not, and slow leaks need the leak rule.",
    )

    # --- Slide 5 Deliverables ---
    s = prs.slides.add_slide(blank_layout)
    add_bar(s)
    add_text(s, "5  ·  DELIVERABLES", Inches(0.6), Inches(0.25), Inches(6), Inches(0.35), 14, GOLD, True)
    add_text(
        s,
        "What you can look at — and use",
        Inches(0.6),
        Inches(0.55),
        Inches(12),
        Inches(0.55),
        28,
        NAVY,
        True,
    )
    items = (
        ("Honest evaluation", "Every system through one real-time queue; walk-forward; run_validate.py fails if docs and numbers disagree."),
        ("Detectors", "Memory jumps (99.6% of faults with data), slow leaks, monitoring gaps. Robustness replay."),
        ("Queue policy", "Budget K, alerts ≤ 25%, alerts ride on jumps. Carry-over option: 74.3% at K=20."),
        ("A tool others can use", "CLI for any export; web app in shadow mode; Alertmanager, Prometheus, PagerDuty; no labels needed."),
    )
    for i, (title, body) in enumerate(items):
        x = Inches(0.5 + (i % 2) * 6.4)
        y = Inches(1.35 + (i // 2) * 2.15)
        panel(s, x, y, Inches(6.15), Inches(2.0))
        add_text(s, title, x + Inches(0.25), y + Inches(0.2), Inches(5.7), Inches(0.4), 18, GOLD, True)
        add_text(s, body, x + Inches(0.25), y + Inches(0.7), Inches(5.7), Inches(1.1), 16, INK)
    add_text(
        s,
        "Scripts: run_queue_eval.py, run_walkforward.py, run_robustness.py  ·  python -m product.cli score  ·  Thank you.",
        Inches(0.6),
        Inches(5.8),
        Inches(12.2),
        Inches(0.6),
        16,
        MUTED,
    )
    add_notes(
        s,
        "Four things you can look at. The evaluation: every system through one real-time queue, a walk-forward check, "
        "and a script that fails if the documents and numbers disagree. The detectors: memory jumps, slow leaks and gaps. "
        "The queue policy, with the classifier for the log share. And a tool another team can use today: a command-line "
        "scorer for their own export, and a web app in shadow mode with Alertmanager, Prometheus and PagerDuty that works "
        "without labels and learns from Real and Noise marks.\n\n"
        "That is the project: budgeted incident detection for cloud microservice on-call, tested honestly against the rules "
        "teams already run. Thank you. I am happy to take questions.",
    )

    prs.save(OUT)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    build()
