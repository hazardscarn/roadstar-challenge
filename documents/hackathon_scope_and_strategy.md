# RoadStar Hackathon 2026 — Scope Analysis & Strategy for a Data Science Team

## Short answer to "is this UI/dashboard-only?"

**No.** A dashboard is the required *delivery wrapper*, but it is explicitly **not** the thing
being scored most heavily. Of the 7 judging criteria published in the Project Brief, only one
("UI/UX & Usability") is about interface polish. The other six are about the algorithms and
data behind the interface:

| Judging criterion | What it's actually rewarding |
|---|---|
| Workflow Speed & Financial Value | Does your **automation logic** actually eliminate manual dispatcher work? |
| Geofencing & Detention Precision | Correct **computation** of dwell time vs. the 2-hour free threshold |
| Problem Discovery & Innovation | Depth of **data exploration** — found edge cases, hidden patterns |
| Mapping & Track & Trace Depth | Quality of the **simulation/telemetry model**, not just a pretty map |
| HOS & Regulatory Logic | A working **compliance/rules engine** (or predictive risk model) |
| Simulation Engine & Real-Time Sync | Realism of a **generative/synthetic data model** of truck movement |
| UI/UX & Usability | Interface quality |

The organizers say it themselves: *"Optimize and Automate workflows that are mostly done
manually"* and *"Freight optimization engines and intelligent routing"* are explicitly listed as
desired outputs (slide 13 of the sponsor deck; the "About" page lists "AI-powered predictive
analytics for logistics" as one of seven build categories). A team that ships only a static
dashboard on top of the raw tables will score fine on UI/UX and weak everywhere else. A team that
ships a real optimization/prediction engine — even behind a minimal UI — is directly aligned with
the majority of the rubric, and it's the harder thing for the many UI/frontend-focused teams at a
7-day hybrid hackathon to also build well. **That's exactly your team's edge as data scientists.**

## Where the real AI/DS scope lives

The brief names four required system components. Three of the four are fundamentally data/ML
problems, not frontend problems:

1. **Dispatcher Dashboard** — UI, but its two "critical requirement" features are computational:
   - *Geofence & Detention Automated Billing*: given arrival/departure timestamps, compute
     billable detention beyond a 2-hour free threshold. The historical data already contains the
     needed timestamps (`Dispatch.LS_DET_PICK_ARRIVE` / `LS_DET_DELV_ARRIVE`), so you can build
     and **backtest this on real history**, not just simulate it live.
   - *Automated load-matching engine* (deadhead/empty-mile reduction): "pairing a Milton-to-London
     delivery with a London-to-Kitchener return shipment" is literally describing a **vehicle
     routing / assignment optimization problem**. This is squarely a data-science/OR problem
     (constraint programming, greedy/heuristic matching, or a light RL formulation), not a UI one.

2. **Simulation Engine** — explicitly asked to be a *separate* backend service that "simulates
   live truck movements... updating GPS, speed, odometer, HOS state" and "simulates route delays,
   dock wait times, duty cycle shifts." This is a synthetic-data-generation / discrete-event
   simulation problem. You can seed it with real distributions pulled from the historical data
   (actual speeds, actual dwell times, actual route distances by lane) so the simulation is
   statistically grounded rather than arbitrary — a natural fit for a data science skill set.

3. **Driver Interface** — the one genuinely UI-heavy component (mobile/web view for
   accepting loads and logging duty status). This is the smallest, most commoditized piece — treat
   it as a thin client, don't over-invest here.

4. **Pipeline & Integration** — "multi-system consolidation." Also plumbing/engineering, not
   core-AI, but necessary connective tissue.

Plus an explicit **bonus scoring lever**: *"Edge Case / Hidden Problem Discovery... Finding and
solving these real-world edge cases elevates your project score."* This is a direct invitation
for exploratory data analysis. The data dictionary work above already surfaced several genuine,
non-obvious findings you can present as "edge cases we discovered":

- The fleet clearly runs **cross-border US/Canada freight** (`Driver.DRIVER_CYCLE_ZONE` = U/C,
  separate `REMAINING_HOURS_CAN_*` and `REMAINING_HOURS_US_*` fields, ~2,400 of 4,031 orders
  cross a US border) — but the brief's HOS section only describes Canadian rules. A dual-jurisdiction
  compliance engine that reconciles FMCSA (US) vs. Transport Canada (CA) HOS limits when a driver's
  route crosses the border is a genuine, hidden regulatory edge case the hosts likely didn't
  anticipate teams would find.
- **316 orders (7.8%) were quoted but never dispatched** — a real-world proxy for the brief's
  "$15k–$50k/month in missed quotes" framing. You could build a model that flags at-risk
  quotes/orders in real time (before they go stale) using the same features (lead time between
  creation and pickup deadline, lane, customer) that distinguish dispatched vs. non-dispatched
  historical orders.
- **Zone codes are customer/facility codes, not fixed locations** (one code maps to up to 16
  different cities) — meaning any dispatch system built purely on zone-code lookups (as a naive
  team might do, trusting the code as a location key) will silently mis-plot loads on the map.
  Catching and fixing this is a legitimate "software bug in standard city dispatch operations"
  finding, which is literally what the brief asks teams to hunt for.
- The `LS_TRAILER1` field in `Dispatch` **doesn't join to the `Trailers` roster at all** — so
  axle-weight compliance (also explicitly required) can't be computed by joining real trailer
  specs; you have to either flag this gap to judges (strong "problem discovery" points) or work
  around it with type-level averages.

## Where an LLM fits (and where it's overkill)

The datasets and requirements are **not** an LLM-shaped problem at their core — HOS compliance,
detention billing, and route optimization are deterministic/rules/optimization problems that an
LLM would do worse and less verifiably than a rules engine or optimizer. Don't force an LLM to
do arithmetic or compliance logic it will get subtly wrong. Good, defensible uses of the "AI
credits" the hosts are providing (per `abouthackathon.md` and the SPUR Innovation sponsor slide):

- **Natural-language dispatcher assistant**: a chat layer over your rules engine / optimizer —
  "which drivers have >6 hours left and are within 50 miles of Milton?" — that translates intent
  into a query against your structured data and HOS engine, then narrates the structured result.
  This is the classic, safe "LLM as translator/orchestrator over deterministic tools" pattern, and
  demos very well to non-technical judges.
- **Auto-generated load quotes / rate justification text** for the quoting-speed pain point the
  brief opens with (dispatchers "bogged down... instead of focusing on high-value quoting").
- **Summarizing edge cases / anomalies** your data pipeline detects, into a plain-English
  "problem discovery" narrative for the judging demo — directly targets that criterion.
- Optionally, a lightweight **RL or heuristic-search layer for the load-matching/deadhead-reduction
  engine** (component 1's "automated load-matching") — this is legitimate reinforcement-learning
  territory (state = driver/truck positions + HOS + open loads; action = assignment; reward =
  revenue minus deadhead miles) if you want to lean into RL specifically, though a greedy/ILP
  baseline will already beat "no matching" and is far faster to get demo-ready in a week.

## Recommended project shape for a data-science-strength team

Given a 7-day hybrid format (kickoff Sept 5, demos Sept 13) and a team that is strong at modeling
but not at frontend polish, the highest-leverage allocation is roughly:

1. **Backend/engine-first (bulk of the week)**: build the detention-billing calculator, HOS
   compliance/violation-risk engine (dual-jurisdiction), and a load-matching/deadhead-reduction
   optimizer — all validated against the real historical `Tlorder`/`Dispatch` data with concrete
   before/after numbers (e.g. "X% deadhead miles recoverable," "$Y/month in detention previously
   unbilled"). These numbers directly answer the "Workflow Speed & Financial Value" criterion with
   evidence, not a claim.
2. **A synthetic simulation engine seeded from real distributions** (speeds, dwell times, lane
   distances pulled from the data) to produce a believable live-tracking demo — satisfies the
   "Simulation Engine & Real-Time Sync" criterion without needing real telematics hardware.
3. **A minimal but clean dashboard** (map + a few key panels: detention $ recovered, HOS
   compliance status, matched-load suggestions) — enough to satisfy UI/UX, but intentionally not
   the primary investment. Consider a fast low-code path (a templated map + table UI) so effort
   stays weighted toward #1–#2.
4. **A short, evidence-backed "edge cases we found" section in the demo** — pull directly from the
   data-quality findings in `data_dictionary.md` (cross-border HOS, zone-code ambiguity, broken
   trailer join, undispatched-quote leakage). Judges explicitly reward this and it's free — you've
   already done the analysis.
5. **One narrow, well-executed LLM feature** (the NL dispatcher assistant is the best ROI) rather
   than spreading LLM use thin across many features.

This plays directly to a data-scientist team's strengths (real analysis, real optimization, real
validated numbers) while still checking every box the rubric and Project Brief ask for — without
competing head-to-head with frontend-focused teams on visual polish, which isn't where most of
the score lives.
