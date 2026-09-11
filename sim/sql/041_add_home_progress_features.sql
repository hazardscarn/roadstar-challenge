-- Home-base-return retarget (documents/logs/23_home_base_return_gap_found.md,
-- NEW_SESSION_TRAINING_RETARGET_PROMPT.md, documents/feature_reference_and_inference_guide.md):
-- nothing before this migration priced a driver ending up stranded far from base with too little
-- HOS cycle time left to get back, nor credited a chain of real trips that works a driver back
-- toward base instead of one full empty return leg. Two things needed, both applied here:
--
-- 1. HOS sub-clock breakdown (driving/duty/cycle1/cycle2, current + next state) -- ALREADY
--    computed in memory (sim/engine/run_sim.py's CompletedTrip, since sim/sql/013-014) for the
--    Simulation Showcase's live display, but never persisted for training. Needed to distinguish
--    "recoverable tomorrow" (a driver near a DAILY limit, which resets via a 10h rest) from
--    "genuinely cycle-capped" (near the 70h/7-day or 120h/14-day limit, which only resets via a
--    long forced reset) -- the blended driver_hos_remaining (already stored) can't tell these
--    apart, it's just the min across all 5.
-- 2. Real position relative to the driver's OWN home terminal (distance_to_home_miles, current +
--    landing) and the two new reward terms this drives: home_progress_bonus (potential-based
--    shaping -- Ng/Harada/Russell 1999 -- credits a real trip proportional to how much it closes
--    the gap back toward home) and cycle_end_stranding_penalty (a real backstop for landing, after
--    a trip, with a tight cycle margin AND still far from home). Distinct from the EXISTING
--    sim.orders.dest_distance_to_hub_km (nearest-ANY-hub, driver-agnostic, already used for the
--    post-completion-deadhead probability) -- that field answers a different question and stays
--    untouched.

alter table sim.assignments add column driver_hos_driving_remaining numeric;
alter table sim.assignments add column driver_hos_duty_remaining numeric;
alter table sim.assignments add column driver_hos_cycle1_remaining numeric;
alter table sim.assignments add column driver_hos_cycle2_remaining numeric;
alter table sim.assignments add column next_hos_driving_remaining numeric;
alter table sim.assignments add column next_hos_duty_remaining numeric;
alter table sim.assignments add column next_hos_cycle1_remaining numeric;
alter table sim.assignments add column next_hos_cycle2_remaining numeric;

alter table sim.assignments add column distance_to_home_miles numeric;
alter table sim.assignments add column distance_to_home_miles_landing numeric;  -- doubles as this
    -- transition's NEXT-state distance-to-home (the position after this trip) -- no separate
    -- next_distance_to_home_miles column, same reasoning next_location_id already establishes.
alter table sim.assignments add column home_progress_bonus numeric;
alter table sim.assignments add column cycle_end_stranding_penalty numeric;

-- Mirror onto training_transitions -- the flattened table the models actually train on.
alter table training_transitions add column driver_hos_driving_remaining numeric;
alter table training_transitions add column driver_hos_duty_remaining numeric;
alter table training_transitions add column driver_hos_cycle1_remaining numeric;
alter table training_transitions add column driver_hos_cycle2_remaining numeric;
alter table training_transitions add column next_hos_driving_remaining numeric;
alter table training_transitions add column next_hos_duty_remaining numeric;
alter table training_transitions add column next_hos_cycle1_remaining numeric;
alter table training_transitions add column next_hos_cycle2_remaining numeric;
alter table training_transitions add column distance_to_home_miles numeric;
alter table training_transitions add column distance_to_home_miles_landing numeric;
alter table training_transitions add column home_progress_bonus numeric;
alter table training_transitions add column cycle_end_stranding_penalty numeric;
