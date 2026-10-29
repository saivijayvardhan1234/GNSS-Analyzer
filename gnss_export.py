"""
gnss_export.py – Export helpers for CSV, Parquet, and Multi‑Sheet Excel.

Key Behaviours
──────────────
*   **Excel safety**: Tables with >1 000 000 rows are omitted from the .xlsx
    workbook.  A ``_SKIPPED_TABLES`` sheet is added listing which tables were
    omitted and their row counts, directing the user to CSV/Parquet.
*   **Excel workbook structure**:
    1.  ``_DeviceMeta``        – device / logger version info
    2.  ``_TableSummary``      – row count, column count, and time ranges
    3.  ``_DataCompleteness``  – per‑table column completeness % (populated)
    4.  ``_DataQuality``       – per‑table column missingness % (nulls)
    5.  ``_Quarantine``        – all malformed lines
    6.  ``_SKIPPED_TABLES``    – tables too large for Excel
    7.  One sheet per table that fits (Fix first, then alphabetical)
*   **Parquet & CSV** are unlimited; they get one file per table.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from gnss_parser import ParseResult

EXCEL_ROW_LIMIT = 1_000_000


# ─────────────────────── Data Completeness & Quality ──────────────────────────


def build_completeness_report(result: ParseResult) -> pd.DataFrame:
    """
    Build a Data Completeness matrix across all tables and columns.

    Returns a DataFrame with columns:
        Table, Column, Origin, TotalRows, PopulatedRows, CompletenessPct, NullCount, NullPct, DType
    """
    records = []
    for tname, df in result.tables.items():
        if df.empty:
            continue
        n = len(df)
        for col in df.columns:
            null_count = int(df[col].isna().sum())
            populated = n - null_count
            pop_pct = round(populated / n * 100, 2) if n else 0.0
            null_pct = round(null_count / n * 100, 2) if n else 0.0
            col_origin = "Derived" if col.startswith("[DERIVED] ") else "Raw"
            records.append({
                "Table": tname,
                "Column": col,
                "Origin": col_origin,
                "TotalRows": n,
                "PopulatedRows": populated,
                "CompletenessPct": pop_pct,
                "NullCount": null_count,
                "NullPct": null_pct,
                "DType": str(df[col].dtype),
            })
    return pd.DataFrame(records)


def build_quality_report(result: ParseResult) -> pd.DataFrame:
    """
    Build a Data‑Quality / Missingness audit report.

    Returns a DataFrame with columns:
        Table, Column, Origin, TotalRows, NullCount, NullPct, DType
    """
    records = []
    for tname, df in result.tables.items():
        if df.empty:
            continue
        n = len(df)
        for col in df.columns:
            null_count = int(df[col].isna().sum())
            col_origin = "Derived" if col.startswith("[DERIVED] ") else "Raw"
            records.append({
                "Table": tname,
                "Column": col,
                "Origin": col_origin,
                "TotalRows": n,
                "NullCount": null_count,
                "NullPct": round(null_count / n * 100, 2) if n else 0.0,
                "DType": str(df[col].dtype),
            })
    return pd.DataFrame(records)


def build_table_summary(result: ParseResult) -> pd.DataFrame:
    """One‑row‑per‑table summary: row count, column counts, time range."""
    records = []
    for tname, df in result.tables.items():
        raw_count = sum(1 for c in df.columns if not c.startswith("[DERIVED] "))
        derived_count = sum(1 for c in df.columns if c.startswith("[DERIVED] "))
        info = {
            "Table": tname,
            "Rows": len(df),
            "Columns": len(df.columns),
            "RawColumns": raw_count,
            "DerivedColumns": derived_count,
        }
        if "[DERIVED] Time_UTC" in df.columns and not df["[DERIVED] Time_UTC"].isna().all():
            info["TimeMin"] = df["[DERIVED] Time_UTC"].min()
            info["TimeMax"] = df["[DERIVED] Time_UTC"].max()
        else:
            info["TimeMin"] = None
            info["TimeMax"] = None
        records.append(info)
    return pd.DataFrame(records)


# ─────────────────────── CSV Export ───────────────────────────────────────────


def export_csvs(result: ParseResult, out_dir: str | Path) -> Dict[str, str]:
    """Write one CSV per table.  Returns dict of {table_name: filepath}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for tname, df in result.tables.items():
        if df.empty:
            continue
        fp = out_dir / f"{tname}.csv"
        df.to_csv(fp, index=False)
        paths[tname] = str(fp)
    return paths


