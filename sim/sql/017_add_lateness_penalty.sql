-- Real appointment-lateness modeling, explicitly requested: coming later than a promised
-- delivery time should be penalized, worse the later it runs (a small grace buffer first), on
-- top of (not instead of) the existing HOS/maintenance risk term. The cascading "a late trip
-- delays the driver's next trip too" effect needs no extra code -- it's already an emergent
-- property of the discrete-event clock (a driver isn't available for their next assignment until
-- their current trip's driving_end, whatever that turns out to be).
--
-- Also fixes a naming problem the new column exposed: training_transitions.lateness_risk_penalty
-- was never actually about appointment lateness -- it's the algebraically-recovered
-- hos_stranding_risk_penalty + maintenance_risk_penalty combo (see extract_transitions.py).
-- Renamed to say what it actually is, now that a real lateness_penalty column exists alongside it.
alter table sim.orders add column promised_delivery_at timestamptz;
alter table sim.assignments add column lateness_penalty numeric;  -- REAL appointment lateness, realized after the fact

alter table training_transitions rename column lateness_risk_penalty to hos_maintenance_risk_penalty;
alter table training_transitions add column lateness_penalty numeric;
alter table training_transitions add column promised_delivery_at timestamptz;
