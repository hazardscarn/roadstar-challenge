-- Several real gaps, found together while diagnosing the first value function's ~0 R^2 and a
-- direct question about where timing features live:
--
-- 1. FTL vs LTL was never distinguished in the revenue model -- every order was charged the same
--    flat per-mile rate. In reality an FTL shipper pays for the WHOLE truck regardless of fill
--    (no real "unused capacity" cost to them), an LTL shipper pays roughly proportional to the
--    space/weight they use at a premium per-unit rate, and a SECOND LTL pickup should add its OWN
--    revenue -- the "secondary pickup for more revenue" mechanic asked for early in this build,
--    never actually wired into the numbers.
-- 2. No lane-level context existed as a feature at all: the order's own length, and how far its
--    destination sits from the nearest hub (the same distance now driving the post-completion
--    deadhead probability in run_sim.py).
-- 3. sim.assignments had no trip_id -- no clean way to join a decision to its own full event
--    history (sim.trip_events) to compute how long the trip actually took, or to distinguish the
--    PLANNED duration estimate available at decision time (which matters for HOS and for value:
--    a longer commitment ties up the driver/truck longer) from the ACTUAL realized duration
--    (an outcome, useful for backtesting, never fed back as an input feature -- that would leak
--    the future into the state).
alter table sim.orders add column service_type text check (service_type in ('FTL','LTL'));
alter table sim.orders add column loaded_miles numeric;
alter table sim.orders add column dest_distance_to_hub_km numeric;

alter table sim.assignments add column trip_id uuid;
alter table sim.assignments add column planned_driving_hours numeric;
alter table sim.assignments add column planned_duty_hours numeric;

alter table training_transitions add column order_service_type text;
alter table training_transitions add column order_loaded_miles numeric;
alter table training_transitions add column dest_distance_to_hub_km numeric;
alter table training_transitions add column planned_driving_hours numeric;
alter table training_transitions add column planned_duty_hours numeric;
alter table training_transitions add column actual_duration_hours numeric;  -- outcome/backtest only, NOT a model input feature
