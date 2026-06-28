"""Unit tests for Phase 2 order parsing and deduction logic.

All tests use in-memory data structures — no XLS fixture files required.
"""

import pytest

from src.orders import (
    OrderLine,
    _is_order_id,
    _parse_grid,
    aggregate_orders_by_tier,
    apply_deductions,
    build_orders_df,
    customer_tier,
    resolve_deduction_group,
)
from src.parser import AGE_BUCKETS, _counts_row


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_counts(groups: list[str], **bucket_overrides) -> dict[str, dict[str, int]]:
    """Build a counts dict with all buckets at 0, then apply overrides.

    bucket_overrides maps  "group__bucket" → value, e.g.:
        _make_counts(["FQ A2", "HQ A1"], **{"FQ A2__0-4": 3, "FQ A2__4-6": 2})
    """
    counts = {g: {b: 0 for b in AGE_BUCKETS} for g in groups}
    for key, val in bucket_overrides.items():
        group, bucket = key.rsplit("__", 1)
        counts[group][bucket] = val
    return counts


def _total(counts: dict, group: str) -> int:
    return _counts_row(counts, group)["Total Physical Stock"]


# ── customer_tier ─────────────────────────────────────────────────────────────

class TestCustomerTier:
    def test_boxer_exact(self):
        assert customer_tier("Boxer") == "boxer"

    def test_boxer_with_branch(self):
        assert customer_tier("Boxer Thembalethu Square - X481") == "boxer"

    def test_boxer_case_insensitive(self):
        assert customer_tier("BOXER KwaNokuthula") == "boxer"

    def test_shoprite_exact(self):
        assert customer_tier("SHOPRITE") == "shoprite"

    def test_shoprite_with_branch(self):
        assert customer_tier("SHOPRITE Checkers - Butterworth") == "shoprite"

    def test_shoprite_mixed_case(self):
        assert customer_tier("Shoprite EC - Despatch") == "shoprite"

    def test_spar_is_default(self):
        assert customer_tier("SPAR EC - Despatch Superspar") == "default"

    def test_pick_n_pay_is_default(self):
        assert customer_tier("Pick n Pay Hyper") == "default"

    def test_other_is_default(self):
        assert customer_tier("BROWNS BUTCHERY & CHESANYAMA") == "default"


# ── apply_deductions: Boxer tier ──────────────────────────────────────────────

class TestApplyDeductionsBoxer:
    def test_drains_only_0_4_bucket(self):
        counts = _make_counts(["FQ A2"], **{"FQ A2__0-4": 3, "FQ A2__4-6": 5})
        lines = [OrderLine("BA2F", "Boxer Store X", qty=2)]
        code_to_group = {"BA2F": "FQ A2"}
        apply_deductions(counts, lines, code_to_group, ["FQ A2"])

        assert counts["FQ A2"]["0-4"] == 1
        assert counts["FQ A2"]["4-6"] == 5  # untouched

    def test_shortfall_when_0_4_exhausted(self):
        """Boxer deducts 5 units but only 3 in 0-4 → 0-4 = -2."""
        counts = _make_counts(["FQ A2"], **{"FQ A2__0-4": 3})
        lines = [OrderLine("BA2F", "Boxer Store X", qty=5)]
        apply_deductions(counts, lines, {"BA2F": "FQ A2"}, ["FQ A2"])

        assert counts["FQ A2"]["0-4"] == -2
        assert counts["FQ A2"].get("_shortfall", 0) == 0
        assert _total(counts, "FQ A2") == -2

    def test_does_not_spill_to_4_6(self):
        """Boxer only touches 0-4; unmet demand goes negative on 0-4, not 4-6."""
        counts = _make_counts(["FQ A2"], **{"FQ A2__0-4": 0, "FQ A2__4-6": 10})
        lines = [OrderLine("BA2F", "Boxer Store X", qty=3)]
        apply_deductions(counts, lines, {"BA2F": "FQ A2"}, ["FQ A2"])

        assert counts["FQ A2"]["4-6"] == 10
        assert counts["FQ A2"]["0-4"] == -3
        assert counts["FQ A2"].get("_shortfall", 0) == 0


