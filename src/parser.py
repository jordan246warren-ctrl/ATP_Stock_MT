"""Phase 1 + Phase 2 orchestration: SOH ingestion, alias mapping, age-bucket aggregation, and inventory pipeline."""

import copy
import pathlib
import re
from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

AGE_BUCKETS = ("0-4", "4-6", "6-9", "9+")
NUMERIC_COLS = ["Total Physical Stock", *AGE_BUCKETS]

_GROUPS_CSV = pathlib.Path(__file__).parent.parent / "Groups.csv"

_GROUP_PREFIXES = (
    "Bull Sides ",
    "Bull HQ ",
    "Bull FQ ",
    "Sides ",
    "HQ ",
    "FQ ",
    "R&L ",
    "Buttocks ",
)


def _assign_bucket(days: float) -> str:
    """Map a decimal days-old value to its age bucket label."""
    if days < 4.0:
        return "0-4"
    elif days < 6.0:
        return "4-6"
    elif days < 9.0:
        return "6-9"
    else:
        return "9+"


def _group_suffix(group: str) -> str:
    for prefix in _GROUP_PREFIXES:
        if group.startswith(prefix):
            return group[len(prefix):]
    return group


def _is_rollup_excluded(group: str) -> bool:
    """Groups that never participate in rollup logic."""
    return (
        group.startswith("Pistola")
        or group.startswith("FQ SO")
        or group.startswith("FQ SQC")
        or group.startswith("Lambs")
        or group.startswith("Mutton")
        or group.startswith("Rams")
    )


def _classify_group(group: str) -> str | None:
    if _is_rollup_excluded(group):
        return None
    if group.startswith("Bull Sides "):
        return "bull_sides"
    if group.startswith("Bull HQ "):
        return "bull_hq"
    if group.startswith("Bull FQ "):
        return "bull_fq"
    if group.startswith("Sides "):
        return "sides"
    if group.startswith("HQ "):
        return "hq"
    if group.startswith("FQ "):
        return "fq"
    if group.startswith("R&L "):
        return "rl"
    if group.startswith("Buttocks "):
        return "buttocks"
    return None


def _parse_grade_set(suffix: str) -> set[int]:
    """Extract grade numbers from a selling-group suffix (e.g. A0-1 → {0, 1})."""
    nums = [int(n) for n in re.findall(r"\d+", suffix)]
    if not nums:
        return set()
    if "-" in suffix and len(nums) >= 2:
        return set(range(nums[0], nums[-1] + 1))
    return {nums[0]}


def _series_key(suffix: str) -> str:
    """Letter-series prefix before grade digits (A0-1 → A, AB3-6 → AB, B/C2 → B/C)."""
    match = re.match(r"^[A-Za-z/]+", suffix)
    return match.group(0) if match else suffix


_GRADE_FAMILY_RE = re.compile(r"(?:^|\s)(AB|B/C|A)(?=\d)")


def grade_family_key(group: str) -> str:
    """Letter grade family for spacer grouping (A, AB, B/C, …)."""
    suffix = _group_suffix(group)
    key = _series_key(suffix)
    if key and key[0] in "ABC":
        return key
    m = _GRADE_FAMILY_RE.search(group)
    return m.group(1) if m else ""


def _closest_match(source_suffix: str, candidates: list[str]) -> str:
    """Pick the candidate whose grade band best overlaps source_suffix."""
    if not candidates:
        raise ValueError("candidates must not be empty")
    if len(candidates) == 1:
        return candidates[0]

    source_grades = _parse_grade_set(source_suffix)
    source_series = _series_key(source_suffix)

    def score(candidate: str) -> tuple[int, int, int]:
        cand_suffix = _group_suffix(candidate)
        cand_grades = _parse_grade_set(cand_suffix)
        overlap = len(source_grades & cand_grades)
        series_match = 1 if _series_key(cand_suffix) == source_series else 0
        prefix_len = sum(
            1 for a, b in zip(source_suffix, cand_suffix) if a == b
        )
        return (series_match, overlap, prefix_len)

    return max(candidates, key=score)


def _groups_by_type(ordered_groups: list[str]) -> dict[str, list[str]]:
    by_type: dict[str, list[str]] = defaultdict(list)
    for group in ordered_groups:
        kind = _classify_group(group)
        if kind:
            by_type[kind].append(group)
    return by_type


