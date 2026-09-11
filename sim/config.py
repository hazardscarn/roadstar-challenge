"""Domain constants used across the loader, calibration, simulation, reward, and dashboard.

SYNTHESIZED ASSUMPTIONS, clearly marked: no price/rate/revenue/invoice field exists anywhere in
the source data (Tlorder/Dispatch checked column-by-column -- see documents/data_dictionary.md
and documents/domain_deep_dive_and_eda_plan.md). Any dollar figure downstream of these constants
is an assumption, not something derived from real data, and must be presented to judges as such.

These values are seeded into the `calibration.assumptions` table by sim/build_calibration.py --
that table, not this file, is the runtime source of truth. Everything downstream (reward.py, the
backtest scripts, the Vercel inference function, the dashboard's $-figure components) reads the
DB row, not this Python constant, so the rate is defined in exactly one place at runtime.
"""

# SYNTHESIZED, but grounded in real 2026 market-rate research now, not a single flat guess (see
# documents/logs/16_reward_realism_pass.md). A flat per-mile rate was checked against this
# network's OWN simulated trip-length distribution (avg loaded trip ~31 miles -- squarely
# short-haul) and found to systematically under-price it: real trucking rates are NOT flat per
# mile -- shorter hauls command a well-documented premium because fixed per-stop costs (dispatch,
# check-in/out, fueling, paperwork) are amortized over fewer miles, the same reason a courier
# charges more per km for a 5km drop than a 500km one. Sourced ranges (2026 US/Ontario market,
# general dry-van rate-per-mile-by-length-of-haul reporting -- see WebSearch results cited in the
# log): short-haul FTL (<100mi) $3.00-$6.00/mi; sub-200mi loads carry an 18-25% premium over the
# long-haul baseline; long-haul market average ~$2.20-2.60/mi. This is the FTL rate -- an FTL
# shipper pays it per mile for the WHOLE truck, flat regardless of fill (see reward.py); LTL
# revenue is derived from the SAME per-mile figure x ASSUMED_LTL_RATE_MULTIPLIER x fill fraction.
#
# Tiered by loaded-trip distance, not one number -- the old flat $3.25/mi survives as the
# "medium" tier (100-150mi), not stretched to cover the whole curve:
LINEHAUL_RATE_TIERS_CAD_PER_MILE = [
    (50.0, 5.00),          # 0-50mi   -- top of the sourced short-haul FTL band
    (100.0, 4.00),         # 50-100mi -- still short-haul, past the steepest amortization drop-off
    (150.0, 3.25),         # 100-150mi -- the old flat rate, kept as this tier's anchor
    (float('inf'), 2.75),  # 150mi+   -- approaching the long-haul market average
]


def linehaul_rate_per_mile(loaded_miles: float) -> float:
    """CAD/mile for a trip of THIS length -- see LINEHAUL_RATE_TIERS_CAD_PER_MILE's sourcing.
    Replaces a flat ASSUMED_LINEHAUL_RATE_PER_MILE that ignored the real, well-documented
    inverse relationship between trip length and per-mile rate.
    """
    for threshold, rate in LINEHAUL_RATE_TIERS_CAD_PER_MILE:
        if loaded_miles <= threshold:
            return rate
    return LINEHAUL_RATE_TIERS_CAD_PER_MILE[-1][1]

# SYNTHESIZED ASSUMPTION -- not derived from data (no per-shipment rate data exists at all, FTL or
# LTL). LTL freight commands a real-world premium per unit of capacity over FTL (the shipper is
# paying for shared handling/consolidation, not a dedicated truck) -- an LTL shipment's revenue is
# ASSUMED_LINEHAUL_RATE_PER_MILE x this multiplier x the fraction of the truck it actually fills,
# so filling MORE of the truck (via a secondary pickup) earns more, and a single half-empty LTL
# shipment earns less than a full FTL truck would over the same distance -- see reward.py.
ASSUMED_LTL_RATE_MULTIPLIER = 1.4

