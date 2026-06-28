"""Phase 2: Abaserve order parsing and customer-tier ATP deductions."""

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

import pandas as pd
import xlrd

from src.parser import AGE_BUCKETS, _closest_match, _group_suffix

# ── Constants ─────────────────────────────────────────────────────────────────

_ORDER_ID_RE = re.compile(r"^\d{5,6}$")

ORDER_TIER_COLS = ["0-4 (Boxer)", "4-6 (Shoprite)", "6-9 (Other)"]
ORDER_TOTAL_COL = "Total"
ORDER_DISPLAY_COLS = [ORDER_TOTAL_COL, *ORDER_TIER_COLS]
_TIER_TO_COL: dict[str, str] = {
    "boxer": "0-4 (Boxer)",
    "shoprite": "4-6 (Shoprite)",
    "default": "6-9 (Other)",
}

# Maps a special selling-group prefix to its normal fallback prefix.
# Used when the mapped group has zero direct stock.
_PISTOLA_FALLBACK: dict[str, str] = {
    "Pistola HQ ": "HQ ",
    "Pistola R&L ": "R&L ",
    "FQ SO ": "FQ ",
    "FQ SQC ": "FQ ",
}

# Bucket waterfall order per customer tier. 9+ is never used for deductions.
_TIER_BUCKETS: dict[str, list[str]] = {
    "boxer":    ["0-4"],
    "shoprite": ["4-6", "0-4"],
    "default":  ["6-9", "4-6", "0-4"],
}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class OrderLine:
    product_code: str
    customer: str
    qty: int


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_order_id(val) -> bool:
    """Return True if val is a 5–6 digit numeric order ID."""
    if val is None:
        return False
    if isinstance(val, float):
        if val != int(val):
            return False
        val = str(int(val))
    else:
        val = str(val).strip()
    return bool(_ORDER_ID_RE.fullmatch(val))


def _cell_to_python(sh, r: int, c: int, datemode: int):
    """Convert an xlrd cell to a Python value (str, float, datetime, or None)."""
    ctype = sh.cell_type(r, c)
    cval = sh.cell_value(r, c)

    if ctype == xlrd.XL_CELL_EMPTY:
        return None
    if ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cval, datemode)
        except Exception:
            return None
    if isinstance(cval, str):
        stripped = cval.strip()
        return stripped if stripped else None
    return cval


# ── Parsing ───────────────────────────────────────────────────────────────────

def _parse_grid(grid: list[list]) -> list[OrderLine]:
    """Parse a list-of-rows (already Python values) into OrderLine objects.

    This pure function is separated from file I/O to enable unit testing without XLS files.

    Row type detection (in priority order):
    - Blank (all None) → skip
    - Order line: col A is 5–6 digit int, col B is customer string, col E is positive qty → extract
    - Product summary: col A is a short alpha-numeric code, col B is string, col C is non-zero float → set current_product
    - Everything else (species banner, group header, subtotal, footer) → skip

    Col D (dispatch date) is explicitly ignored per spec — we never inspect it.
    """
    order_lines: list[OrderLine] = []
    current_product: str | None = None

    for row in grid:
        # Pad to at least 7 columns
        while len(row) < 7:
            row.append(None)

        a, b, c, d, e = row[0], row[1], row[2], row[3], row[4]

        # Blank row
        if all(v is None for v in row):
            continue

        # Order line: 5–6 digit order ID in A, customer string in B, positive qty in E.
        # Bulk-kg lines have D == 100.0 (spec sentinel) — skip those specifically.
        if _is_order_id(a) and isinstance(b, str) and b:
            if isinstance(d, (int, float)) and not isinstance(d, bool) and abs(d - 100.0) < 0.001:
                continue  # bulk-kg line
            if e is None:
                continue
            try:
                qty = int(float(e))
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            if current_product is None:
                continue
            order_lines.append(OrderLine(
                product_code=current_product,
                customer=str(b),
                qty=qty,
            ))
            continue

        # Product summary: short alpha-numeric code in A, description in B,
        # aggregate count (non-zero float) in C, more data in D and E
        if (
            a is not None
            and isinstance(a, str)
            and not _is_order_id(a)
            and isinstance(b, str)
            and b
            and isinstance(c, (int, float))
            and c != 0
            and d is not None
            and e is not None
        ):
            current_product = a
            continue

        # All other row types (species banner, group header, subtotals, footer) → skip

    return order_lines


def parse_orders(orders_file) -> list[OrderLine]:
    """Parse an Abaserve .xls dispatch report into a list of OrderLine objects.

    Args:
        orders_file: file-like object with .read() (Streamlit UploadedFile or BytesIO).

    Returns:
        List of OrderLine objects with product_code, customer, qty.
    """
    orders_file.seek(0)
    raw_bytes = orders_file.read()
    book = xlrd.open_workbook(file_contents=raw_bytes)
    sh = book.sheets()[0]

    grid = [
        [_cell_to_python(sh, r, c, book.datemode) for c in range(sh.ncols)]
        for r in range(sh.nrows)
    ]

    return _parse_grid(grid)


# ── Customer tier ─────────────────────────────────────────────────────────────