def build_rollup_map(
    ordered_groups: list[str] | None = None,
    groups_path: pathlib.Path = _GROUPS_CSV,
) -> tuple[dict[str, list[str]], list[str]]:
    """Build source group → rollup target groups using global grade-band matching.

    Returns:
        rollup_map: each rollup source maps to its bracket target selling groups.
        missing: human-readable warnings when a required target type is absent.
    """
    if ordered_groups is None:
        _, ordered_groups = load_alias_map(groups_path)

    by_type = _groups_by_type(ordered_groups)
    rollup_map: dict[str, list[str]] = {}
    missing: list[str] = []

    for sides in by_type.get("sides", []):
        suffix = _group_suffix(sides)
        targets: list[str] = []
        for target_type, label in (
            ("fq", "FQ"),
            ("hq", "HQ"),
            ("rl", "R&L"),
            ("buttocks", "Buttocks"),
        ):
            candidates = by_type.get(target_type, [])
            if not candidates:
                missing.append(f"{sides}: no {label} group found in Groups.csv")
                continue
            targets.append(_closest_match(suffix, candidates))
        if targets:
            rollup_map[sides] = targets

    for hq in by_type.get("hq", []):
        suffix = _group_suffix(hq)
        targets = []
        for target_type, label in (("rl", "R&L"), ("buttocks", "Buttocks")):
            candidates = by_type.get(target_type, [])
            if not candidates:
                missing.append(f"{hq}: no {label} group found in Groups.csv")
                continue
            targets.append(_closest_match(suffix, candidates))
        if targets:
            rollup_map[hq] = targets

    for bull_sides in by_type.get("bull_sides", []):
        suffix = _group_suffix(bull_sides)
        targets = []
        for target_type, label in (("bull_fq", "Bull FQ"), ("bull_hq", "Bull HQ")):
            candidates = by_type.get(target_type, [])
            if not candidates:
                missing.append(f"{bull_sides}: no {label} group found in Groups.csv")
                continue
            targets.append(_closest_match(suffix, candidates))
        if targets:
            rollup_map[bull_sides] = targets

    return rollup_map, missing


def _empty_rollups(groups: list[str]) -> dict[str, dict[str, int]]:
    return {g: {col: 0 for col in NUMERIC_COLS} for g in groups}


def _counts_row(counts: dict[str, dict[str, int]], group: str) -> dict[str, int]:
    """Build a display row from per-bucket counts (0-4 may be negative after deductions)."""
    buckets = counts[group]
    total = sum(buckets.get(b, 0) for b in AGE_BUCKETS)
    return {
        "Total Physical Stock": total,
        **{b: buckets[b] for b in AGE_BUCKETS},
    }


def compute_rollups(
    counts: dict[str, dict[str, int]],
    ordered_groups: list[str],
    rollup_map: dict[str, list[str]],
) -> dict[str, dict[str, int]]:
    """Sum incoming rollup contributions per target group and column."""
    rollups = _empty_rollups(ordered_groups)

    for source, targets in rollup_map.items():
        if source not in counts:
            continue
        source_row = _counts_row(counts, source)
        for target in targets:
            if target not in rollups:
                continue
            for col in NUMERIC_COLS:
                val = source_row[col]
                if val > 0:
                    rollups[target][col] += val

    return rollups


def load_alias_map(
    groups_path: pathlib.Path = _GROUPS_CSV,
) -> tuple[dict[str, str], list[str]]:
    """Read Groups.csv and return a (code → group) mapping and an ordered group list.

    The order of selling groups reflects their first appearance in the file,
    which determines the display order in the output table.
    """
    df = pd.read_csv(groups_path, header=0, dtype=str, keep_default_na=False)
    df.columns = ["product_code", "description", "selling_group"]

    df = df[(df["product_code"].str.strip() != "") & (df["selling_group"].str.strip() != "")]

    code_to_group: dict[str, str] = {}
    ordered_groups: list[str] = []
    seen: set[str] = set()

    for _, row in df.iterrows():
        code = row["product_code"].strip()
        group = row["selling_group"].strip()
        code_to_group[code] = group
        if group not in seen:
            seen.add(group)
            ordered_groups.append(group)

    return code_to_group, ordered_groups


