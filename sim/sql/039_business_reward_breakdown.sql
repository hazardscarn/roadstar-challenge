-- Real user feedback: the Simulation Showcase's "net reward" KPI bundled real dollars
-- (order_revenue, deadhead cost, realized lateness) together with forward-looking ML risk
-- penalties (HOS-stranding-risk, maintenance-risk, opportunity-cost -- terms that exist to help
-- the model RANK candidates, not dollars anyone actually spent) and, for a completed trip, the
-- realized breakdown-repair cost -- something dispatch decisions don't meaningfully control in
-- this sim (truck health/maintenance is out of scope here). The result was a confusing,
-- occasionally negative number that didn't map to "did dispatch do a good job." This splits the
-- REAL, business-explainable components out on their own: revenue earned, deadhead cost paid
-- (pre-pickup + post-delivery), and realized lateness penalty -- net_margin = revenue - deadhead
-- - lateness, with no risk-penalty or breakdown-cost terms mixed in. reward_total stays as the
-- model's own internal scoring signal, just no longer the headline number shown to a business
-- audience.

alter table simulation.trip_log add column if not exists order_revenue numeric;
alter table simulation.trip_log add column if not exists deadhead_cost numeric;              -- pre-pickup, $
alter table simulation.trip_log add column if not exists post_delivery_deadhead_cost numeric; -- real cost of running back empty, $
alter table simulation.trip_log add column if not exists lateness_penalty_amount numeric;     -- realized, $
alter table simulation.trip_log add column if not exists net_margin numeric generated always as
  (coalesce(order_revenue, 0) - coalesce(deadhead_cost, 0) - coalesce(post_delivery_deadhead_cost, 0) - coalesce(lateness_penalty_amount, 0)) stored;

alter table simulation.runs add column if not exists total_deadhead_cost numeric;
alter table simulation.runs add column if not exists total_lateness_penalty numeric;
alter table simulation.runs add column if not exists net_margin numeric;
alter table simulation.runs add column if not exists baseline_net_margin numeric;