# ── apply_deductions: Shoprite tier ──────────────────────────────────────────

class TestApplyDeductionsShoprite:
    def test_drains_4_6_first_then_0_4(self):
        counts = _make_counts(["FQ A2"], **{"FQ A2__0-4": 4, "FQ A2__4-6": 3})
        lines = [OrderLine("BA2F", "SHOPRITE Checkers", qty=5)]
        apply_deductions(counts, lines, {"BA2F": "FQ A2"}, ["FQ A2"])

        assert counts["FQ A2"]["4-6"] == 0   # 3 taken
        assert counts["FQ A2"]["0-4"] == 2   # 2 taken from 0-4
        assert counts["FQ A2"].get("_shortfall", 0) == 0

    def test_does_not_touch_6_9(self):
        counts = _make_counts(["FQ A2"], **{
            "FQ A2__4-6": 0,
            "FQ A2__0-4": 0,
            "FQ A2__6-9": 10,
        })
        lines = [OrderLine("BA2F", "SHOPRITE Big Store", qty=2)]
        apply_deductions(counts, lines, {"BA2F": "FQ A2"}, ["FQ A2"])

        assert counts["FQ A2"]["6-9"] == 10  # untouched
        assert counts["FQ A2"]["0-4"] == -2
        assert counts["FQ A2"].get("_shortfall", 0) == 0


# ── apply_deductions: Default (SPAR) tier ────────────────────────────────────

class TestApplyDeductionsDefault:
    def test_drains_6_9_then_4_6_then_0_4(self):
        counts = _make_counts(["FQ A2"], **{
            "FQ A2__6-9": 2,
            "FQ A2__4-6": 3,
            "FQ A2__0-4": 4,
        })
        lines = [OrderLine("BA2F", "SPAR Superspar", qty=7)]
        apply_deductions(counts, lines, {"BA2F": "FQ A2"}, ["FQ A2"])

        assert counts["FQ A2"]["6-9"] == 0
        assert counts["FQ A2"]["4-6"] == 0
        assert counts["FQ A2"]["0-4"] == 2
        assert counts["FQ A2"].get("_shortfall", 0) == 0

    def test_9_plus_never_touched(self):
        counts = _make_counts(["FQ A2"], **{
            "FQ A2__6-9": 0,
            "FQ A2__4-6": 0,
            "FQ A2__0-4": 0,
            "FQ A2__9+": 10,
        })
        lines = [OrderLine("BA2F", "RNE Superspar", qty=5)]
        apply_deductions(counts, lines, {"BA2F": "FQ A2"}, ["FQ A2"])

        assert counts["FQ A2"]["9+"] == 10
        assert counts["FQ A2"]["0-4"] == -5
        assert counts["FQ A2"].get("_shortfall", 0) == 0


# ── resolve_deduction_group ───────────────────────────────────────────────────

class TestResolveDeductionGroup:
    def test_uses_mapped_group_when_stock_available(self):
        """Pistola HQ has stock → deducts from it directly."""
        ordered_groups = ["Pistola HQ A1", "HQ A0-1"]
        counts = _make_counts(ordered_groups, **{"Pistola HQ A1__0-4": 5})
        result = resolve_deduction_group("Pistola HQ A1", counts, ordered_groups)
        assert result == "Pistola HQ A1"

    def test_falls_back_to_hq_when_pistola_empty(self):
        """Pistola HQ has no stock → falls back to the matching HQ group."""
        ordered_groups = ["Pistola HQ A1", "HQ A0-1", "HQ AB2-4"]
        counts = _make_counts(ordered_groups)  # all zeros
        result = resolve_deduction_group("Pistola HQ A1", counts, ordered_groups)
        assert result == "HQ A0-1"

    def test_fq_so_fallback_to_fq(self):
        """FQ SO with no stock falls back to matching FQ group."""
        ordered_groups = ["FQ SO AB2", "FQ AB2", "FQ A0-1"]
        counts = _make_counts(ordered_groups)
        result = resolve_deduction_group("FQ SO AB2", counts, ordered_groups)
        assert result == "FQ AB2"

    def test_pistola_rl_fallback(self):
        """Pistola R&L with no stock falls back to matching R&L group."""
        ordered_groups = ["Pistola R&L AB3-6", "R&L AB3-6", "R&L A0-1"]
        counts = _make_counts(ordered_groups)
        result = resolve_deduction_group("Pistola R&L AB3-6", counts, ordered_groups)
        assert result == "R&L AB3-6"

    def test_non_special_group_returned_unchanged(self):
        """Normal FQ group is returned as-is (no fallback logic applies)."""
        ordered_groups = ["FQ A2", "HQ A2"]
        counts = _make_counts(ordered_groups)
        result = resolve_deduction_group("FQ A2", counts, ordered_groups)
        assert result == "FQ A2"


