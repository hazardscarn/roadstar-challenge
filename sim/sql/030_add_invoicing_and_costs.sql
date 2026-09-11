-- Invoicing + cost tables (documents/logs/22's UI handoff -- the user asked directly for these,
-- and asked separately whether driver pay/full tax calc belongs in scope -- it doesn't: no real
-- driver pay-rate data exists anywhere in the source (ground_truth.drivers.pay_type is a real
-- code -- V/P/D -- but has no accompanying $ rate anywhere), and the hackathon brief's own
-- financial ask is shipper-side detention billing, not payroll. These two tables cover what IS
-- real/askable: an invoice sent TO the shipper, and an internal per-trip cost breakdown for
-- margin reporting -- both built from numbers the sim/live pipeline already computes.
--
-- Sourced structure (documents/logs/22): Ontario deliveries carry 13% HST (CRA: tax rate follows
-- the province of DELIVERY, not the carrier's home province -- a real trucking invoice needs a
-- province field for this reason, not just a flat assumed rate); CRA requires an invoice over
-- CAD 150 to show the recipient's name, date, description of supply, and GST/HST amount
-- SEPARATELY from the subtotal (not bundled into one total) -- reflected in the separate
-- subtotal/tax/total columns below, not a single amount field. Real trucking-specific line items
-- (linehaul, detention broken out by hours/rate, fuel surcharge, accessorials) come from a
-- surveyed real invoice-template structure, not guessed.

create table live.invoices (
  invoice_id uuid primary key default gen_random_uuid(),
  invoice_number text unique not null,  -- human-facing sequential number, not the uuid
  trip_id uuid references live.trips,
  quote_id uuid references live.quote_requests,
  issued_at timestamptz default now(),
  due_at timestamptz,
  -- CRA-required recipient/supplier fields (an invoice over CAD 150 must show these, per
  -- documents/logs/22's sourcing) -- SYNTHESIZED shipper contact info where the source data has
  -- none (no shipper-identity table exists anywhere in ground_truth/reference), flagged the same
  -- way every other synthesized $ figure in this project is.
  bill_to_name text,
  bill_to_address text,
  delivery_province text,  -- drives which province's HST/GST rate applies -- NOT always Ontario
  -- Line items, kept SEPARATE (not pre-summed) so the PDF can render CRA-compliant itemization:
  linehaul_amount numeric,
  detention_amount numeric,          -- from live.detention_billing.amount for this trip
  fuel_surcharge_amount numeric default 0,
  accessorial_amount numeric default 0,  -- lumper/layover/TONU/liftgate/oversize -- catch-all until itemized further
  subtotal numeric generated always as
    (coalesce(linehaul_amount,0) + coalesce(detention_amount,0) + coalesce(fuel_surcharge_amount,0) + coalesce(accessorial_amount,0)) stored,
  tax_rate numeric default 0.13,  -- SOURCED: Ontario HST -- see header comment on why this isn't hardcoded per-row
  tax_amount numeric generated always as
    ((coalesce(linehaul_amount,0) + coalesce(detention_amount,0) + coalesce(fuel_surcharge_amount,0) + coalesce(accessorial_amount,0)) * tax_rate) stored,
  total_amount numeric generated always as
    ((coalesce(linehaul_amount,0) + coalesce(detention_amount,0) + coalesce(fuel_surcharge_amount,0) + coalesce(accessorial_amount,0)) * (1 + tax_rate)) stored,
  status text default 'draft' check (status in ('draft','sent','paid','overdue','void')),
  sent_to_email text,
  sent_at timestamptz,
  pdf_url text  -- wherever the generated PDF ends up stored (Supabase Storage or similar)
);

-- Internal per-trip cost breakdown -- NOT shown to the shipper, used for margin/profitability
-- reporting on the manager dashboard. Every figure here is already computed by the reward
-- pipeline (sim/engine/reward.py) -- this table just persists it per REAL live trip instead of
-- only existing transiently during scoring, the same real-vs-synthesized rigor as everywhere
-- else: all these $ figures are the SAME synthesized-but-consistent rates used throughout this
-- project (sim/config.py), not new assumptions invented for invoicing.
create table live.trip_costs (
  trip_id uuid primary key references live.trips,
  loaded_miles numeric,
  deadhead_miles numeric,
  operating_cost_per_mile numeric,  -- from calibration.assumptions at the time, not hardcoded here
  fuel_and_operating_cost numeric generated always as ((coalesce(loaded_miles,0) + coalesce(deadhead_miles,0)) * coalesce(operating_cost_per_mile,0)) stored,
  maintenance_risk_cost numeric default 0,   -- expected_breakdown_cost() at decision time
  realized_breakdown_cost numeric default 0, -- ASSUMED_BREAKDOWN_COST_CAD if this trip actually had one
  lateness_penalty_cost numeric default 0,
  total_cost numeric generated always as
    ((coalesce(loaded_miles,0) + coalesce(deadhead_miles,0)) * coalesce(operating_cost_per_mile,0)
     + coalesce(maintenance_risk_cost,0) + coalesce(realized_breakdown_cost,0) + coalesce(lateness_penalty_cost,0)) stored,
  revenue numeric,  -- denormalized from the invoice/quote for a one-row margin view
  margin numeric generated always as
    (coalesce(revenue,0) - ((coalesce(loaded_miles,0) + coalesce(deadhead_miles,0)) * coalesce(operating_cost_per_mile,0)
     + coalesce(maintenance_risk_cost,0) + coalesce(realized_breakdown_cost,0) + coalesce(lateness_penalty_cost,0))) stored
);

-- Driver pay is explicitly OUT of scope (see header comment) -- this table intentionally does
-- NOT exist. If a future pass adds it, source real pay-type rate data first (ground_truth.drivers.
-- pay_type has no accompanying rate anywhere in the source export -- checked directly) or label
-- any $ figure SYNTHESIZED as loudly as every other assumed rate in sim/config.py.
