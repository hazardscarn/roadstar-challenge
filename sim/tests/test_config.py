"""Pure-function tests for sim/config.py's pricing/billing helpers -- no DB, no I/O."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sim.config import ASSUMED_DETENTION_RATE_PER_HR_CAD, leg_detention, quote_price


class TestLegDetention:
    """Per-leg detention (dashboard/server/main.py's Simulation Showcase week run) -- pickup and
    delivery each billed against their OWN 2-free-hour allowance, unlike live.detention_billing's
    cron job (sim/sql/010) which groups by trip_id only and conflates both legs' dwell into one
    figure -- a real, pre-existing gap this function fixes for the new callers."""

    def test_under_free_hours_bills_nothing(self):
        arr = datetime(2026, 1, 1, 8, 0)
        dep = arr + timedelta(hours=1, minutes=30)
        result = leg_detention(arr, dep)
        assert result["billable_hours"] == 0.0
        assert result["amount"] == 0.0

    def test_exactly_free_hours_bills_nothing(self):
        arr = datetime(2026, 1, 1, 8, 0)
        dep = arr + timedelta(hours=2)
        result = leg_detention(arr, dep)
        assert result["billable_hours"] == 0.0

    def test_over_free_hours_bills_the_excess_at_the_real_rate(self):
        arr = datetime(2026, 1, 1, 8, 0)
        dep = arr + timedelta(hours=5)  # 3h billable (5 - 2 free)
        result = leg_detention(arr, dep)
        assert result["billable_hours"] == 3.0
        assert result["amount"] == 3.0 * ASSUMED_DETENTION_RATE_PER_HR_CAD

    def test_two_legs_of_the_same_trip_are_billed_independently(self):
        """The exact scenario live's trip-only grouping gets wrong: a short pickup dwell (no
        detention) and a long delivery dwell (real detention) on the SAME trip must not average
        or sum into one blended, wrong figure -- each leg's own result is independent."""
        pickup_arr = datetime(2026, 1, 1, 8, 0)
        pickup_dep = pickup_arr + timedelta(minutes=45)  # well under 2h free -- $0
        delivery_arr = datetime(2026, 1, 1, 14, 0)
        delivery_dep = delivery_arr + timedelta(hours=4)  # 2h billable

        pickup = leg_detention(pickup_arr, pickup_dep)
        delivery = leg_detention(delivery_arr, delivery_dep)
        assert pickup["amount"] == 0.0
        assert delivery["amount"] == 2.0 * ASSUMED_DETENTION_RATE_PER_HR_CAD

    def test_missing_milestone_returns_zero_not_a_crash(self):
        assert leg_detention(None, None) == {"billable_hours": 0.0, "amount": 0.0}
        assert leg_detention(datetime(2026, 1, 1), None) == {"billable_hours": 0.0, "amount": 0.0}


class TestQuotePrice:
    """The customer-facing quote/invoice pricing (real 2026-researched rates) -- same function
    the live quote summary and invoice generation both call, so what a customer is quoted and
    what they're invoiced can never independently drift."""

    def test_ftl_ignores_fill_ratio(self):
        # FTL: the shipper pays for the whole truck regardless of how full it is.
        full = quote_price(100, "Dry Van", "FTL", fill_ratio=1.0)
        half = quote_price(100, "Dry Van", "FTL", fill_ratio=0.5)
        assert full["linehaul_amount"] == half["linehaul_amount"]

    def test_ltl_scales_with_fill_ratio(self):
        full = quote_price(100, "Dry Van", "LTL", fill_ratio=1.0)
        half = quote_price(100, "Dry Van", "LTL", fill_ratio=0.5)
        assert half["linehaul_amount"] < full["linehaul_amount"]

    def test_reefer_and_flatbed_cost_more_than_dry_van(self):
        van = quote_price(100, "Dry Van", "FTL")
        reefer = quote_price(100, "Reefer", "FTL")
        flatbed = quote_price(100, "Flatbed", "FTL")
        assert reefer["rate_per_mile"] > van["rate_per_mile"]
        assert flatbed["rate_per_mile"] > van["rate_per_mile"]

    def test_fuel_surcharge_is_a_positive_fraction_of_linehaul(self):
        pricing = quote_price(100, "Dry Van", "FTL")
        assert 0 < pricing["fuel_surcharge_amount"] < pricing["linehaul_amount"]
        assert pricing["estimated_total_charge"] == round(pricing["linehaul_amount"] + pricing["fuel_surcharge_amount"], 2)

    def test_zero_miles_is_zero_dollars_not_a_crash(self):
        pricing = quote_price(0, "Dry Van", "FTL")
        assert pricing["linehaul_amount"] == 0.0
        assert pricing["estimated_total_charge"] == 0.0
