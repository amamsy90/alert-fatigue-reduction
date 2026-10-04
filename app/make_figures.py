"""Draw the report figures from the result csv files.

    python make_figures.py

Writes the SVG figures (no extra library) and, when matplotlib and seaborn
are installed, the same two plots as PNG.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "figures"
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#1f1f1e", "#6b6a64", "#e4e3dd"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
FONT = 'font-family="Arial, Helvetica, sans-serif"'


def _svg(width: int, height: int, title: str, desc: str, body: list[str]) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">',
            f'  <title id="t">{title}</title>',
            f'  <desc id="d">{desc}</desc>',
            f'  <rect width="{width}" height="{height}" fill="{SURFACE}"/>',
            *body,
            "</svg>",
        ]
    )


def detection_vs_k() -> None:
    q = pd.read_csv("results/queue_eval_gaia.csv")
    ks = sorted(q["budget_k"].unique())
    series = [
        ("proposed", "Full queue (jumps + gaps + alerts)", BLUE),
        ("delta_5m", "Rate rule delta(mem[5m]) > 0.5 GB", ORANGE),
        ("logs_severity", "Log severity + duplicates", AQUA),
    ]
    W, H, L, R, T, B = 680, 360, 60, 250, 56, 300
    x = {k: L + i * (W - L - R) / (len(ks) - 1) for i, k in enumerate(ks)}

    def y(v: float) -> float:
        return B - v * (B - T)

    body = [f'  <text x="{L}" y="30" {FONT} font-size="15" fill="{INK}">Faults caught vs daily budget K '
            f'(test period, {int(q["faults"].iloc[0]):,} faults)</text>']
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        body.append(f'  <line x1="{L}" y1="{y(v):.1f}" x2="{W - R}" y2="{y(v):.1f}" stroke="{GRID}"/>')
        body.append(f'  <text x="{L - 8}" y="{y(v) + 4:.1f}" text-anchor="end" {FONT} font-size="11" fill="{MUTED}">{v:.0%}</text>')
    for k in ks:
        body.append(f'  <text x="{x[k]:.1f}" y="{B + 20}" text-anchor="middle" {FONT} font-size="12" fill="{MUTED}">K={k}</text>')
    oracle = q[q["queue"] == "proposed"].set_index("budget_k")["oracle_ceiling"]
    pts = " ".join(f"{x[k]:.1f},{y(oracle[k]):.1f}" for k in ks)
    body.append(f'  <polyline points="{pts}" fill="none" stroke="{MUTED}" stroke-width="1.5" stroke-dasharray="4 4"/>')
    body.append(f'  <text x="{x[ks[-1]] + 10:.1f}" y="{y(oracle[ks[-1]]) - 10:.1f}" {FONT} font-size="12" fill="{MUTED}">Best possible</text>')
    offsets = {"proposed": -2, "delta_5m": 14, "logs_severity": 4}
    for name, label, color in series:
        vals = q[q["queue"] == name].set_index("budget_k")["detection_rate"]
        pts = " ".join(f"{x[k]:.1f},{y(vals[k]):.1f}" for k in ks)
        body.append(f'  <polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round"/>')
        for k in ks:
            body.append(f'  <circle cx="{x[k]:.1f}" cy="{y(vals[k]):.1f}" r="4" fill="{color}" stroke="{SURFACE}" stroke-width="2"/>')
        k20 = vals.get(20)
        if k20 is not None and name != "logs_severity":
            body.append(f'  <text x="{x[20]:.1f}" y="{y(k20) - 10 if name == "proposed" else y(k20) + 20:.1f}" '
                        f'text-anchor="middle" {FONT} font-size="12" fill="{INK}">{k20:.1%}</text>')
        body.append(f'  <text x="{x[ks[-1]] + 10:.1f}" y="{y(vals[ks[-1]]) + offsets[name]:.1f}" {FONT} font-size="12" fill="{INK}">{label}</text>')
    desc = "; ".join(
        f"{label}: " + ", ".join(f"K={k} {q[(q.queue == name) & (q.budget_k == k)].detection_rate.iloc[0]:.1%}" for k in ks)
        for name, label, _ in series
    )
    (OUT / "detection_vs_k.svg").write_text(_svg(W, H, "Faults caught versus daily budget K", desc, body), encoding="utf-8")


def walkforward() -> None:
    w = pd.read_csv("results/walkforward_gaia.csv")
    folds = sorted(w["fold"].unique())
    series = [("delta_5m", "Rate rule", ORANGE), ("proposed", "Full queue", BLUE)]
    W, H, L, R, T, B = 680, 360, 60, 20, 70, 290
    group = (W - L - R) / len(folds)
    bar = (group - 18) / 2

    def y(v: float) -> float:
        return B - v * (B - T)

    body = [f'  <text x="{L}" y="30" {FONT} font-size="15" fill="{INK}">Walk-forward: faults caught each later week (K=20)</text>']
    lx = L
    for _, label, color in series:
        body.append(f'  <rect x="{lx}" y="42" width="12" height="12" rx="2" fill="{color}"/>')
        body.append(f'  <text x="{lx + 18}" y="52" {FONT} font-size="12" fill="{INK}">{label}</text>')
        lx += 110
    body.append(f'  <text x="{lx + 10}" y="52" {FONT} font-size="12" fill="{MUTED}">% under each week = faults with metric data</text>')
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        body.append(f'  <line x1="{L}" y1="{y(v):.1f}" x2="{W - R}" y2="{y(v):.1f}" stroke="{GRID}"/>')
        body.append(f'  <text x="{L - 8}" y="{y(v) + 4:.1f}" text-anchor="end" {FONT} font-size="11" fill="{MUTED}">{v:.0%}</text>')
    lines = []
    for i, fold in enumerate(folds):
        part = w[w["fold"] == fold].set_index("queue")
        gx = L + i * group + 9
        for j, (name, _, color) in enumerate(series):
            v = float(part.loc[name, "detection_rate"])
            bx = gx + j * (bar + 2)
            h = max(0.0, B - y(v))
            # rounded top only: a rounded rect plus a square patch at the baseline
            body.append(f'  <rect x="{bx:.1f}" y="{y(v):.1f}" width="{bar:.1f}" height="{h:.1f}" rx="4" fill="{color}"/>')
            body.append(f'  <rect x="{bx:.1f}" y="{B - min(h, 4):.1f}" width="{bar:.1f}" height="{min(h, 4):.1f}" fill="{color}"/>')
            if name == "proposed":
                body.append(f'  <text x="{bx + bar / 2:.1f}" y="{y(v) - 6:.1f}" text-anchor="middle" {FONT} font-size="11" fill="{INK}">{v:.0%}</text>')
        start = pd.Timestamp(part["train_until"].iloc[0])
        data = float(part["faults_with_metric_data"].iloc[0])
        cx = L + i * group + group / 2
        body.append(f'  <text x="{cx:.1f}" y="{B + 18}" text-anchor="middle" {FONT} font-size="11" fill="{MUTED}">{start:%d %b}</text>')
        weight = ' font-weight="bold"' if data < 0.7 else ""
        body.append(f'  <text x="{cx:.1f}" y="{B + 34}" text-anchor="middle" {FONT} font-size="11" fill="{INK}"{weight}>{data:.0%}</text>')
        lines.append(f"week of {start:%d %b}: rate rule {float(part.loc['delta_5m', 'detection_rate']):.1%}, "
                     f"full queue {float(part.loc['proposed', 'detection_rate']):.1%}, metric data {data:.0%}")
    body.append(f'  <text x="{L}" y="{H - 8}" {FONT} font-size="11" fill="{MUTED}">Week of 19 Aug: metrics stopped on every service for 2.5 days.</text>')
    (OUT / "walkforward.svg").write_text(_svg(W, H, "Walk-forward detection by later week", "; ".join(lines), body), encoding="utf-8")


def publication_pngs() -> None:
    """Same two comparisons, drawn with seaborn from the result tables."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import seaborn as sns

    sns.set_theme(style="whitegrid")
    labels = {
        "proposed": "Full queue",
        "delta_5m": "Rate rule",
        "logs_severity": "Log severity",
    }
    colors = {"Full queue": BLUE, "Rate rule": ORANGE, "Log severity": AQUA, "Best possible": MUTED}

    q = pd.read_csv("results/queue_eval_gaia.csv")
    shown = q[q["queue"].isin(labels)].copy()
    shown["queue"] = shown["queue"].map(labels)
    oracle = q[q["queue"] == "proposed"][["budget_k", "oracle_ceiling"]].copy()
    oracle["queue"] = "Best possible"

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    sns.lineplot(data=shown, x="budget_k", y="detection_rate", hue="queue",
                 hue_order=["Log severity", "Rate rule", "Full queue"],
                 palette=colors, marker="o", ax=ax)
    ax.plot(oracle["budget_k"], oracle["oracle_ceiling"], color=MUTED, linestyle="--",
            label="Best possible")
    ax.legend(title=None)
    ax.set(xlabel="Daily budget K", ylabel="Faults caught",
           title="Faults caught versus daily budget K")
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: f"{v:.0%}"))
    ax.set_xticks(sorted(shown["budget_k"].unique()))
    fig.text(0.01, 0.01, "Test period, 1,223 faults. Each point is that budget, not a confidence interval.",
             fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(OUT / "detection_vs_k.png", dpi=150)
    plt.close(fig)

    w = pd.read_csv("results/walkforward_gaia.csv")
    week = w[w["queue"].isin(["proposed", "delta_5m"])].copy()
    week["queue"] = week["queue"].map(labels)
    week["week_start"] = pd.to_datetime(week["train_until"])
    week["week"] = week["week_start"].dt.strftime("%d %b")
    order = week.sort_values("week_start")["week"].drop_duplicates().tolist()
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    sns.barplot(data=week, x="week", y="detection_rate", hue="queue",
                order=order, palette=colors, ax=ax)
    ax.set(xlabel="Week starting", ylabel="Faults caught",
           title="Walk-forward detection by later week (K = 20)")
    ax.set_ylim(0, 1)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _p: f"{v:.0%}"))
    fig.text(0.01, 0.01,
             "Each bar is that week's detection rate. The spread is week-to-week variation, not a confidence interval.",
             fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(OUT / "walkforward.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    detection_vs_k()
    walkforward()
    written = [OUT / "detection_vs_k.svg", OUT / "walkforward.svg"]
    try:
        publication_pngs()
    except ImportError as exc:
        print(f"SVG only; PNG needs matplotlib and seaborn ({exc})")
    else:
        written.extend([OUT / "detection_vs_k.png", OUT / "walkforward.png"])
    print("Wrote " + ", ".join(str(p) for p in written))
