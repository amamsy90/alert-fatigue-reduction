# Day-12 home-lab snapshot

`day12-rebuilt-20261103T150116Z.tar.xz` is the frozen snapshot used for Table 2
in `FINAL_REPORT.md`. It was produced on ai-ROG and recompressed with xz.

| File | SHA-256 |
|---|---|
| `day12-rebuilt-20261103T150116Z.tar.xz` (this directory) | `378dc71f3ef4143662acc1d0a3dda8c5e32f7752f3ceb77efe9b88bf0985c56e` |
| Original `.tar.gz` upload (not stored; same bytes before xz) | `968940267defb49a6f92c93824369560c417fa8df4546d85c3009938cf9da782` |

The archive contains:

- `data/collected/metrics.csv`
- `data/collected/alerts.csv` — 1,182 episodes inside the metric window, the file the day-12 evaluator read
- `data/faults.csv`
- `alerts_rebuilt.csv` — 1,184 episodes from the Prometheus rebuild (`rebuild_summary.json`)
- `rebuild_summary.json`

The two extra rebuilt episodes fall outside the metric window, so the scored file has 1,182 episodes and the rebuild summary has 1,184. `summary.json` `test_end` is `2026-11-02T19:15:03Z`. The cutoff described with the run is `2026-11-02T19:15:11Z`. `rebuild_summary.json` `end` is `2026-11-02T19:15:00Z`.

Reproduce Table 2 from the repository root:

```bash
./homelab/scripts/reproduce_day12.sh
```
