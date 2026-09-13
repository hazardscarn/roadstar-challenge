"""Shared real-data-informed hub-share weighting for London/Milton/Barrie -- used by BOTH
sim/calibrate_driver_home_hub.py (driver home hub) and sim/calibrate_truck_profile.py (truck home
hub), so the two populations end up with consistent, non-arbitrary hub proportions instead of two
independently-guessed splits.

Real user feedback, twice over (once for trucks, and the same principle applied to drivers for
consistency): don't split 3 hubs evenly -- London and Milton have a REAL observed split (from the
53/131 drivers with enough real historical-leg history to infer an actual home base,
sim/calibrate_driver_home_hub.py's own signal), Barrie has none (it's a brand-new hub, no history
exists to derive a share from). So: take the real London:Milton ratio from that historical signal,
fold Barrie in at a fixed ASSUMED share, and scale London/Milton's real ratio down to fill the
rest -- real signal preserved where it exists, one clearly-labeled assumption where it doesn't.
"""

BARRIE_SHARE = 0.15  # SYNTHESIZED -- no real Barrie volume/headcount data exists yet (brand-new
# hub); a real user judgment call, not derived from data.

# Real user correction, superseding this file's earlier "derive London:Milton from real signal"
# approach: checked directly what that produced -- the real historical-leg signal turned out to be
# so heavily Milton-concentrated (London:Milton observed near 4:38, i.e. ~9%:91%) that the
# resulting FULL population came out ~76% Milton / ~8% London / ~17% Barrie, not the discussed
# target. London is a real, currently-operating hub (unlike Barrie) -- collapsing it to near-zero
# defeats the actual purpose of a 3-hub dispatch demo (showing London-area trucks legitimately
# working London-area freight). Fixed, discussed targets instead: Milton 55%, London 30%,
# Barrie 15% -- real signal no longer drives the SHARE, but still used elsewhere (this file's
# caller can still bias an individual driver/truck toward their real nearest hub within these
# fixed shares if it has real signal to work with; the population-level PROPORTION is what's fixed
# here, not each individual's hub).
MILTON_SHARE = 0.55
LONDON_SHARE = 0.30


def compute_hub_weights(
    historical_counts: dict[int, int], london_id: int, milton_id: int, barrie_id: int,
) -> dict[int, float]:
    """Returns {location_id: probability} for all 3 hubs, summing to 1.0 -- the fixed, discussed
    Milton 55% / London 30% / Barrie 15% split (see module docstring for why this replaced a
    real-ratio-derived split). `historical_counts` is accepted but no longer drives the share
    (kept in the signature so callers/tests that still pass real per-driver signal don't need to
    change) -- it's a no-op input now, not a silently-ignored bug."""
    del historical_counts
    return {milton_id: MILTON_SHARE, london_id: LONDON_SHARE, barrie_id: BARRIE_SHARE}


def hub_quotas(n: int, shares: dict) -> dict:
    """Integer headcount per hub (keyed however `shares` is keyed -- location_id or city name,
    caller's choice) summing EXACTLY to n. Largest-remainder rounding, not naive round() per hub
    (naive rounding can over/undershoot the total by 1-2 for an odd n) -- shared by sim/calibrate_
    driver_home_hub.py (the full 131-driver population) and sim/live/seed_demo_fleet.py (the
    small demo fleet), so both quota-fill the same discussed shares the same, exact way."""
    raw = {hub: n * share for hub, share in shares.items()}
    quotas = {hub: int(v) for hub, v in raw.items()}
    shortfall = n - sum(quotas.values())
    remainders = sorted(raw, key=lambda hub: raw[hub] - quotas[hub], reverse=True)
    for hub in remainders[:shortfall]:
        quotas[hub] += 1
    return quotas


# NOTE: an earlier version of this file also had a `nearest_hub_map()`/`hub_order_quotas()` pair
# that bucketed the day's ORDER BOOK to the nearest of the 3 dispatch hubs, floored per hub. Real
# user feedback found that still landed almost entirely on Milton/GTA and showed nothing at all
# near Peterborough/Niagara Falls -- removed in favor of sim/live/generate_dispatch_day.py's own
# region-based (30 k-means regions, not 3 points) sqrt-dampened stratification, which spans the
# whole coverage area instead of clustering around 3 fixed anchors. This file now only covers
# home-hub weighting (trucks/drivers), which the 3-hub anchor set is still correct for.
