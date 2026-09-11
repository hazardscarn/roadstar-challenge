"""Seeds a small, realistic live.* fleet snapshot for testing score_quote() locally -- the
theory-proof harness (documents/logs/19-20's live-sim design), NOT a real live-data feed. Mixes
idle and mid-route drivers on purpose so build_candidates() actually exercises both code paths.
"""
import random
import uuid
from datetime import datetime, timedelta, timezone

from sim.db import cursor
from sim.engine.run_sim import load_sim_data


def seed(n_idle: int = 15, n_mid_route: int = 10, seed_value: int = 1):
    rng = random.Random(seed_value)
    data = load_sim_data()
    now = datetime.now(timezone.utc)

    with cursor() as cur:
        cur.execute("select driver_id from ground_truth.drivers order by driver_id limit %s", (n_idle + n_mid_route,))
        driver_ids = [r[0] for r in cur.fetchall()]
        cur.execute("select truck_number from ground_truth.trucks order by truck_number limit %s", (n_idle + n_mid_route,))
        truck_numbers = [r[0] for r in cur.fetchall()]

    location_ids = list(data.locations.keys())
    hub_ids = list(data.hub_ids.values())

    with cursor() as cur:
        cur.execute("truncate table live.driver_status cascade")
        cur.execute("truncate table live.trips cascade")
        cur.execute("truncate table live.truck_maintenance_state cascade")

        for i, (driver_id, truck_number) in enumerate(zip(driver_ids, truck_numbers)):
            is_mid_route = i >= n_idle
            hos_remaining = rng.uniform(2.0, 13.0)

            # Truck maintenance state: realistic spread, matching sim/engine/maintenance.py's
            # initialize_fleet() distribution (documents/logs/18) -- most healthy, a few overdue.
            pct = rng.uniform(0, 1.1)
            cum_km = pct * 25000
            days_since = rng.uniform(0, 1.1) * 180
            last_service_at = now - timedelta(days=days_since)
            cur.execute("""
                insert into live.truck_maintenance_state (truck_number, cumulative_km_since_service, last_service_at, service_interval_km, service_interval_days)
                values (%s, %s, %s, %s, %s)
                on conflict (truck_number) do update set cumulative_km_since_service=excluded.cumulative_km_since_service,
                    last_service_at=excluded.last_service_at
            """, (truck_number, cum_km, last_service_at, 25000, 180))

            if not is_mid_route:
                loc_id = rng.choice(hub_ids)  # idle drivers plausibly sitting at a hub
                cur.execute("""
                    insert into live.driver_status (driver_id, updated_at, last_location_id, hos_remaining_hours,
                        duty_status, current_trip_id, truck_number, trailer_type, trailer_capacity_lbs, trailer_capacity_pallets)
                    values (%s, %s, %s, %s, 'off_duty', null, %s, 'Dry Van', 44500, 26)
                """, (driver_id, now, loc_id, hos_remaining, truck_number))
            else:
                trip_id = uuid.uuid4()
                dest_loc = rng.choice(location_ids)
                eta = now + timedelta(hours=rng.uniform(0.5, 6.0))
                projected_hos = max(0.0, hos_remaining - rng.uniform(1.0, 4.0))
                cur.execute("""
                    insert into live.driver_status (driver_id, updated_at, last_location_id, hos_remaining_hours,
                        duty_status, current_trip_id, truck_number, trailer_type, trailer_capacity_lbs, trailer_capacity_pallets)
                    values (%s, %s, null, %s, 'driving', %s, %s, 'Dry Van', 44500, 26)
                """, (driver_id, now, hos_remaining, trip_id, truck_number))
                cur.execute("""
                    insert into live.trips (trip_id, driver_id, status, last_event, eta,
                        dest_location_id, projected_hos_remaining_hours, projected_truck_pct_km_interval, projected_truck_pct_days_interval)
                    values (%s, %s, 'in_progress', 'DEPSHIP', %s, %s, %s, %s, %s)
                """, (trip_id, driver_id, eta, dest_loc, projected_hos, pct, days_since / 180))

    print(f'Seeded {n_idle} idle + {n_mid_route} mid-route drivers into live.*')


if __name__ == '__main__':
    seed()