# REAL, from ground_truth.historical_legs: 334 of 3,011 loaded legs (334 Multi-stop/LTL + 2,677
# Point-to-point/FTL) were classified LTL -- an 11.1% share. Used to draw each simulated order's
# service_type; unlike the earlier flat TRANSITION_PRIORS['p_secondary_pickup']=0.07 guess (now
# retired), this is checked directly against the data, not assumed.
P_LTL_ORDER = 334 / (334 + 2677)

# SYNTHESIZED ASSUMPTION -- not derived from data. Mid-point of the industry-typical detention
# rate range noted in documents/domain_deep_dive_and_eda_plan.md ($50-100/hr CAD).
ASSUMED_DETENTION_RATE_PER_HR_CAD = 75.0

# SYNTHESIZED ASSUMPTION -- not derived from data (no real "how long does a human dispatcher take
# per assignment" data exists anywhere in this project). A manual dispatch decision realistically
# involves checking each candidate driver's live HOS clock, current position, truck health/
# maintenance status, and running ETA math by hand or spreadsheet -- a few minutes per candidate
# considered, times several real candidates per order. Used ONLY for the Simulation Showcase's own
# "time this automated decision-making would take a human dispatcher" framing -- a labeled,
# order-of-magnitude estimate for the pitch, not a claim about any real carrier's actual dispatch
# process.
ASSUMED_MANUAL_DISPATCH_MINUTES_PER_DECISION = 8.0

# SYNTHESIZED, but researched against real mid-2026 sourcing (WebSearch, this build pass):
# FreightWaves Checkpoint's own worked fuel-surcharge examples land FSC at 28.5-33.5% of linehaul
# for US FTL dry van at 2026 diesel pricing; Canadian carrier-published FSC tables (Maritime-
# Ontario, weekly) run notably higher on their own base-rate schedule, but that's because THEIR
# published "FSC%" is computed against a much smaller contracted base rate, not against a full
# market linehaul figure -- applying that percentage directly to a market-rate linehaul number
# would double-count. Using the FreightWaves worked-example range's midpoint (~30%), which is
# computed the same way this invoice does (FSC = rate% x linehaul): a defensible, cited figure,
# not the carrier-specific headline percentage. Customer-facing only (invoicing/quote display) --
# NOT fed into the trained reward model, which was calibrated and trained against linehaul alone;
# changing the model's own revenue signal now would silently redefine what it was trained to
# optimize. Sources: FreightWaves Checkpoint "Fuel Surcharges in Trucking" (worked example: $1,000
# linehaul x 28.5% FSC = $285); Dashdoc fuel-surcharge guide (worked example: 33.5% at 2026 EIA
# diesel pricing).
FUEL_SURCHARGE_RATE = 0.30

# SYNTHESIZED, sourced from 2026 US/Canada FTL market-rate spread research (WebSearch, this build
# pass -- UberFreight/FreightWaves 2026 rate surveys): dry van ~$2.05-2.80/mi spot in mid-2026;
# reefer ~$3.39/mi all-in (typically 20-40% over dry van -- temperature control, specialized
# equipment, tighter capacity); flatbed ~$3.72/mi all-in (typically 30-50% over dry van -- open-
# deck securement/tarping labor, smaller equipment pool). Applied as a multiplier on top of this
# network's own distance-tiered dry-van-anchored rate (LINEHAUL_RATE_TIERS_CAD_PER_MILE) at the
# customer-facing quote/invoice layer ONLY -- like FUEL_SURCHARGE_RATE above, this is NOT fed into
# the trained reward model's order_revenue, which was calibrated on the dry-van rate alone across
# training and the validated backtest.
TRUCK_TYPE_RATE_MULTIPLIER = {
    'Dry Van': 1.00,
    'Reefer': 1.30,
    'Flatbed': 1.40,
}


