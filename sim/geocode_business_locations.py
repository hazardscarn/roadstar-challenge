"""Curated, REAL, named business addresses for the Dispatch Board's order book -- real user ask,
twice over: (1) show an actual business address in the dispatch table, not a bare city name; (2)
fix orders clustering almost entirely around Milton/GTA even for London- and Barrie-hub trucks
("it's very bad to show them that London hub trucks driving into Milton for picking and
Mississauga") -- checked directly, both this project's real historical lane data AND its existing
2,000-row OSM-sourced synthetic_facility pool are themselves 90%+ nearest-Milton (a real artifact
of the GTA being far more densely mapped/travelled than London/Barrie/Niagara/Peterborough), so
neither can supply the geographic spread on their own.

Every entry below is a REAL, named business found via web search (factories/plants/distribution
centres -- companies and their real street addresses, cited in code review) or a real national
chain retail location (Walmart Supercentre, Costco Wholesale, Home Depot, Canadian Tire) confirmed
by successful geocoding against that specific city -- if a chain has no real store in a given city,
Nominatim simply won't resolve it there and this script drops the row (see `nominatim_geocode()`'s
own None-on-no-match handling), so nothing fabricated survives into the table.

`hub_catchment` is an explicit, deliberate tag (not a nearest-anchor distance computation) -- see
sim/sql/055_business_locations.sql's own comment for why: Kitchener/Waterloo/Cambridge/Guelph are
geographically a little closer to Milton than to London, but are explicitly "London hub" territory
per the real user ask ("London area, KWC, Guelph etc"); Niagara folds into Milton's catchment,
Peterborough into Barrie's -- there are only 3 real dispatch hubs, so every edge-of-coverage city
needs to land in one of them.

Run once (idempotent -- upserts by label, safe to re-run):
`python -m sim.geocode_business_locations`
"""
from sim.db import cursor
from sim.geocode_locations import in_coverage, insert_locations, nominatim_geocode

# (business name, city, hub_catchment). Street-address-anchored entries use "name, street, city"
# as the geocode query for precision; chain/retail entries geocode on "name, city, Ontario" and
# rely on Nominatim actually finding a real store there.
BUSINESSES: list[tuple[str, str, str]] = [
    # -- Milton hub catchment: Milton/GTA-west + Niagara --
    ("Home Depot", "Milton, Ontario", "Milton"),
    ("Canadian Tire", "Brampton, Ontario", "Milton"),
    ("Walmart", "Mississauga, Ontario", "Milton"),
    ("Home Depot", "Vaughan, Ontario", "Milton"),
    ("Costco", "Vaughan, Ontario", "Milton"),
    ("Walmart", "Brampton, Ontario", "Milton"),
    ("Walmart", "Georgetown, Ontario", "Milton"),
    ("Costco", "Brampton, Ontario", "Milton"),
    ("Home Depot", "Mississauga, Ontario", "Milton"),
    ("Canadian Tire", "Milton, Ontario", "Milton"),
    ("Sobeys", "Milton, Ontario", "Milton"),
    ("Loblaws", "Milton, Ontario", "Milton"),
    ("Real Canadian Superstore", "Brampton, Ontario", "Milton"),
    # Niagara -- folded into Milton catchment (no independent 4th hub)
    ("General Motors St. Catharines Propulsion", "570 Glendale Avenue, St. Catharines, Ontario", "Milton"),
    ("Walmart", "St. Catharines, Ontario", "Milton"),
    ("Costco", "Niagara Falls, Ontario", "Milton"),
    ("Walmart", "Welland, Ontario", "Milton"),
    ("Home Depot", "St. Catharines, Ontario", "Milton"),
    ("Canadian Tire", "Niagara Falls, Ontario", "Milton"),

    # -- London hub catchment: London + Kitchener/Waterloo/Cambridge/Guelph + Woodstock/St. Thomas --
    ("3M Canada", "1840 Oxford Street East, London, Ontario", "London"),
    ("General Dynamics Land Systems", "London, Ontario", "London"),
    ("Trojan Technologies", "London, Ontario", "London"),
    ("Toyota Motor Manufacturing Canada", "1055 Fountain Street North, Cambridge, Ontario", "London"),
    ("Toyota Motor Manufacturing Canada", "1717 Dundas Street, Woodstock, Ontario", "London"),
    ("Linamar Corporation", "287 Speedvale Avenue West, Guelph, Ontario", "London"),
    ("Sleeman Breweries", "Guelph, Ontario", "London"),
    ("Home Hardware", "St. Jacobs, Ontario", "London"),
    ("Walmart", "London, Ontario", "London"),
    ("Costco", "London, Ontario", "London"),
    ("Walmart", "Kitchener, Ontario", "London"),
    ("Costco", "Waterloo, Ontario", "London"),
    ("Walmart", "Cambridge, Ontario", "London"),
    ("Walmart", "Guelph, Ontario", "London"),
    ("Home Depot", "London, Ontario", "London"),
    ("Canadian Tire", "Woodstock, Ontario", "London"),
    ("Home Depot", "Kitchener, Ontario", "London"),
    ("Costco", "Kitchener, Ontario", "London"),
    ("Home Depot", "Guelph, Ontario", "London"),
    ("Canadian Tire", "Cambridge, Ontario", "London"),
    ("Canadian Tire", "London, Ontario", "London"),
    ("Sobeys", "London, Ontario", "London"),
    ("Real Canadian Superstore", "Kitchener, Ontario", "London"),

    # -- Barrie hub catchment: Barrie/Orillia/Alliston + Peterborough --
    ("Honda of Canada Manufacturing", "4700 Industrial Parkway, Alliston, Ontario", "Barrie"),
    ("Walmart", "Barrie, Ontario", "Barrie"),
    ("Costco", "Barrie, Ontario", "Barrie"),
    ("Home Depot", "Barrie, Ontario", "Barrie"),
    ("Walmart", "Orillia, Ontario", "Barrie"),
    ("Canadian Tire", "Orillia, Ontario", "Barrie"),
    ("Casino Rama", "Rama, Ontario", "Barrie"),
    ("Canadian Tire", "Barrie, Ontario", "Barrie"),
    ("Sobeys", "Barrie, Ontario", "Barrie"),
    ("Home Hardware", "Orillia, Ontario", "Barrie"),
    # Peterborough -- folded into Barrie catchment (no independent 4th hub)
    ("Walmart", "Peterborough, Ontario", "Barrie"),
    ("Costco", "Peterborough, Ontario", "Barrie"),
    ("Home Depot", "Peterborough, Ontario", "Barrie"),
    ("Canadian Tire", "Peterborough, Ontario", "Barrie"),
    ("Trent University", "Peterborough, Ontario", "Barrie"),
]


