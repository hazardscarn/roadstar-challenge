-- Live inference for the home-base-return retarget (documents/logs/23-24, NEW_SESSION_TRAINING_
-- RETARGET_PROMPT.md, documents/feature_reference_and_inference_guide.md Section 7's open items,
-- verified against the real live schema rather than assumed). Two real gaps closed:
--
-- 1. live.driver_status only ever stored ONE blended HOS number (min across all 5 real clocks).
--    home_progress_bonus/cycle_end_stranding_penalty specifically need the CYCLE clocks (70h/7d,
--    120h/14d) separated from the daily ones (13h/14h/16h) -- you cannot recover 2 numbers from
--    their min. Real, honestly-scoped simplification (see sim/live/telemetry_simulator.py's own
--    updated comment): all 4 clocks are decremented by real elapsed on-duty time same as the
--    existing blended figure always has been, but this pass does NOT add qualifying-reset
--    detection (a driver's daily clocks resetting after 10h+ off-duty) -- flagged, not hidden;
--    the existing blended hos_remaining_hours never modeled resets either, so this doesn't
--    regress anything, and log 24's own measured finding (this fleet's real demand never gets
--    close to a cycle-tight driver even over 30-day runs) means reset timing rarely matters here.
--
-- 2. live.trips modeled only ONE live trip per driver in practice (driver_status.current_trip_id,
--    a single pointer) -- a real dispatcher books trips days in advance, so a driver can have
--    several already-assigned FUTURE trips queued before any of them starts. Verified directly:
--    no unique constraint blocks multiple rows per driver today, but /api/assign unconditionally
--    overwrote current_trip_id on every new booking, silently orphaning an earlier one from the
--    single column every scoring/read path joined through -- a real bug, not just a missing
--    feature. Fixed with a real status value ('scheduled') for a booked-but-not-yet-started trip
--    distinct from 'assigned' (telemetry-simulator-ready-to-start-now); telemetry promotes the
--    next scheduled trip to 'assigned' when the current one completes. planned_driving_hours/
--    planned_duty_hours/planned_completion_at let a not-yet-started trip be "walked through" for
--    projection purposes before telemetry has ever computed its real landing state.

alter table live.driver_status add column if not exists hos_driving_hours_remaining numeric;
alter table live.driver_status add column if not exists hos_duty_hours_remaining numeric;
alter table live.driver_status add column if not exists hos_cycle1_hours_remaining numeric;
alter table live.driver_status add column if not exists hos_cycle2_hours_remaining numeric;

alter table live.trips add column if not exists planned_driving_hours numeric;
alter table live.trips add column if not exists planned_duty_hours numeric;
alter table live.trips add column if not exists planned_completion_at timestamptz;

-- No CHECK constraint exists on live.trips.status today (confirmed directly) -- 'scheduled' is a
-- new free-text value, nothing to alter.
