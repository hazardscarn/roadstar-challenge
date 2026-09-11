-- The temporal-realism pass (documents/logs/17) splits "order booked" from "dispatch decided" --
-- decision_time is when the DISPATCH_DECISION event actually fires (max(booked_at, requested
-- pickup - 24h)), not the same instant as created_at any more. pickup_window_start already
-- exists (sim/sql/005_sim.sql) and is repurposed as the REQUESTED pickup time (still a single
-- point in this model, not a real start/end window -- see sim/engine/run_sim.py's Order.requested_pickup_at).
alter table sim.orders add column decision_time timestamptz;