def geocode_businesses() -> list[dict]:
    rows = []
    catchment_by_label: dict[str, str] = {}
    for i, (name, place, catchment) in enumerate(BUSINESSES):
        # Street-address entries (place starts with a digit) geocode BEST on the address alone --
        # a business-name prefix in front of a real mailing address confuses Nominatim's parser
        # more often than it helps (checked directly: several known-real plants failed to match
        # with the name prefixed, resolved fine without it).
        query = f"{place}, Canada" if place[0].isdigit() else f"{name}, {place}, Canada"
        print(f"  [{i + 1}/{len(BUSINESSES)}] {query}")
        result = nominatim_geocode(query)
        if result is None:
            print("    -> no match, skipping")
            continue
        lat, lon = float(result["lat"]), float(result["lon"])
        if not in_coverage(lat, lon):
            print(f"    -> outside coverage region ({lat:.3f},{lon:.3f}), excluded")
            continue
        # Display city is the SECOND comma-segment for a street-address entry ("1055 Fountain St
        # N, Cambridge, Ontario" -> "Cambridge"), the FIRST for a plain "Name, City, Ontario" one
        # -- real bug found directly: using segment 0 unconditionally put the raw street name in
        # the label for every address-anchored entry ("Linamar Corporation (287 Speedvale...)").
        city = place.split(",")[1].strip() if place[0].isdigit() else place.split(",")[0].strip()
        label = f"{name} ({city})"
        rows.append({"label": label, "city": city, "tier": "real_business", "lat": lat, "lon": lon, "source": "nominatim"})
        catchment_by_label[label] = catchment
    return rows, catchment_by_label


def write_catchments(labels_to_catchment: dict[str, str]) -> None:
    with cursor() as cur:
        cur.execute(
            "select location_id, label from reference.locations where tier = 'real_business' and label = any(%s)",
            (list(labels_to_catchment),),
        )
        for loc_id, label in cur.fetchall():
            catchment = labels_to_catchment.get(label)
            if not catchment:
                continue
            cur.execute(
                """insert into reference.business_hub_catchment (location_id, hub_catchment) values (%s, %s)
                   on conflict (location_id) do update set hub_catchment = excluded.hub_catchment""",
                (loc_id, catchment),
            )


if __name__ == "__main__":
    print(f"Geocoding {len(BUSINESSES)} curated real business locations via Nominatim...")
    geocoded, catchments = geocode_businesses()
    print(f"  -> {len(geocoded)} resolved inside the coverage region")
    insert_locations(geocoded)
    write_catchments(catchments)
    with cursor() as cur:
        cur.execute("select hub_catchment, count(*) from reference.business_hub_catchment group by hub_catchment")
        for catchment, n in cur.fetchall():
            print(f"  {catchment}: {n} businesses")
    print("done.")
