"""Unit tests for rollup computation and bracket display logic."""

import io

import pandas as pd

from src.parser import (
    AGE_BUCKETS,
    _soh_header_row,
    build_rollup_map,
    compute_rollups,
    format_cell,
    load_alias_map,
    parse_soh_counts,
)

ORDERED_A01 = [
    "Sides A0-1",
    "FQ A0-1",
    "HQ A0-1",
    "R&L A0-1",
    "Buttocks A1",
]


def _make_counts(**bucket_overrides) -> dict[str, dict[str, int]]:
    """Build counts for ORDERED_A01 groups; overrides use 'group__bucket' keys."""
    counts = {g: {b: 0 for b in AGE_BUCKETS} for g in ORDERED_A01}
    for key, val in bucket_overrides.items():
        group, bucket = key.rsplit("__", 1)
        counts[group][bucket] = val
    return counts


def _rollups_for(counts: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    rollup_map, _ = build_rollup_map(ORDERED_A01)
    return compute_rollups(counts, ORDERED_A01, rollup_map)


class TestComputeRollupsPositive:
    def test_sides_rolls_into_hq_same_bucket(self):
        counts = _make_counts(**{"Sides A0-1__0-4": 10, "HQ A0-1__0-4": 10})
        rollups = _rollups_for(counts)

        assert rollups["HQ A0-1"]["0-4"] == 10
        assert format_cell(10, rollups["HQ A0-1"]["0-4"]) == "10 (20)"


class TestComputeRollupsNegativeSkip:
    def test_negative_sides_does_not_reduce_hq_bracket(self):
        counts = _make_counts(**{"Sides A0-1__0-4": -5, "HQ A0-1__0-4": 10})
        rollups = _rollups_for(counts)

        assert rollups["HQ A0-1"]["0-4"] == 0
        assert format_cell(10, rollups["HQ A0-1"]["0-4"]) == "10"

    def test_negative_hq_does_not_reduce_rl_bracket(self):
        counts = _make_counts(**{"HQ A0-1__0-4": -3, "R&L A0-1__0-4": 8})
        rollups = _rollups_for(counts)

        assert rollups["R&L A0-1"]["0-4"] == 0
        assert format_cell(8, rollups["R&L A0-1"]["0-4"]) == "8"


class TestComputeRollupsMixedBuckets:
    def test_positive_higher_bucket_rolls_when_net_total_zero(self):
        """0-4 negative must not block 4-6 rollup when net total is zero."""
        counts = _make_counts(**{
            "Sides A0-1__0-4": -5,
            "Sides A0-1__4-6": 5,
            "HQ A0-1__0-4": 0,
            "HQ A0-1__4-6": 2,
        })
        rollups = _rollups_for(counts)

        assert rollups["HQ A0-1"]["0-4"] == 0
        assert rollups["HQ A0-1"]["4-6"] == 5
        assert format_cell(2, rollups["HQ A0-1"]["4-6"]) == "2 (7)"


class TestParseSohHeaderless:
    def test_headerless_file_counts_first_row(self):
        """SOH files without a header row must not drop the first carcass."""
        code_to_group, ordered_groups = load_alias_map()
        buf = io.BytesIO()
        pd.DataFrame(
            [
                ["BA2", "BEEF A2", "1626302", "16263038", "1.1"],
                ["BA2", "BEEF A2", "1626302", "16263037", "1.2"],
            ]
        ).to_excel(buf, index=False, header=False)
        buf.seek(0)
        buf.name = "headerless.xlsx"

        assert _soh_header_row(buf, code_to_group, "openpyxl") is None

        buf.seek(0)
        counts, unmapped = parse_soh_counts(buf, code_to_group, ordered_groups)
        assert unmapped == []
        assert counts["Sides A2"]["0-4"] == 2

