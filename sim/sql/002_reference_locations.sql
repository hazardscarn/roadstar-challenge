create table reference.locations (
  location_id serial primary key,
  label text,                                   -- 'Lear Corp (Whitby)' or a synthetic facility name
  city text,
  province text default 'ON',
  tier text check (tier in ('real_customer','synthetic_facility','terminal_hub')),
  geog geography(Point, 4326) not null,
  source text check (source in ('nominatim','overpass','manual')),
  near_highway text,                             -- '401'/'403'/'400'/'407'/null, informational
  buffer_minutes integer default 10,             -- geofence dwell-confirmation buffer, tunable per site
  radius_m integer,                              -- set from config.GEOFENCE_RADIUS_M at insert time
  created_at timestamptz default now()
);
create index on reference.locations using gist (geog);
create unique index on reference.locations (label);

-- the two primary terminal hubs get their own explicit rows, not folded into synthetic_facility
insert into reference.locations (label, city, tier, geog, source, radius_m) values
  ('RoadStar Terminal — London', 'London', 'terminal_hub', ST_GeogFromText('POINT(-81.2453 42.9849)'), 'manual', 200),
  ('RoadStar Terminal — Milton', 'Milton', 'terminal_hub', ST_GeogFromText('POINT(-79.8774 43.5183)'), 'manual', 200)
on conflict (label) do nothing;
