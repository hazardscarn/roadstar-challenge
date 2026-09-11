-- PRIMARY: pg_cron job. Test `select cron.schedule(...)` succeeds on this project's tier before
-- relying on it -- if pg_cron isn't available, uncomment the FALLBACK view below instead and
-- skip this block. Rate is read from calibration.assumptions, not hardcoded here.
select cron.schedule(
  'compute-detention',
  '*/5 * * * *',
  $$
    insert into live.detention_billing (trip_id, arrival_at, departure_at, amount)
    select trip_id,
           min(occurred_at) filter (where event_type = 'arrival'),
           max(occurred_at) filter (where event_type = 'departure'),
           greatest(0, extract(epoch from (
             max(occurred_at) filter (where event_type = 'departure') -
             min(occurred_at) filter (where event_type = 'arrival')
           )) / 3600.0 - (select value from calibration.assumptions where key = 'detention_free_hours'))
           * (select value from calibration.assumptions where key = 'detention_rate_per_hr_cad')
    from live.geofence_events
    group by trip_id
    on conflict (trip_id) do update set amount = excluded.amount;
  $$
);

-- FALLBACK (uncomment if pg_cron is unavailable on this project tier): compute on read instead
-- of on a schedule -- same output shape, no cron dependency, just a live-computed view instead
-- of a materialized table refreshed every 5 minutes.
--
-- drop table if exists live.detention_billing;
-- create view live.detention_billing as
--   select trip_id,
--          min(occurred_at) filter (where event_type = 'arrival') as arrival_at,
--          max(occurred_at) filter (where event_type = 'departure') as departure_at,
--          (select value from calibration.assumptions where key = 'detention_free_hours') as free_hours,
--          greatest(0, extract(epoch from (
--            max(occurred_at) filter (where event_type = 'departure') -
--            min(occurred_at) filter (where event_type = 'arrival')
--          )) / 3600.0 - (select value from calibration.assumptions where key = 'detention_free_hours')) as billable_hours,
--          greatest(0, extract(epoch from (
--            max(occurred_at) filter (where event_type = 'departure') -
--            min(occurred_at) filter (where event_type = 'arrival')
--          )) / 3600.0 - (select value from calibration.assumptions where key = 'detention_free_hours'))
--          * (select value from calibration.assumptions where key = 'detention_rate_per_hr_cad') as amount
--   from live.geofence_events
--   group by trip_id;
