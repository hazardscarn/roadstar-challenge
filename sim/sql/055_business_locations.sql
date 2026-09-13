-- Real user ask: Dispatch Board orders showed a bare city name for pickup/dropoff ("MISSISSAUGA,
-- ON"), and their origin/destination was drawn from calibration.lane_frequency's real historical
-- lanes -- overwhelmingly GTA/Milton (that's where the real trucking company actually ran), so
-- London- and Barrie-hub trucks kept getting dispatched into Milton/Mississauga freight instead of
-- their own area. Checked directly: even reference.locations' existing 2,000-row synthetic_facility
-- pool (real OpenStreetMap warehouse/industrial buildings spanning the WHOLE coverage bbox) is
-- itself 91% nearest-Milton/2.5% nearest-London/7% nearest-Barrie -- OSM tagging density is a real-
-- world artifact of the GTA being far more densely mapped than London/Barrie/Niagara/Peterborough,
-- so that pool can't fix this either. This adds a curated set of ACTUAL, named, geocoded real
-- businesses (sim/geocode_business_locations.py -- factories/DCs found via web search, chain
-- retail locations confirmed by successful geocoding) explicitly spread across all 3 real hub
-- catchments, so order generation can stratify by hub and draw REAL, named pickup/dropoff points
-- local to each hub's own area.
alter table reference.locations drop constraint locations_tier_check;
alter table reference.locations add constraint locations_tier_check
  check (tier in ('real_customer', 'synthetic_facility', 'terminal_hub', 'real_business'));

-- Which of the 3 real dispatch hubs (dispatch.days/reference.locations' own terminal_hub rows)
-- each curated business is meant to represent local freight for -- an explicit tag, not a nearest-
-- anchor haversine computation, because the intended grouping doesn't always match raw distance
-- (Kitchener/Cambridge/Guelph are geographically a bit closer to Milton than to London, but are
-- explicitly "London hub" territory per the real user ask: "London area, KWC, Guelph etc"; Niagara
-- folds into the Milton catchment, Peterborough into the Barrie catchment -- there are only 3 real
-- dispatch hubs, so every edge-of-coverage city needs to land in one of them, not a 4th/5th bucket).
create table reference.business_hub_catchment (
  location_id integer primary key references reference.locations(location_id) on delete cascade,
  hub_catchment text not null check (hub_catchment in ('Milton', 'London', 'Barrie'))
);