def _soh_header_row(soh_file, code_to_group: dict[str, str], engine: str) -> int | None:
    """Return Excel header row index, or None when the file has no header row."""
    soh_file.seek(0)
    first_row = pd.read_excel(soh_file, header=None, nrows=1, engine=engine, dtype=str)
    first_cell = str(first_row.iloc[0, 0]).strip()
    if first_cell in code_to_group:
        return None

    header_labels = {
        "product", "product code", "code", "description", "days", "days old", "barcode",
    }
    for value in first_row.iloc[0, : min(5, first_row.shape[1])]:
        label = str(value).strip().lower()
        if label in header_labels:
            return 0

    return 0


def parse_soh_counts(
    soh_file,
    code_to_group: dict[str, str],
    ordered_groups: list[str],
) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Parse an SOH file and return raw per-bucket counts plus unmapped codes.

    Every row in the SOH file represents exactly 1 unit of stock.
    Column positions used: A (index 0) = Product Code, E (index 4) = Days Old.

    Returns:
        counts: {selling_group: {age_bucket: int}}
        unmapped: product codes present in SOH but missing from Groups.csv.
    """
    filename = getattr(soh_file, "name", "")
    engine = "openpyxl" if filename.lower().endswith(".xlsx") else "xlrd"

    soh_file.seek(0)
    header_row = _soh_header_row(soh_file, code_to_group, engine)
    soh_file.seek(0)
    raw: pd.DataFrame = pd.read_excel(
        soh_file, header=header_row, engine=engine, dtype=str
    )

    raw = raw.dropna(how="all").reset_index(drop=True)
    raw = raw[~raw.iloc[:, 0].astype(str).str.strip().str.lower().str.contains("total", na=False)]
    raw = raw.reset_index(drop=True)

    if raw.shape[1] < 5:
        raise ValueError(
            f"SOH file has only {raw.shape[1]} column(s); "
            "expected at least 5 (Product Code in col A, Days Old in col E)."
        )

    counts: dict[str, dict[str, int]] = {
        g: {b: 0 for b in AGE_BUCKETS} for g in ordered_groups
    }
    unmapped: list[str] = []
    seen_unmapped: set[str] = set()

    for _, row in raw.iterrows():
        code = str(row.iloc[0]).strip()
        if not code or code.lower() == "nan":
            continue

        group = code_to_group.get(code)
        if group is None:
            if code not in seen_unmapped:
                unmapped.append(code)
                seen_unmapped.add(code)
            continue

        try:
            days = float(row.iloc[4])
        except (ValueError, TypeError):
            continue

        counts[group][_assign_bucket(days)] += 1

    return counts, unmapped


def build_inventory_df(
    counts: dict[str, dict[str, int]],
    ordered_groups: list[str],
    orders_applied: bool = False,
) -> tuple[pd.DataFrame, dict[str, dict[str, int]], list[str]]:
    """Compute rollups and build the visible inventory DataFrame.

    Args:
        counts: per-bucket tallies (0-4 may be negative after deductions).
        ordered_groups: display order from Groups.csv.
        orders_applied: when True, include rows with negative ATP (direct != 0 or incoming != 0).

    Returns:
        df: visible selling-group rows with direct counts.
        rollups: incoming rollup amounts per group/column.
        missing_rollup_targets: grade families missing required target groups.
    """
    rollup_map, missing_rollup_targets = build_rollup_map(ordered_groups)
    rollups = compute_rollups(counts, ordered_groups, rollup_map)

    rows = []
    for group in ordered_groups:
        row = {"Selling Group": group, **_counts_row(counts, group)}
        incoming_total = rollups[group]["Total Physical Stock"]
        direct_total = row["Total Physical Stock"]
        if orders_applied:
            if direct_total != 0 or incoming_total != 0:
                rows.append(row)
        else:
            if direct_total > 0 or incoming_total > 0:
                rows.append(row)

    df = pd.DataFrame(rows, columns=["Selling Group", *NUMERIC_COLS])
    return df, rollups, missing_rollup_targets


@dataclass
class InventoryViews:
    """Separate physical, ATP, and orders DataFrames for the three-tab UI."""

    physical_df: pd.DataFrame | None
    physical_rollups: dict[str, dict[str, int]] | None
    atp_df: pd.DataFrame | None
    atp_rollups: dict[str, dict[str, int]] | None
    orders_df: pd.DataFrame | None
    unmapped_soh: list[str]
    unmapped_orders: list[str]
    missing_rollup_targets: list[str]


def process_inventory_views(
    soh_file=None,
    orders_file=None,
) -> InventoryViews:
    """Build physical stock, ATP, and/or orders views from uploaded files.

    Physical Stock requires SOH. ATP requires SOH + orders. Orders requires orders only.
    """
    code_to_group, ordered_groups = load_alias_map()

    physical_df: pd.DataFrame | None = None
    physical_rollups: dict[str, dict[str, int]] | None = None
    atp_df: pd.DataFrame | None = None
    atp_rollups: dict[str, dict[str, int]] | None = None
    orders_df: pd.DataFrame | None = None
    unmapped_soh: list[str] = []
    unmapped_orders: list[str] = []
    missing_rollup_targets: list[str] = []

    order_lines = []
    if orders_file is not None:
        from src.orders import build_orders_df, parse_orders

        order_lines = parse_orders(orders_file)
        orders_df, unmapped_orders = build_orders_df(
            order_lines, code_to_group, ordered_groups
        )

    if soh_file is not None:
        counts_physical, unmapped_soh = parse_soh_counts(
            soh_file, code_to_group, ordered_groups
        )
        physical_df, physical_rollups, missing_rollup_targets = build_inventory_df(
            counts_physical, ordered_groups, orders_applied=False
        )

        if orders_file is not None:
            from src.orders import apply_deductions

            counts_atp = copy.deepcopy(counts_physical)
            deduct_unmapped = apply_deductions(
                counts_atp, order_lines, code_to_group, ordered_groups
            )
            if not unmapped_orders:
                unmapped_orders = deduct_unmapped
            atp_df, atp_rollups, _ = build_inventory_df(
                counts_atp, ordered_groups, orders_applied=True
            )

    return InventoryViews(
        physical_df=physical_df,
        physical_rollups=physical_rollups,
        atp_df=atp_df,
        atp_rollups=atp_rollups,
        orders_df=orders_df,
        unmapped_soh=unmapped_soh,
        unmapped_orders=unmapped_orders,
        missing_rollup_targets=missing_rollup_targets,
    )


def process_inventory(
    soh_file,
    orders_file=None,
) -> tuple[pd.DataFrame, dict[str, dict[str, int]], list[str], list[str], list[str]]:
    """Full ATP pipeline: parse SOH, optionally apply order deductions, build DataFrame.

    Args:
        soh_file: uploaded SOH file (.xls / .xlsx).
        orders_file: optional Abaserve orders file (.xls). When None, returns physical stock only.

    Returns:
        df: visible inventory rows.
        rollups: incoming rollup amounts per group/column.
        unmapped_soh: SOH product codes missing from Groups.csv.
        unmapped_orders: order product codes missing from Groups.csv.
        missing_rollup_targets: grade families missing required target groups.
    """
    views = process_inventory_views(soh_file, orders_file)
    if orders_file is not None:
        assert views.atp_df is not None and views.atp_rollups is not None
        return (
            views.atp_df,
            views.atp_rollups,
            views.unmapped_soh,
            views.unmapped_orders,
            views.missing_rollup_targets,
        )
    assert views.physical_df is not None and views.physical_rollups is not None
    return (
        views.physical_df,
        views.physical_rollups,
        views.unmapped_soh,
        views.unmapped_orders,
        views.missing_rollup_targets,
    )


def process_soh(
    soh_file,
) -> tuple[pd.DataFrame, dict[str, dict[str, int]], list[str], list[str]]:
    """Backward-compatible Phase 1 wrapper (no order deductions).

    Returns:
        df, rollups, unmapped, missing_rollup_targets
    """
    df, rollups, unmapped, _, missing = process_inventory(soh_file, orders_file=None)
    return df, rollups, unmapped, missing


def format_cell(direct: int, incoming: int) -> str:
    """Format a cell as plain n, dash, or direct (total)."""
    total = direct + incoming
    if total == 0 and direct == 0:
        return "—"
    if incoming == 0:
        return str(direct)
    return f"{direct} ({total})"