# ── aggregate_orders_by_tier / build_orders_df ────────────────────────────────

class TestAggregateOrdersByTier:
    def test_boxer_and_spar_on_same_group(self):
        lines = [
            OrderLine("BA2H", "Boxer Store X", 4),
            OrderLine("BA2H", "SPAR EC - Despatch", 6),
        ]
        code_to_group = {"BA2H": "HQ A2"}
        by_group, unmapped = aggregate_orders_by_tier(lines, code_to_group)

        assert unmapped == []
        assert by_group["HQ A2"]["0-4 (Boxer)"] == 4
        assert by_group["HQ A2"]["4-6 (Shoprite)"] == 0
        assert by_group["HQ A2"]["6-9 (Other)"] == 6

    def test_shoprite_goes_to_shoprite_column(self):
        lines = [OrderLine("BA2F", "SHOPRITE Checkers", 5)]
        code_to_group = {"BA2F": "FQ A2"}
        by_group, unmapped = aggregate_orders_by_tier(lines, code_to_group)

        assert unmapped == []
        assert by_group["FQ A2"]["4-6 (Shoprite)"] == 5

    def test_unmapped_code_collected(self):
        lines = [OrderLine("UNKNOWN", "Boxer Store", 3)]
        by_group, unmapped = aggregate_orders_by_tier(lines, {})

        assert by_group == {}
        assert unmapped == ["UNKNOWN"]

    def test_empty_orders(self):
        by_group, unmapped = aggregate_orders_by_tier([], {"BA2F": "FQ A2"})
        assert by_group == {}
        assert unmapped == []


class TestBuildOrdersDf:
    def test_filters_to_groups_with_demand(self):
        lines = [
            OrderLine("BA2H", "Boxer Store", 2),
            OrderLine("BA3H", "SPAR Store", 1),
        ]
        code_to_group = {"BA2H": "HQ A2", "BA3H": "HQ A3-6"}
        ordered = ["HQ A2", "HQ A3-6", "FQ A2"]
        df, unmapped = build_orders_df(lines, code_to_group, ordered)

        assert len(df) == 2
        assert list(df["Selling Group"]) == ["HQ A2", "HQ A3-6"]
        assert df.loc[df["Selling Group"] == "HQ A2", "0-4 (Boxer)"].iloc[0] == 2
        assert df.loc[df["Selling Group"] == "HQ A2", "Total"].iloc[0] == 2
        assert df.loc[df["Selling Group"] == "HQ A3-6", "Total"].iloc[0] == 1

    def test_empty_orders_returns_empty_df(self):
        df, unmapped = build_orders_df([], {"BA2F": "FQ A2"}, ["FQ A2"])
        assert df.empty
        assert unmapped == []


# ── _parse_grid (pure orders parsing) ────────────────────────────────────────