def quote_price(loaded_miles: float, load_type: str, service_type: str = 'FTL', fill_ratio: float = 1.0) -> dict:
    """The customer-facing quoted price for a trip of this length/equipment/fill -- the SAME
    distance-tiered base rate and LTL/fill treatment reward.py's order_revenue uses (so the
    quote shown to a customer and the number the dispatch model is actually optimizing against
    never silently diverge on the base economics), with the real-world truck-type premium and
    fuel surcharge layered on top for what actually appears on a quote/invoice. One function, both
    callers (score_quote.py's live quote summary, dashboard/server/main.py's invoice generation) --
    the project's own "one function, two callers" convention.
    """
    rate_per_mile = linehaul_rate_per_mile(loaded_miles)
    truck_multiplier = TRUCK_TYPE_RATE_MULTIPLIER.get(load_type, 1.00)
    base_rate_per_mile = rate_per_mile * truck_multiplier
    if service_type == 'LTL':
        linehaul_amount = loaded_miles * base_rate_per_mile * ASSUMED_LTL_RATE_MULTIPLIER * fill_ratio
    else:
        linehaul_amount = loaded_miles * base_rate_per_mile
    fuel_surcharge_amount = linehaul_amount * FUEL_SURCHARGE_RATE
    return {
        'rate_per_mile': round(base_rate_per_mile, 2),
        'linehaul_amount': round(linehaul_amount, 2),
        'fuel_surcharge_amount': round(fuel_surcharge_amount, 2),
        'estimated_total_charge': round(linehaul_amount + fuel_surcharge_amount, 2),
    }


def leg_detention(arrival_at, departure_at, free_hours: float = 2.0,
                   rate_per_hr: float = ASSUMED_DETENTION_RATE_PER_HR_CAD) -> dict:
    """Billable detention for ONE leg (pickup OR delivery) of a trip -- billed against its own
    free-hours allowance, not the whole trip's dwell lumped together. This is the correct
    per-leg treatment; live.detention_billing's cron job (sim/sql/010) groups by trip_id ONLY,
    which conflates pickup-leg and delivery-leg dwell into a single figure (a real, pre-existing
    gap, not propagated here -- see the Simulation Showcase's use of this function,
    dashboard/server/main.py). `arrival_at`/`departure_at` are `None` when a leg's milestone
    wasn't reached (e.g. an order still in progress) -- returns zero detention rather than
    raising, so a caller can call this unconditionally.
    """
    if arrival_at is None or departure_at is None or departure_at <= arrival_at:
        return {'billable_hours': 0.0, 'amount': 0.0}
    dwell_hours = (departure_at - arrival_at).total_seconds() / 3600
    billable_hours = max(0.0, dwell_hours - free_hours)
    return {'billable_hours': round(billable_hours, 2), 'amount': round(billable_hours * rate_per_hr, 2)}


# SYNTHESIZED ASSUMPTIONS -- appointment-lateness modeling. No real quoted-delivery-time data
# exists anywhere in the source (checked already for a general "promise" concept in the data
# dictionary work); this is a business-logic addition, not something derived from data.
# - PROMISE_BUFFER_HOURS: how much slack is built into a quoted delivery time beyond the typical
#   drive+dwell estimate -- covers typical time-to-dispatch and gives the promise real headroom,
#   the way a real quote isn't just "best-case transit time with zero margin."
# - LATE_GRACE_MINUTES: a small buffer before lateness starts costing anything at all -- a few
#   minutes late isn't a real service failure.
# - LATE_PENALTY_PER_HOUR_CAD: the base rate; applied to (hours_late ** LATE_PENALTY_EXPONENT), a
#   CONVEX (not linear) shape on purpose -- satisfaction degrades faster than proportionally the
#   longer a customer waits past what they were promised, not just accumulates steadily.
ASSUMED_PROMISE_BUFFER_HOURS = 5.0
LATE_GRACE_MINUTES = 15
ASSUMED_LATE_PENALTY_PER_HOUR_CAD = 40.0
LATE_PENALTY_EXPONENT = 1.5

# SYNTHESIZED ASSUMPTION -- not derived from data. What it costs to RUN the truck per mile
# (fuel, wear, driver pay-per-mile), distinct from the linehaul RATE above (what the shipper
# pays). This is the basis for deadhead_cost -- an empty mile costs this, earns nothing.
ASSUMED_OPERATING_COST_PER_MILE = 1.75  # CAD/mile

