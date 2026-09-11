"""Creates real Supabase Auth accounts + public.profiles rows for the demo: 1 manager + 30 real
drivers (scaled up from the original 8 -- "8 is not the best option" -- picked by
seed_demo_fleet.py's own _build_fleet_roster(), reused here so both scripts always agree on
exactly which driver_ids are the demo fleet), per the UI-build plan's "small demo set" decision --
not all 131 drivers, since none of them have real emails/passwords anywhere in the source data.

Uses the Supabase Admin Auth REST API (SUPABASE_SERVICE_ROLE_KEY) to create users -- the only
thing in this whole build that needs the service-role key, everything else writes directly via
SUPABASE_DB_URL (sim/db.py's existing pattern, bypasses RLS as a superuser connection). Idempotent:
re-running looks up an existing user by email instead of erroring.

Credentials are written to documents/demo_credentials.md (gitignored -- never committed).
"""
import os

import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ['VITE_SUPABASE_URL']
SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
if not SERVICE_ROLE_KEY:
    raise RuntimeError('SUPABASE_SERVICE_ROLE_KEY not set in .env -- get it from Supabase dashboard > Project Settings > API.')

ADMIN_HEADERS = {
    'apikey': SERVICE_ROLE_KEY,
    'Authorization': f'Bearer {SERVICE_ROLE_KEY}',
    'Content-Type': 'application/json',
}

MANAGER_EMAIL = 'davidacad10@gmail.com'
DRIVER_PASSWORD = 'RoadStar2026!'  # shared, demo-only -- these are synthesized logins, not real driver credentials


def create_or_get_user(email: str, password: str) -> str:
    resp = requests.post(f'{SUPABASE_URL}/auth/v1/admin/users', headers=ADMIN_HEADERS, json={
        'email': email, 'password': password, 'email_confirm': True,
    })
    if resp.status_code in (200, 201):
        return resp.json()['id']
    # Already exists -- look it up instead of failing (idempotent re-run).
    list_resp = requests.get(f'{SUPABASE_URL}/auth/v1/admin/users', headers=ADMIN_HEADERS, params={'email': email})
    list_resp.raise_for_status()
    users = list_resp.json().get('users', [])
    matching = [u for u in users if u['email'] == email]
    if not matching:
        raise RuntimeError(f'Could not create OR find user {email}: {resp.status_code} {resp.text}')
    return matching[0]['id']


def upsert_profile(cur, user_id: str, role: str, driver_id: int | None):
    cur.execute(
        """insert into public.profiles (user_id, role, driver_id) values (%s, %s, %s)
           on conflict (user_id) do update set role = excluded.role, driver_id = excluded.driver_id""",
        (user_id, role, driver_id),
    )


def main():
    conn = psycopg2.connect(os.environ['SUPABASE_DB_URL'])
    cur = conn.cursor()

    from sim.live.seed_demo_fleet import _build_fleet_roster
    demo_driver_ids, _driver_trucks = _build_fleet_roster(cur)

    lines = [
        '# Demo login credentials (gitignored -- do not commit)',
        '',
        '| Role | Email | Password | driver_id |',
        '|---|---|---|---|',
    ]

    manager_password = os.environ.get('DEMO_MANAGER_PASSWORD', 'RoadStarManager2026!')
    manager_id = create_or_get_user(MANAGER_EMAIL, manager_password)
    upsert_profile(cur, manager_id, 'manager', None)
    lines.append(f'| manager | {MANAGER_EMAIL} | {manager_password} | — |')
    print(f'manager -> {MANAGER_EMAIL} ({manager_id})')

    for driver_id in demo_driver_ids:
        email = f'driver{driver_id}@roadstar.demo'
        user_id = create_or_get_user(email, DRIVER_PASSWORD)
        upsert_profile(cur, user_id, 'driver', driver_id)
        lines.append(f'| driver | {email} | {DRIVER_PASSWORD} | {driver_id} |')
        print(f'driver {driver_id} -> {email} ({user_id})')

    conn.commit()
    cur.close()
    conn.close()

    os.makedirs('documents', exist_ok=True)
    with open('documents/demo_credentials.md', 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('\nWrote documents/demo_credentials.md')


if __name__ == '__main__':
    main()
