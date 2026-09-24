# Speaker notes — Alert Fatigue Reduction

**Sector:** cloud microservice operations (online platforms). Keep every answer in that lane.

Seven minutes. Five slides. Read this. Leave the slides on the screen.

Say your own name when you start. On slide 2, if you have a closer personal line — a platform you have watched, a shift — say that sentence, then return to the budget line.

---

## Slide 1 — Motivation (0:00–1:20)

Good morning. My proposal is Alert Fatigue Reduction for cloud microservice operations — the on-call queue behind an online platform made of many small services.

When the person on duty can check only a few warnings, the system should show the real problem and keep the noise out of the way.

Those services do not pause. One of them can raise thousands of alerts in a day, and most need no action. Some repeat. Others look different but come from one fault on one container.

The person on duty can read only a handful in an hour. That is alert fatigue. The real incident is in the queue, and it is missed because attention ran out, not because the monitor was silent. Users of the platform pay that delay.

Today’s rule is fixed severity, plus deleting exact copies. It does not read the alert, it does not see that a memory jump and the logs around it are one incident, and it does not change when five slots are free or fifty.

The goal is to show more real faults than those rules, and to hand the on-call person a smaller pile.

---

## Slide 2 — Impact (1:20–2:20)

This is about one sector: cloud microservice platforms — login, database, cache, web — not banks and hospitals as separate industries. When a warning is buried in that queue, the people using the online service wait longer, or the service goes down. A night of false alarms also teaches the on-call engineer to stop trusting the queue, so a fault that was already detected becomes an outage for users.

For me, the point is the decision, not the model. I want one sentence I can stand behind: at this budget, these faults on this platform were shown to a person, and these were not. If the person on duty still misses the fault, a high score on paper is not success.

---

## Slide 3 — Implementation and data (2:20–4:00)

I will build this in Python, in four steps. The data matches the sector.

GAIA, the Generic AIOps Atlas, MicroSS scenario, from CloudWise on GitHub: about two months of microservice logs, plus container memory for each service. Every fault was injected and written down — a program that holds extra memory for about ten minutes on one service. So I know the right answer, and I do not trust the alert’s own severity. The logs are China time and the memory files are UTC, so lining the clocks up is part of loading. Learn on the first week. Test on the fifty-five days after it.

Then four steps. Align each alert to the fault on the same service. Classify, with a miss weighing ten times one extra alert. Watch memory: a jump is memory rising more than half a gigabyte above its own level in the last fifteen minutes, for at least two points. The half-gigabyte size was chosen on week one only. A single odd point is ignored. Spend the budget: one list of K items a day. The biggest jumps go first; leftover slots go to alerts — send it now, ask a person, or hide it. I test at five, ten, twenty, and fifty a day.

A company cannot drop in the GAIA-trained model as is. The app runs in shadow mode first. The on-call person marks items Real or Noise. Those marks become that company’s labels, and the model is retrained on them.

---

## Slide 4 — Evaluation (4:00–6:00)

Success is four checks, fixed before I look at the scores. Learn on the first week. Test on the fifty-five days after it. I will not shuffle the days. The baseline is fixed severity plus exact copies removed, on the same days.

The numbers on this slide are from that test period — 1,226 injected faults.

A memory jump overlaps 86 percent of those faults. The first rule, week-one average plus three standard deviations, only reached 54 percent.

At twenty items a day, the shared queue — jumps first, then alerts — catches 73 percent of the faults. The severity baseline catches none, because the log lines inside a fault are warnings, not errors. Alerts alone catch 13 percent. About 18 items a day, of which about 1.3 are false.

Two cautions, so I do not over-claim. Every GAIA fault is a memory program, so this dataset favours the memory rule. On a real platform some faults will not touch memory, and the alert classifier matters more. Say “on this dataset.” And a jump is only confirmed at its second point, so detection takes about two minutes, not the fraction of a minute the script prints.

The 95 percent detection target is not met at twenty items a day. Most injected faults leave no log line, so only the memory path can find them.

---

## Slide 5 — Deliverables (6:00–7:00)

Four things you can look at. A pipeline, with the baseline numbers on the shared split. A classifier, compared at the same budget. A memory-jump detector, scored on the injected faults. And a budget policy — send, ask, or hide — with the scripts to rerun the test period.

Those pieces are already running, plus a local on-call web app in shadow mode: Real and Noise buttons, a saved model, and a reminder to retrain after seven days or when new Real marks arrive.

That is the proposal: alert fatigue reduction for cloud microservice on-call, on public data with injected faults, tested after week one with four checks. Thank you. I am happy to take questions.

---

## Defence — only if someone asks

Do not say this in the seven minutes unless they ask.

**Where does the work stand?** The logs are China time and the memory files are UTC. After that alignment, the jump rule overlaps 86.1 percent of the 1,226 test-period faults (1,056), with 256 jumps that have no fault. The old mean-plus-3σ rule was 53.8 percent and 322 false jumps. At 20 items a day the shared queue (jumps first) catches 72.8 percent against 0 percent for the baseline, about 18 items and 1.3 false items a day. At 5 / 10 / 50 the catch rates are 19.2 / 38.8 / 86.0 percent.

**How do you explain a memory jump?** Memory on one service sits at its own recent level. A jump is that series rising more than 0.5 GB above the median of the previous 15 minutes, for at least two points no more than two minutes apart. That is one incident. The logs from that service during the jump belong to it. The 0.5 GB size was chosen on week 1 only.

**Can a company use a model trained on open data?** Not as is. Their services and alert text differ, and they have no injected-fault labels. Keep the queue and the jump rule. Run in shadow mode. Mark items Real or Noise. Retrain on those labels. The app already does this.

**Why does the baseline catch 0%?** The log lines inside a GAIA memory fault are warnings, not errors, so a fixed severity rule never escalates them. Most injected faults have no log line at all.

**Why not 95% at K=20?** The 95 percent target assumed every fault would leave an alert. On this data, 70.9 percent of test faults have a jump and no log. A log-only system is capped near 18 percent. At K=50 every jump fits and detection reaches 86 percent, which is the memory-rule ceiling (86.1 percent of faults have an overlapping jump). 11.3 percent are silent: no log and no jump.

**Was 72.8% a lucky month?** No. Retrain each later week: mean 74.7 percent, 95 percent interval 67.3 to 80.8, and the queue beats the baseline in all eight weeks. The weak week (52.6 percent) showed 11.8 items a day, so the budget of 20 was not full. Fewer qualifying jumps that week, not a full queue.

**Should you change the dataset?** No. Every injection here is a memory program. That is the finding, not a bug. A new mixed-fault set would need CPU and disk series we do not have.