# SYNTHESIZED, but now sourced against real 2025/2026 commercial-truck-breakdown cost reporting
# (documents/logs/16_reward_realism_pass.md), not a guessed pair of numbers. Real figures found:
# direct tow + repair runs $3,000-$9,000 USD/incident; downtime (the truck out of service, still
# accruing ownership/driver/fuel cost) adds ~$450-760 USD/day over an average 2.3-2.4 day outage,
# ~$1,000-1,800 USD more. All-in, in CAD (~1.37x USD at time of research): roughly $5,500-$14,800
# CAD per real breakdown -- this project's original flat $8,000 realized-event guess turns out to
# sit comfortably inside that real range, so it's kept close, not thrown out.
#
# What WAS wrong: two DIFFERENT numbers were used for the same event -- a $2,000 "expected cost"
# at decision time (scoring which truck to pick) vs. an $8,000 "realized" cost once it actually
# happened. That 4x gap meant the policy's own decision-time scoring structurally UNDER-weighted
# breakdown risk relative to the real cost the batch-level reporting then charged it for after the
# fact -- exactly why aggregate reward looked far worse than what any single dispatch decision
# appeared to optimize for. One realistic, sourced figure now backs BOTH uses (probability x this,
# at decision time; the full amount, once realized) -- see maintenance.py.
ASSUMED_BREAKDOWN_COST_CAD = 7500.0

# SOURCED (documents/logs/17_temporal_realism_pass.md): real freight brokerage/carrier practice
# tenders a load to a driver 24-72h before pickup (below 24h risks failed tenders); dispatchers
# separately finalize each day's route plan in the MORNING for that day's pickups, never at the
# pickup moment itself. 24h is the conservative, lower end of that real range -- the point past
# which a dispatch decision genuinely has to be made, not "the instant the order was booked."
# Orders booked with MORE lead time than this just sit on the books until this cutoff arrives;
# orders booked with LESS (a real ~25% of them, per calibration.order_lead_time_hours) get
# decided immediately, same as before this was added.
DISPATCH_DECISION_CUTOFF_HOURS = 24.0

# Ξ΅-greedy exploration schedule -- standard RL practice (Ξ΅-greedy/UCB) decays exploration as
# confidence in the learned value builds, instead of a flat rate for an entire run. Applied along
# SIMULATED elapsed time (0 -> 1 across the run), not per-decision-count, since one run is now a
# single continuous multi-month history, not repeated short episodes. See run_sim.py's
# epsilon_schedule().
EPSILON_START = 0.6
EPSILON_END = 0.05

# SYNTHESIZED ASSUMPTION -- not derived from data. The $ value of a pound of UNUSED capacity on
# an assignment (reward.py's opportunity_cost_penalty = (1 - load_fill_ratio) x this x
# capacity_lbs) -- a rough proxy for the revenue a fuller load on the same trip could have
# earned. No real per-lb revenue figure exists to calibrate this against either.
ASSUMED_CAPACITY_VALUE_RATE_PER_LB = 0.01  # CAD/lb

# SYNTHESIZED, matches live.truck_maintenance_state's defaults (sim/sql/006_live.sql) -- kept in
# one place so the simulator's breakdown-risk model and the live dashboard's warning threshold
# agree with each other.
MAINTENANCE_SERVICE_INTERVAL_KM = 25000

# SYNTHESIZED -- real fleets schedule service by calendar time too ("every N km OR every M
# months, whichever comes first"), not distance alone; a truck driven lightly but sitting since a
# long-ago service is a real maintenance-risk case a distance-only model would completely miss.
# ~6 months, a common real-world interval for this class of vehicle; no real service-log data
# exists to calibrate against, same caveat as MAINTENANCE_SERVICE_INTERVAL_KM.
MAINTENANCE_SERVICE_INTERVAL_DAYS = 180

