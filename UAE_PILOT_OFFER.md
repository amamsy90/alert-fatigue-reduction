# UAE Pilot Offer — Budgeted On-Call Queue

**From:** Aishah Mohamed Alyammahi, founder  
**Product:** Alert fatigue reduction — a causal daily queue that shows at most **K** items to on-call  
**Repo (reference):** https://github.com/amamsy90/alert-fatigue-reduction  
**For:** SRE / platform leads and MSPs exploring a short paid pilot in the UAE  

---

## The problem

On-call teams still get page storms: hundreds of alerts a day while a person can act on only a handful. Severity rules and exact-duplicate suppression leave real metric faults buried, and attention runs out before the list does. The useful question is not “did we alert?” — it is **given a hard budget of K items a day, which ones should someone see, as they happen?**

## What this pilot is

A **2–4 week** paid pilot on *your* stack (or a representative slice of it). Default posture is safe:

| Path | What we do |
|---|---|
| **Evaluate** | Run *Evaluate my data* on a metrics CSV (plus optional past incidents / alerts), **or** pull a sample from Prometheus. Tune on an early window; score the rest against your current severity path, simple metric rules, and a random-chance line. |
| **Shadow (default)** | Run the budgeted queue alongside your tools. Nothing pages. After each run you see a **would-have-paged** view — what Live would have opened — so you can judge value without changing paging. |
| **Live PagerDuty (optional)** | Only if you agree mid-pilot: save a routing key and switch Connectors to Live. Sample / Evaluate demos never page; only agreed ingest jobs do. |

Connectors in scope: **Alertmanager** webhook, **Prometheus** pull, **PagerDuty** (shadow or Live). Ranking uses causal metric rules first; an optional scikit-learn alert ranker kicks in when you have Real/Noise marks or labels.

## Deliverables

1. **Ranked / budgeted queue demo** on your data (web UI + export), with K you choose (e.g. 10–20/day).  
2. **Baseline vs K-budget metrics** — faults/problems caught, false items, and comparison to “alerts by severity” and simple Prometheus-style rules under the same K.  
3. **Go / no-go note** — plain recommendation: keep shadow, move to Live on a slice, change K, or stop. No long deck required.

## What we need from you

- **Data:** either (a) metrics CSV (+ optional incidents and alerts CSVs), or (b) a read-only Prometheus URL + example query / service label mapping.  
- **One contact** who can answer stack questions and confirm shadow vs Live.  
- Optional: Alertmanager webhook access and/or a PagerDuty routing key if you want Live in week 3–4.

Setup is Docker or a small VM; login and ingest token supported so the instance is not left open.

## Commercial sketch (editable)

| Item | Placeholder |
|---|---|
| **Pilot fee** | **AED \[X,000 – Y,000\]** for 2–4 weeks (fixed scope above). Edit before send. |
| **Hosting** | Option for **UAE / data-residency hosting** (customer VPC, or agreed UAE region) so metrics and marks stay where you need them. |
| **After pilot** | Separate commercial discussion if you continue (subscription or support). Not priced here. |

This note is an outreach sketch, not a contract. Hosting, liability, and data processing terms would sit in a short pilot agreement you and counsel review. No claim is made about regulated-sector certification unless we add that in writing.

## Next step

Reply with a preferred window and whether you start from **CSV Evaluate** or **Prometheus pull**. I will send a one-page data checklist and a calendar slot for a 30-minute scoping call.

**Contact:** \[your email / phone — edit before send\]  
**Company / trade name on the invoice:** \[placeholder — UAE trade-license path under discussion; use personal or licensed entity as agreed\]

—
Aishah Mohamed Alyammahi  
Budgeted on-call queue · UAE pilot outreach  