class TestParseGrid:
    def test_basic_extraction(self):
        """Standard product summary followed by two order lines."""
        grid = [
            # product summary row: code, desc, qty_count, weight, value
            ["BA2F", "BEEF A2 FORE QUARTER", 6.0, 418.14, 31695.01, None, None],
            # order lines — col D (date) is ignored, any non-100 value is fine
            ["382172", "Boxer Store A", None, "2026-06-18", 3.0, 100.0, 75.0],
            ["382173", "SHOPRITE Checkers", None, "2026-06-18", 2.0, 80.0, 60.0],
        ]
        lines = _parse_grid(grid)
        assert len(lines) == 2
        assert lines[0] == OrderLine("BA2F", "Boxer Store A", 3)
        assert lines[1] == OrderLine("BA2F", "SHOPRITE Checkers", 2)

    def test_bulk_kg_line_skipped(self):
        """Bulk-kg sentinel lines (D == 100.0) are skipped per spec."""
        grid = [
            ["BA2F", "BEEF A2 FORE QUARTER", 6.0, 418.14, 31695.01, None, None],
            ["382000", "Bulk Kg Customer", None, 100.0, 50.0, None, None],
        ]
        lines = _parse_grid(grid)
        assert lines == []

    def test_zero_qty_skipped(self):
        grid = [
            ["BA2F", "BEEF A2 FORE QUARTER", 6.0, 418.14, 31695.01, None, None],
            ["382001", "SPAR Superspar", None, "2026-06-18", 0.0, 0.0, 0.0],
        ]
        lines = _parse_grid(grid)
        assert lines == []

    def test_order_before_product_summary_skipped(self):
        """Order lines with no preceding product summary are discarded."""
        grid = [
            ["382001", "Boxer Store", None, "2026-06-18", 5.0, 200.0, 80.0],
        ]
        lines = _parse_grid(grid)
        assert lines == []

    def test_blank_rows_skipped(self):
        grid = [
            [None, None, None, None, None, None, None],
            ["BA2F", "BEEF A2 FORE QUARTER", 6.0, 418.14, 31695.01, None, None],
            [None, None, None, None, None, None, None],
            ["382001", "SPAR Store", None, "2026-06-18", 4.0, 160.0, 60.0],
        ]
        lines = _parse_grid(grid)
        assert len(lines) == 1
        assert lines[0].qty == 4

    def test_order_line_accepted_regardless_of_col_d_type(self):
        """Col D is ignored — order lines work whether D is a string, float, or None."""
        grid = [
            ["BA2F", "BEEF A2 FORE QUARTER", 6.0, 418.14, 31695.01, None, None],
            ["382001", "Boxer A", None, "2026-06-18", 2.0, 80.0, 70.0],  # string date
            ["382002", "Boxer B", None, 46191.0, 1.0, 40.0, 70.0],       # float date serial
            ["382003", "Boxer C", None, None, 3.0, 120.0, 70.0],          # None
        ]
        lines = _parse_grid(grid)
        assert len(lines) == 3
        assert all(l.product_code == "BA2F" for l in lines)

    def test_product_context_inherited_across_multiple_orders(self):
        """All order lines under the same summary inherit the same product code."""
        grid = [
            ["BA2F", "BEEF A2 FORE QUARTER", 6.0, 418.14, 31695.01, None, None],
            ["382001", "Boxer A", None, "2026-06-18", 1.0, 50.0, 70.0],
            ["382002", "Boxer B", None, "2026-06-18", 2.0, 100.0, 70.0],
            ["382003", "SPAR C",  None, "2026-06-18", 3.0, 150.0, 60.0],
        ]
        lines = _parse_grid(grid)
        assert all(l.product_code == "BA2F" for l in lines)
        assert len(lines) == 3

    def test_product_context_resets_on_new_summary(self):
        """A new product summary row updates current_product for subsequent orders."""
        grid = [
            ["BA2F", "BEEF A2 FORE QUARTER", 6.0, 418.14, 31695.01, None, None],
            ["382001", "Boxer A", None, "2026-06-18", 2.0, 80.0, 70.0],
            ["BA3F", "BEEF A3 FORE QUARTER", 2.0, 122.0, 8000.0, None, None],
            ["382002", "SPAR B",  None, "2026-06-18", 1.0, 60.0, 65.0],
        ]
        lines = _parse_grid(grid)
        assert lines[0].product_code == "BA2F"
        assert lines[1].product_code == "BA3F"