# ─────────────────────── Parquet Export ────────────────────────────────────────


def export_parquets(result: ParseResult, out_dir: str | Path) -> Dict[str, str]:
    """Write one Parquet per table.  Returns dict of {table_name: filepath}."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for tname, df in result.tables.items():
        if df.empty:
            continue
        fp = out_dir / f"{tname}.parquet"
        # Ensure timezone-aware datetime columns are compatible with Parquet
        df_copy = df.copy()
        for col in df_copy.select_dtypes(include=["datetimetz"]).columns:
            df_copy[col] = df_copy[col].dt.tz_convert("UTC")
        df_copy.to_parquet(fp, index=False, engine="pyarrow")
        paths[tname] = str(fp)
    return paths


# ─────────────────────── Excel Export ─────────────────────────────────────────


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with all tz‑aware datetime columns made tz‑naive (UTC)."""
    df = df.copy()
    for col in df.select_dtypes(include=["datetimetz"]).columns:
        df[col] = df[col].dt.tz_convert("UTC").dt.tz_localize(None)
    return df


def export_excel(result: ParseResult, out_path: str | Path | None = None) -> bytes:
    """
    Build a multi‑sheet Excel workbook.

    If *out_path* is given the file is written to disk; in all cases the raw
    bytes of the workbook are returned (handy for Streamlit downloads).
    """
    buf = io.BytesIO()
    completeness_df = build_completeness_report(result)
    quality_df = build_quality_report(result)
    summary_df = build_table_summary(result)

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # 1. Device meta
        meta_df = pd.DataFrame(
            list(result.device_meta.items()), columns=["Key", "Value"]
        )
        meta_df.to_excel(writer, sheet_name="_DeviceMeta", index=False)

        # 2. Table summary (strip tz for Excel compatibility)
        _strip_tz(summary_df).to_excel(writer, sheet_name="_TableSummary", index=False)

        # 3. Data Completeness & Quality
        _strip_tz(completeness_df).to_excel(writer, sheet_name="_DataCompleteness", index=False)
        _strip_tz(quality_df).to_excel(writer, sheet_name="_DataQuality", index=False)

        # 4. Quarantine
        q = result.quarantine
        if not q.empty:
            q.head(EXCEL_ROW_LIMIT).to_excel(
                writer, sheet_name="_Quarantine", index=False
            )
        else:
            pd.DataFrame({"Info": ["No quarantined lines"]}).to_excel(
                writer, sheet_name="_Quarantine", index=False
            )

        # 5. Data tables – Fix first, then alphabetical; skip large ones
        skipped = []
        order = sorted(result.tables.keys())
        if "Fix" in order:
            order.remove("Fix")
            order.insert(0, "Fix")

        for tname in order:
            df = result.tables[tname]
            if df.empty:
                continue
            if len(df) > EXCEL_ROW_LIMIT:
                skipped.append({"Table": tname, "Rows": len(df),
                                "Note": "Use CSV/Parquet export for this table"})
                continue
            # Sanitise sheet name (Excel max 31 chars)
            sheet = tname[:31]
            _strip_tz(df).to_excel(writer, sheet_name=sheet, index=False)

        # 6. Skipped‑tables notice
        if skipped:
            pd.DataFrame(skipped).to_excel(
                writer, sheet_name="_SKIPPED_TABLES", index=False
            )

    excel_bytes = buf.getvalue()
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(excel_bytes)
    return excel_bytes


# ─────────────────────── In‑memory CSV / Parquet for Streamlit ────────────────


def table_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Serialize a DataFrame to CSV bytes (for Streamlit download)."""
    return df.to_csv(index=False).encode("utf-8")


def table_to_parquet_bytes(df: pd.DataFrame) -> bytes:
    """Serialize a DataFrame to Parquet bytes (for Streamlit download)."""
    buf = io.BytesIO()
    df_copy = df.copy()
    for col in df_copy.select_dtypes(include=["datetimetz"]).columns:
        df_copy[col] = df_copy[col].dt.tz_convert("UTC")
    df_copy.to_parquet(buf, index=False, engine="pyarrow")
    return buf.getvalue()