def customer_tier(name: str) -> Literal["boxer", "shoprite", "default"]:
    """Classify a customer name into a deduction tier.

    Priority: Boxer → Shoprite → default (SPAR, Pick n Pay, others).
    Matching is case-insensitive prefix check.
    """
    n = name.strip().lower()
    if n.startswith("boxer"):
        return "boxer"
    if n.startswith("shoprite"):
        return "shoprite"
    return "default"


# ── Deduction group resolution ────────────────────────────────────────────────

def resolve_deduction_group(
    group: str,
    counts: dict[str, dict[str, int]],
    ordered_groups: list[str],
) -> str:
    """Return the group to deduct from, applying Pistola / FQ-SO / FQ-SQC fallback.

    Fallback logic (spec §Pistola):
    - Try the mapped group first: if it has any direct bucket stock, use it.
    - When direct total is 0, swap the special prefix for the parallel normal prefix
      and use grade-band matching (_closest_match) to find the best candidate.
    """
    g_counts = counts.get(group, {})
    direct_total = sum(g_counts.get(b, 0) for b in AGE_BUCKETS)

    if direct_total > 0:
        return group

    for special_prefix, normal_prefix in _PISTOLA_FALLBACK.items():
        if group.startswith(special_prefix):
            suffix = group[len(special_prefix):]
            candidates = [g for g in ordered_groups if g.startswith(normal_prefix)]
            if candidates:
                return _closest_match(suffix, candidates)
            break

    return group


# ── Deduction engine ──────────────────────────────────────────────────────────

def apply_deductions(
    counts: dict[str, dict[str, int]],
    order_lines: list[OrderLine],
    code_to_group: dict[str, str],
    ordered_groups: list[str],
) -> list[str]:
    """Apply order deductions to counts in place using customer-tier bucket waterfalls.

    Deductions happen at Selling Group level. Multiple order lines for the same
    (group, tier) pair are aggregated before deducting.

    Shortfall handling: when the tier waterfall is exhausted, the remaining quantity
    is subtracted from the 0-4 bucket (may go negative). Other buckets stay floored at 0.

    Args:
        counts: mutable dict from parse_soh_counts (modified in place).
        order_lines: parsed order lines from parse_orders.
        code_to_group: product code → selling group mapping from Groups.csv.
        ordered_groups: ordered list of all selling groups.

    Returns:
        unmapped_orders: product codes from the orders file missing from Groups.csv.
    """
    # Aggregate orders by (selling_group, tier) before deducting to avoid
    # order-dependent results when multiple lines share a group + tier.
    agg: dict[tuple[str, str], int] = defaultdict(int)
    unmapped_orders: list[str] = []
    seen_unmapped: set[str] = set()

    for line in order_lines:
        group = code_to_group.get(line.product_code)
        if group is None:
            if line.product_code not in seen_unmapped:
                unmapped_orders.append(line.product_code)
                seen_unmapped.add(line.product_code)
            continue
        tier = customer_tier(line.customer)
        agg[(group, tier)] += line.qty

    for (group, tier), qty in agg.items():
        target = resolve_deduction_group(group, counts, ordered_groups)
        if target not in counts:
            continue

        remaining = qty
        for bucket in _TIER_BUCKETS[tier]:
            if remaining == 0:
                break
            available = counts[target][bucket]
            take = min(remaining, available)
            counts[target][bucket] -= take
            remaining -= take

        if remaining > 0:
            counts[target]["0-4"] -= remaining

    return unmapped_orders


# ── Orders summary (tier demand table) ────────────────────────────────────────

def aggregate_orders_by_tier(
    order_lines: list[OrderLine],
    code_to_group: dict[str, str],
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Aggregate order qty by selling group and customer tier.

    Each tier's total is placed in that tier's entry-bucket column (Boxer → 0-4, etc.).
    """
    agg: dict[tuple[str, str], int] = defaultdict(int)
    unmapped_orders: list[str] = []
    seen_unmapped: set[str] = set()

    for line in order_lines:
        group = code_to_group.get(line.product_code)
        if group is None:
            if line.product_code not in seen_unmapped:
                unmapped_orders.append(line.product_code)
                seen_unmapped.add(line.product_code)
            continue
        tier = customer_tier(line.customer)
        agg[(group, tier)] += line.qty

    by_group: dict[str, dict[str, int]] = defaultdict(
        lambda: {col: 0 for col in ORDER_TIER_COLS}
    )
    for (group, tier), qty in agg.items():
        by_group[group][_TIER_TO_COL[tier]] += qty

    return dict(by_group), unmapped_orders


def build_orders_df(
    order_lines: list[OrderLine],
    code_to_group: dict[str, str],
    ordered_groups: list[str],
) -> tuple[pd.DataFrame, list[str]]:
    """Build the Orders tab DataFrame — one row per selling group with any demand."""
    by_group, unmapped_orders = aggregate_orders_by_tier(order_lines, code_to_group)

    rows = []
    for group in ordered_groups:
        cols = by_group.get(group)
        if not cols or sum(cols.values()) == 0:
            continue
        rows.append({"Selling Group": group, **cols})

    if not rows:
        return pd.DataFrame(columns=["Selling Group", *ORDER_DISPLAY_COLS]), unmapped_orders

    df = pd.DataFrame(rows, columns=["Selling Group", *ORDER_TIER_COLS])
    df[ORDER_TOTAL_COL] = df[ORDER_TIER_COLS].sum(axis=1)
    return df[["Selling Group", *ORDER_DISPLAY_COLS]], unmapped_orders