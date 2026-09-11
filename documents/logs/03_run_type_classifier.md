# Run-Type Classifier: Promoted from Notebook to Application Code

**What:** `sim/classify.py` — `classify_run_type()` and `classify_trip_pattern()`, extracted
verbatim from `analysis/data_analysis.ipynb`.

**Why extracted rather than left in the notebook**: `sim/load_ground_truth.py` (labels every
historical leg with its run type at load time) and any calibration/backtest script that needs
`run_type` must all use the exact same classification logic. Leaving it in a notebook means
copy-pasting the function (or worse, re-typing it) into every script that needs it — one canonical
module, imported everywhere, is the only way to guarantee they never drift apart.

**A real gotcha this avoids**: `analysis/driver-analysis.ipynb` defines an *earlier, simpler*
6-category function with the exact same name (`classify_run_type`), which groups empty-leg
adjacency by `(NAME, TRIP_NUMBER)` — resetting at every trip-number boundary. The canonical
8-category version in `data_analysis.ipynb` groups by `NAME` alone, sorted chronologically across
trip-number boundaries, because a truck doesn't know or care where one trip number ends and the
next begins. This distinction was found and fixed earlier in the session specifically because the
trip-number-boundary version was silently misclassifying real "drove straight into another load"
legs as "abandoned with nothing following" — a real bug, not a style preference. `sim/classify.py`
carries an explicit docstring warning off the 6-category version, and
`sim/tests/test_classify.py::test_empty_to_pickup_and_after_delivery_cross_trip_number` is a
regression test built specifically to catch a reintroduction of that bug.

**The 8 categories**: Point-to-point single load (FTL), Multi-stop consolidated freight (LTL),
Empty-pallet consolidation run, Yard shuttle (loaded), Yard shuttle (empty trailer), Empty run to
pickup (deadhead approach), Empty run after delivery (deadhead departure), Other/mid-chain empty
repositioning. Full definitions and the validated numbers behind each are in
`analysis/data_analysis.ipynb`.

**Test status**: 7/7 passing (`pytest sim/tests/test_classify.py`).

**General rule, not just for this function**: confirmed with the user that `driver-analysis.ipynb`
is an earlier notebook with some outdated/incorrect analysis carried over from before the
trip-number-boundary bug (and others) were found and fixed. **`analysis/data_analysis.ipynb` is
the sole source of truth** for any number, distribution, or piece of logic pulled from the EDA
work into application code going forward — `driver-analysis.ipynb` should be treated as
superseded draft material, not cross-checked against or reused, even where the two overlap.

## Domain constants (`sim/config.py`)

Every other module reads from here rather than re-declaring these values:
- Canadian HOS limits (13h driving / 14h on-duty / 16h elapsed window / 10h daily off-duty / 8h
  core rest / 70h-7-day / 120h-14-day cycles) — real regulatory numbers from the Project Brief,
  not assumptions.
- Trailer capacity by load type (`Dry Van`/`Reefer`/`Flatbed`) — `Flatbed` has no roster row in
  the source data (a known gap), so its capacity is synthesized here, clearly labeled.
- Geofence radius/buffer defaults.
- **Synthesized $-rate assumptions** (`ASSUMED_LINEHAUL_RATE_PER_MILE`,
  `ASSUMED_DETENTION_RATE_PER_HR_CAD`) — loudly commented as assumptions, because **no price,
  rate, revenue, or invoice field exists anywhere in the source data** (confirmed against
  `documents/data_dictionary.md` and `documents/domain_deep_dive_and_eda_plan.md`, column by
  column). These values get seeded once into `calibration.assumptions` and everything downstream
  (reward function, backtests, the inference function, the dashboard's dollar figures) reads that
  one database row rather than each hardcoding the rate a second or third time.
