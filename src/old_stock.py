"""Old-stock report: extract individual SOH items aged 6+ days (6-9 and 9+ buckets).

Completely independent of the ATP pipeline — reads the SOH file separately, never
mutates counts, and has no dependency on orders.py or any Phase 2 logic.
"""

import pathlib
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from src.parser import _soh_header_row

_GROUPS_CSV = pathlib.Path(__file__).parent.parent / "Groups.csv"

OLD_BUCKETS: tuple[str, str] = ("6-9", "9+")


@dataclass
class OldStockItem:
    selling_group: str
    days_old: float
    barcode: str
    bucket: Literal["6-9", "9+"]


def _assign_old_bucket(days: float) -> Literal["6-9", "9+"] | None:
    """Return the age bucket if it qualifies as old stock, else None."""
    if days < 6.0:
        return None
    if days < 9.0:
        return "6-9"
    return "9+"


def _load_code_to_group(groups_path: pathlib.Path = _GROUPS_CSV) -> dict[str, str]:
    """Read Groups.csv and return a product-code → selling-group mapping."""
    df = pd.read_csv(groups_path, header=0, dtype=str, keep_default_na=False)
    df.columns = ["product_code", "description", "selling_group"]
    df = df[
        (df["product_code"].str.strip() != "")
        & (df["selling_group"].str.strip() != "")
    ]
    return {
        row["product_code"].strip(): row["selling_group"].strip()
        for _, row in df.iterrows()
    }


def extract_old_stock(soh_file) -> list[OldStockItem]:
    """Parse a SOH file and return all items aged 6 or more days.

    Reads the file independently of the ATP pipeline — never touches counts or
    order deductions.

    Args:
        soh_file: file-like object with .read() and .seek() (Streamlit UploadedFile
                  or BytesIO).

    Returns:
        List of OldStockItem sorted 9+ first, then 6-9; within each bucket by
        days_old descending (oldest first).
    """
    code_to_group = _load_code_to_group()

    filename = getattr(soh_file, "name", "")
    engine = "openpyxl" if filename.lower().endswith(".xlsx") else "xlrd"

    soh_file.seek(0)
    header_row = _soh_header_row(soh_file, code_to_group, engine)
    soh_file.seek(0)
    raw: pd.DataFrame = pd.read_excel(
        soh_file, header=header_row, engine=engine, dtype=str
    )

    raw = raw.dropna(how="all").reset_index(drop=True)
    raw = raw[
        ~raw.iloc[:, 0].astype(str).str.strip().str.lower().str.contains("total", na=False)
    ].reset_index(drop=True)

    if raw.shape[1] < 5:
        raise ValueError(
            f"SOH file has only {raw.shape[1]} column(s); "
            "expected at least 5 (Product Code in col A, Barcode in col D, Days Old in col E)."
        )

    items: list[OldStockItem] = []

    for _, row in raw.iterrows():
        code = str(row.iloc[0]).strip()
        if not code or code.lower() == "nan":
            continue

        group = code_to_group.get(code)
        if group is None:
            continue

        try:
            days = float(row.iloc[4])
        except (ValueError, TypeError):
            continue

        bucket = _assign_old_bucket(days)
        if bucket is None:
            continue

        barcode = str(row.iloc[3]).strip()

        items.append(OldStockItem(
            selling_group=group,
            days_old=days,
            barcode=barcode,
            bucket=bucket,
        ))

    # Sort: 9+ first, then 6-9; within each bucket oldest first
    bucket_order = {"9+": 0, "6-9": 1}
    items.sort(key=lambda x: (bucket_order[x.bucket], -x.days_old))

    return items