# SOURCED (documents/logs/19): real truckload dry-van fleets average 14,991 miles between roadside
# breakdowns (TMC/FleetNet America benchmarking), vs. a fleet-wide cross-segment average of 33,637
# miles -- used here as the dry-van-specific figure since that's this fleet's own real load type
# mix. This fleet's real average trip length (sim.assignments, avg loaded+deadhead miles) is
# ~53.5mi, so a truck AT its due point should break down on roughly 1 trip in 280
# (53.5/14,991 ~= 0.36%). The ORIGINAL curve (0.15 at-due / 0.30 capped) was ~40-80x too high --
# not a deliberate design choice, an unsourced guess that went unchecked until now. That inflated
# rate made V(s) learn an extreme, score-dominating cliff at the overdue threshold (see
# sim/engine/value_function.py's V_SHRINKAGE_FACTOR fix docstring for the concrete regression it
# caused) -- fixing the root rate here is the more correct fix; the shrinkage/clip stays too, as a
# defensive backstop against any future retrain producing another sharp cliff.
BREAKDOWN_RISK_AT_DUE = 0.0036   # ~53.5mi avg trip / 14,991mi real dry-van MBB, at exactly 100% overdue
BREAKDOWN_RISK_MAX = 0.01        # ~3x the at-due rate for a truck driven well past due -- a real
                                  # felt cost for neglect, not a 30%-per-trip implosion

# SOURCED alongside the above: real fleets schedule PROACTIVE service before a truck ever reaches
# the hard "due" line, not only reactively after a breakdown -- without this, a truck in a long
# simulated year has no way back to a healthy state at all (TruckMaintenanceState.serviced()
# existed but was never actually called anywhere in the sim loop -- a real gap, not a design
# choice, found directly from this session's maintenance-rate investigation). Modeled as a real
# probabilistic event per trip once a truck crosses the 85%-of-interval "due soon" line (the same
# threshold live.truck_maintenance_state's dashboard warning already uses), rather than every
# truck servicing at exactly 100% -- matches how real fleets don't perfectly time every service.
PROACTIVE_MAINTENANCE_THRESHOLD_PCT = 0.85
PROACTIVE_MAINTENANCE_PROB_PER_TRIP = 0.05  # SYNTHESIZED -- no real scheduling-behavior data exists
MAINTENANCE_DOWNTIME_DAYS = 2.0             # SYNTHESIZED but a plausible real shop turnaround, per the user's own estimate

# Real, from the data dictionary: Trailers.CAPACITY_LBS only has two distinct values (Dry Van /
# Reefer); Flatbed has no roster row at all (Known Issue) so its capacity is synthesized here too.
CAPACITY_BY_LOAD_TYPE = {'Dry Van': 44500, 'Reefer': 43500, 'Flatbed': 44500}

# Real, from data_analysis.ipynb section 1c: max pallets observed across the historical data.
CAPACITY_PALLETS_BY_LOAD_TYPE = {'Dry Van': 26, 'Reefer': 26, 'Flatbed': 26}

# Home-base-return retarget (documents/logs/23_home_base_return_gap_found.md,
# NEW_SESSION_TRAINING_RETARGET_PROMPT.md, documents/feature_reference_and_inference_guide.md) --
# SYNTHESIZED, not derived from data (no real "how much slack does a real dispatcher build in
# before treating a driver as at-risk of a cycle-end stranding" figure exists to calibrate
# against). Widens the drive-time-needed-to-get-home estimate before comparing it to remaining
# cycle hours, the same role ASSUMED_PROMISE_BUFFER_HOURS plays for delivery promises -- a few
# hours of real slack (traffic, a dock delay, a later-than-planned departure) rather than a
# knife-edge comparison. Propose-then-validate, same standard as documents/logs/17/18: this is a
# first proposal, checked empirically via the paired significance test this same session runs,
# not assumed correct on the first try.
HOS_URGENCY_SAFETY_BUFFER_HOURS = 4.0

