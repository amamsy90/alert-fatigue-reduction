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

Then four steps. Align each alert to the fault on the same service. Classify, with a miss weighing ten times one extra alert. Watch memory: a jump is memory rising more than half a gigabyte above its own level in the last fifteen minutes, for at least two points. The half-gigabyte size was chosen on week one only. A single odd point is ignored. Spend the budget: one list of K items a day. Items are shown the moment they are confirmed, first come first served; alerts may use a quarter of the slots — send it now, ask a person, or hide it. If metrics stop for ten minutes, that gap is an item too. I test at five, ten, twenty, and fifty a day.

A company cannot drop in the GAIA-trained model as is. The app runs in shadow mode first. The on-call person marks items Real or Noise. Those marks become that company’s labels, and the model is retrained on them.

---

## Slide 4 — Evaluation (4:00–6:00)

Success is fixed before I look at the scores. Learn on the first week. Test on the fifty-five days after it — 1,223 injected faults. I never shuffle the days, and nothing in the queue may use information from later in the day.

I compare against rules a team already runs, through the same queue. At twenty items a day: log severity catches none, because the lines inside a fault are warnings. A static memory threshold catches 57 percent. A Prometheus rate rule — memory up half a gigabyte in five minutes — catches 69 percent with about two false items a day.

My jump rule with a threshold learned on week one catches 73 percent with one false item a day. The full queue, which also shows slow leaks, monitoring gaps and a few log alerts, catches 70 percent. The best any method could do at twenty a day is 85 percent, because GAIA injects twenty-two faults a day.

Of the faults I miss, almost half fall in metric outages and half are days with more faults than slots. Two percent are the detector's fault. Re-learned every week, the detector beats the rate rule in all eight later weeks.

Two cautions. Every GAIA fault is the same one-gigabyte memory program — say "on this dataset." I tested other sizes and slow leaks on the real series: half a gigabyte to two gigabytes are caught, a quarter is not, and leaks need the separate leak rule.

---

## Slide 5 — Deliverables (6:00–7:00)

Four things you can look at. The evaluation: every system through one real-time queue, with a walk-forward check and a script that fails if the documents and the numbers disagree. The detectors: memory jumps, slow leaks and monitoring gaps. The queue policy, with the alert classifier for the log share. And a tool another team can use today: a command-line scorer for their own export, and a web app in shadow mode with Alertmanager, Prometheus and PagerDuty, that works without labels and learns from Real and Noise marks.

That is the project: budgeted incident detection for cloud microservice on-call, on public data with injected faults, tested honestly against the rules teams already run. Thank you. I am happy to take questions.

---

## Defence — only if someone asks

Do not say this in the seven minutes unless they ask.

**Where does the work stand?** The logs are China time and the memory files are UTC. After that alignment, the jump rule overlaps 86.1 percent of the 1,223 test faults, and 99.6 percent of the faults that have metric data. At 20 items a day, in real time, the jump rule with a learned threshold catches 72.6 percent and the full queue 70.2 percent. A normal Prometheus rate rule catches 68.9 percent with twice the false items. The best possible at 20 a day is 84.9 percent, because GAIA injects 22 faults a day.

**How do you explain a memory jump?** Memory on one service sits at its own recent level. A jump is that series rising more than 0.5 GB above the median of the previous 15 minutes, for at least two points no more than two minutes apart. That is one incident. The logs from that service during the jump belong to it. The 0.5 GB size was chosen on week 1 only.

**Can a company use a model trained on open data?** Not as is. Their services and alert text differ, and they have no injected-fault labels. Keep the queue and the jump rule. Run in shadow mode. Mark items Real or Noise. Retrain on those labels. The app already does this.

**Why does the log baseline catch 0%?** The log lines inside a GAIA memory fault are warnings, not errors, so a fixed severity rule never escalates them. Most injected faults have no log line at all. That is why I also compare against metric rules a team would already have — the rate rule is the fair baseline. The detector beats it by 3.7 points with half the false items; the full queue by 1.3 points.

**Why not 95% at K=20?** The 95 percent target assumed every fault would leave an alert and that there are fewer faults than slots. On this data 71 percent of test faults have a jump and no log, and there are 22 faults a day for 20 slots, so the ceiling is 84.9 percent. 10.8 percent of faults fall in monitoring gaps, where no metric method can see them; the queue shows those gaps as items.

**Was it a lucky month?** No. Everything learned is re-learned before each later week: the jump rule with threshold averages 74.8 percent and beats the rate rule in all eight weeks; the full queue, which also spends slots on leaks, gaps and alerts, averages 72.5 percent. The weak week, 52 percent, is a 2.5-day metric outage on every service: only 61 percent of that week's faults have any metric data.

**Did the old version cheat?** The first queue ranked each day's jumps by their final peak, which needs the whole day in advance. I replaced it with a threshold on the rise at confirmation, learned on week one. Same score, fewer false items, and it works live.

**Should you change the dataset?** Not before submission. Every injection here is a memory program; the robustness replay covers other sizes and leaks on the real series. Other fault types (CPU, disk, code) are future work on another benchmark.


**Does it only work because every fault is 1 GB?** Partly, and I tested it. I added synthetic faults to the real memory series. Steps from half a gigabyte to two gigabytes, and one-minute spikes, are caught 98 to 100 percent of the time. A quarter of a gigabyte is not: it is below the jump size, so a team must set that size for its own services. Leaks that climb over half an hour or three hours defeat both my jump rule and the Prometheus rate rule, so I added a slow-leak rule: it catches 99 and 73 percent of them for a third of an item a day.

**Why are faults still missed?** At twenty a day I miss 364 faults. 166 are during metric outages, 191 are days with more faults than slots, and only 7 are the detector's fault.

**What did you try that did not work?** A learned model to rank jumps. The rise threshold already gives 94 percent precision, so it added nothing. Carrying unused slots to the next day did help: 74.3 percent at K=20, and it still beats the rate rule given the same carry-over.
