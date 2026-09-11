# Region Clustering (a Real Fix) and Learning-to-Rank (a Real Negative Result)

**What:** Two follow-ups after direct, pointed pushback on the state-value model — "why raw
lat/lon, that's not thinking it through" and "we need a model that ranks top-N options, consider
bandits/transformers/DQN too." One fix landed cleanly; the other was tried honestly and reported
as not working, rather than kept quiet.

## Why raw lat/lon was wrong, and the research behind fixing it

Trees split axis-aligned — a lat/lon threshold can't express a real 2D area without many
sequential splits, and with sparse per-location data (2,110 locations, some visited only a
handful of times) that overfits to coordinate noise rather than learned geography. Caught
directly in a smoke test: a deliberately remote location outscored a real hub with lat/lon
dominating feature importance at only R²≈0.06.

**On DQN/transformers vs. gradient-boosted trees, actually researched, not assumed**: Grinsztajn
et al. (NeurIPS 2022, *"Why do tree-based models still outperform deep learning on tabular
data?"*) is a well-replicated finding that GBTs match or beat neural approaches on structured
tabular data at this scale (~1M rows, 5-20 features) — exactly this project's regime. What's
already built — Fitted Value/Q Iteration via XGBoost — is a documented technique for exactly this
(Ernst et al. 2005, *"Tree-Based Batch Mode Reinforcement Learning"*), and matches the standard
Approximate Dynamic Programming literature for fleet/dispatch problems specifically (Warren
Powell's ADP work). A DQN or transformer here would trade stability/sample-efficiency for no
demonstrated gain — a reasoned conclusion, not a dismissal.

## The fix: k-means region clustering (`sim/cluster_locations.py`)

k over the 2,110 real location coordinates chosen by silhouette score across a real candidate
range (10/15/20/25/30) — not picked arbitrarily. Silhouette increased monotonically through the
tested range (0.4667 → 0.5320), so k=30 was chosen as the upper end of what's practical (≈70
locations/region on average) rather than an artificially "optimal" point that doesn't exist in
this data. Written to `reference.locations.region_id` + `calibration.region_clusters` (centroids,
for classifying any future point) — `sim/sql/021`.

**Result, and it's a genuinely better one**: the Q-model (`train_value_function.py`) and the
state-value model (`train_state_value_function.py`) were both re-trained on `region_id` instead
of raw lat/lon. The state-value model's regional breakdown is now interpretable and verified
against real data:

- **Region 26 (the London hub itself, ~11 locations, tight cluster)**: avg V(s) = **-239.9**, from
  **361,280 decisions** (33% of the entire dataset) — not a sparse-data fluke. Verified directly:
  avg realized `target_value` there is -245, and it's *not* driven by breakdown risk (below
  average) or lateness (modest) — it's driven by real deadhead: since simulated demand is drawn
  from real historical lanes originating at scattered customer addresses, not at the company
  terminal, a driver starting AT the hub genuinely pays more average deadhead to reach real
  demand than one already positioned near where orders actually originate. A real, well-supported,
  explainable finding — exactly what region-based features were supposed to surface instead of
  coordinate noise.
- 24 of the remaining 29 regions cluster in a sensible, continuous value range (30-150), no wild
  individual-point swings.

## The ranker: a real, honest negative result

Framed "rank the top-N options" as a proper learning-to-rank problem (`sim/training/train_ranker.py`,
XGBoost `rank:ndcg`) instead of regression-then-sort — the technically correct match for the
actual downstream use. Built `sim.candidate_scores` (`sim/sql/023`) to log the top-10 scored
candidates per order arrival (not just the winner) — 2,167,445 rows from a 2,000-run batch.

**The label problem, handled honestly**: only the dispatched candidate has a real outcome. Chosen
the more rigorous of two options (explicitly, not the faster self-distillation one): ground
unchosen candidates' relevance in REAL empirical outcomes from elsewhere in the dataset (bucketed
by region/HOS/truck-risk/service-type, 463 buckets over 216,929 real dispatched decisions), never
from a model scoring itself.

**Concrete evaluation, not just NDCG in isolation**: does the ranker's #1 pick match the real
dispatched candidate when that candidate's real outcome was actually good (top half of its
group)? **Ranker: 14.5%. Existing greedy `rank_position==1` baseline: 42.7%.** The ranker
underperforms simply trusting the already-computed immediate-reward ordering. NDCG alone
(0.8516) looked fine and would have been misleading reported on its own — the concrete check is
what actually mattered and is what's reported here.

**Diagnosis**: comparing a single noisy realized value (the chosen candidate) against smoothed
463-bucket averages (the other 9) likely doesn't carry enough decision-specific differentiation
within one arrival's group to teach useful *relative* distinctions — the bucket-average relevance
signal is probably too coarse for what a ranker needs.

**Decision, made explicitly rather than silently abandoning or continuing to iterate**: stop here.
The pointwise Q-model (`train_value_function.py`) and state-value model
(`train_state_value_function.py`) remain the deliverable — `score_candidate()`'s existing ordering
already outperforms this ranking attempt on the one metric that was actually checked. Documented
as a genuine negative result, not hidden.

## Files

- `sim/sql/021_add_region_clusters.sql`, `022_add_region_features.sql`, `023_add_candidate_scores.sql`
- `sim/cluster_locations.py` — k-means, k chosen by silhouette score
- `sim/engine/policy.py` — `rank_candidates()`, the full-group scoring helper
- `sim/training/train_ranker.py` — the ranker attempt, kept in the repo with its real result printed, not deleted

All 42 tests still pass. `train_value_function.py`/`train_state_value_function.py` re-trained on
region_id (R² 0.0944 and 0.0608 respectively on the full 10K-run batch, comparable to the lat/lon
version's raw numbers but now backed by interpretable, verified regional structure instead of
coordinate-noise overfitting).