# SYNTHESIZED -- the real cost of a driver actually ending a 7-day/14-day HOS cycle stranded far
# from any hub: a forced multi-day reset with the truck sitting wherever the cycle bound, lost
# revenue days, and real logistics to get the driver (and possibly a relief driver) home. No real
# incident-cost data exists for this specific scenario (distinct from the sourced
# ASSUMED_BREAKDOWN_COST_CAD, which is a mechanical failure, not a legal/scheduling one) -- this is
# a first, reasoned proposal (same order of magnitude as a real breakdown's downtime cost, since
# both strand a truck away from revenue service for days), meant to be checked and iterated on via
# the paired significance test, not treated as final on the first pass.
ASSUMED_CYCLE_STRANDING_PENALTY_CAD_MAX = 2000.0

# Real physical constant, not an assumption.
KM_PER_MILE = 1.60934

# REAL, measured directly from calibration.lane_routes (real OSRM-routed distance/duration over
# every cached real lane): avg(distance_m/1609.34 / (duration_s/3600)) = 44.08 mph. Used ONLY for
# the live-inference chain-walk's projection of a not-yet-started 'scheduled' trip's deadhead
# travel time (sim/live/score_quote.py) -- the trip's own real routed dh_hours already drove its
# pricing/scoring at assignment time; this is a light estimate for projecting FUTURE state before
# telemetry has ever run, not a re-pricing. Documents/feature_reference_and_inference_guide.md
# Section 3's own open item ("pull a real network-derived average, not a guessed constant").
AVG_NETWORK_SPEED_MPH = 44.08

# Geofencing (Section 1 of research/roadstar_platform_plan.md) -- tunable, not load-bearing precision.
GEOFENCE_RADIUS_M = {'terminal_hub': 200, 'default': 120}
GEOFENCE_DWELL_BUFFER_MINUTES = 10

# Canadian HOS limits (Project Brief Section 4 -- these ARE real regulatory constants, not assumptions).
HOS_MAX_DRIVING_HOURS = 13
HOS_MAX_ON_DUTY_HOURS = 14
HOS_MAX_ELAPSED_WINDOW_HOURS = 16
HOS_MIN_DAILY_OFF_DUTY_HOURS = 10
HOS_MIN_CORE_REST_HOURS = 8
HOS_CYCLE_1_MAX_HOURS = 70   # 7-day
HOS_CYCLE_2_MAX_HOURS = 120  # 14-day

# Detention: 2h free window is a contractual assumption per the Project Brief, not from the data.
DETENTION_FREE_HOURS = 2

# Southern Ontario coverage region (Project Brief Section 2). A genuine rectangular bounding
# box, not an approximation -- the min/max envelope of precise Nominatim-geocoded coordinates
# for all 6 named places (4 boundary cities + 2 terminal hubs), not one city per cardinal edge:
# using Niagara Falls' latitude as a strict south edge would have put London itself (lat 42.98,
# south of Niagara Falls' 43.11) outside its own box -- the boundary cities aren't at pure
# compass extremes relative to each other, so the box has to be the envelope that contains all
# of them, not four independently-chosen edges. Verified: London, Barrie, Peterborough,
# Pickering, Niagara Falls, and Milton all fall inside. An earlier, looser hand-estimated box let
# real but out-of-region cities (Sudbury, Timmins, Cochrane) leak into reference.locations --
# see documents/logs/04_location_sourcing.md for that fix.
# A ~0.05-degree pad (~5km) is added on every edge: the box above uses each city's exact
# geocoded center point as an edge, so a real address slightly south/west of e.g. London's own
# center (a razor-thin edge) would otherwise be wrongly excluded from London itself.
_PAD = 0.05
COVERAGE_BBOX = {
    'min_lat': 42.9837 - _PAD, 'min_lon': -81.2496 - _PAD,
    'max_lat': 44.3893 + _PAD, 'max_lon': -78.3199 + _PAD,
}
TERMINAL_HUBS = {
    'London': (42.9837, -81.2496),
    'Milton': (43.5137, -79.8828),
}

# Self-hosted OSRM server (ops/osrm_setup.sh), running persistently as the `roadstar-osrm`
# Docker container -- see documents/logs/02_infrastructure_setup.md.
OSRM_BASE_URL = 'http://localhost:5000'
