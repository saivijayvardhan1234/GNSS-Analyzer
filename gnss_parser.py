"""
gnss_parser.py – Bulletproof parser for Android GNSS Logger .txt files.

Design Principles
─────────────────
1.  **Dynamic schema discovery** – Header comment lines (e.g. `# Raw,col1,col2…`)
    are parsed at load‑time to build a schema dict.  The parser never hard‑codes
    column names; if Google adds a column tomorrow, the parser handles it.
2.  **Zero data mutation** – Empty fields → ``None``.  No interpolation, no
    default filling, no silent coercion. Raw data remains untouched and pristine.
3.  **Strict Data Separation** – All enriched/generated columns are explicitly
    prefixed with `[DERIVED] ` in the column header (e.g. `[DERIVED] Time_UTC`).
4.  **Quarantine, never crash** – Malformed lines (wrong column count, truncated
    end‑of‑file, unknown record prefix) go into a quarantine list with the
    original line number, raw text, and a human‑readable reason.
5.  **2^53 UI Safe Rendering** – High-precision integer / uint64 columns (like
    FullBiasNanos, TimeNanos) are safely sanitized to strings for Streamlit UI
    rendering via `sanitize_for_display()`, while remaining numeric in exports.

Returns
-------
A ``ParseResult`` dataclass containing:
- ``tables``: ``dict[str, pd.DataFrame]`` with pristine raw + derived numeric data.
- ``display_tables``: ``dict[str, pd.DataFrame]`` safe for Streamlit/JS rendering.
- ``quarantine``: ``pd.DataFrame`` with columns [line_no, raw_line, reason].
- ``device_meta``: ``dict`` parsed from the file‑level header comment.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

# JavaScript Number.MAX_SAFE_INTEGER / MIN_SAFE_INTEGER (2^53 - 1)
JS_MAX_SAFE_INT = 9_007_199_254_740_991
JS_MIN_SAFE_INT = -9_007_199_254_740_991


def sanitize_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a copy of df safe for Streamlit / JavaScript table rendering.

    Any int64/uint64 or object/float columns containing integers that exceed
    JavaScript's Number.MAX_SAFE_INTEGER (2^53 - 1 = 9,007,199,254,740,991)
    or fall below MIN_SAFE_INTEGER are explicitly cast to string.
    This prevents 'value exceeds 2^53' JavaScript serialization crashes in
    st.dataframe while leaving raw numeric precision intact for Parquet/CSV exports.
    """
    if df is None or df.empty:
        return df

    df_out = df.copy()
    for col in df_out.columns:
        s = df_out[col]
        # Integer dtypes
        if pd.api.types.is_integer_dtype(s.dtype):
            v_min = s.min()
            v_max = s.max()
            if pd.notna(v_min) and (v_min < JS_MIN_SAFE_INT or v_max > JS_MAX_SAFE_INT):
                df_out[col] = s.astype(str)
        # Object dtypes or columns with Nanos in name
        elif pd.api.types.is_object_dtype(s.dtype) or "Nanos" in col:
            has_unsafe = False
            for v in s.dropna():
                if isinstance(v, (int, float)) and (v > JS_MAX_SAFE_INT or v < JS_MIN_SAFE_INT):
                    has_unsafe = True
                    break
            if has_unsafe:
                df_out[col] = s.apply(
                    lambda x: str(int(x)) if pd.notna(x) and isinstance(x, (int, float)) and (x > JS_MAX_SAFE_INT or x < JS_MIN_SAFE_INT)
                    else (str(x) if isinstance(x, int) and (x > JS_MAX_SAFE_INT or x < JS_MIN_SAFE_INT) else x)
                )
    return df_out


# ───────────────────────── Constellation & Band Maps ─────────────────────────

CONSTELLATION_MAP: Dict[int, str] = {
    0: "UNKNOWN",
    1: "GPS",
    2: "SBAS",
    3: "GLONASS",
    4: "QZSS",
    5: "BDS",       # BeiDou
    6: "Galileo",
    7: "IRNSS",     # NavIC
}

# Carrier frequency (MHz, rounded) → human‑readable band name.
# Mapping covers the major GNSS bands as of 2024.
_BAND_MAP_MHZ: Dict[Tuple[float, float], str] = {
    (1575.0, 1576.0):  "L1",
    (1176.0, 1177.0):  "L5",
    (1227.0, 1228.0):  "L2",
    (1600.0, 1606.0):  "G1 (GLONASS)",
    (1246.0, 1257.0):  "G2 (GLONASS)",
    (1561.0, 1562.0):  "B1I (BeiDou)",
    (1575.42 - 1, 1575.42 + 1): "B1C/E1",
    (1207.0, 1208.0):  "B2a/E5b",
    (1268.0, 1269.0):  "B3I (BeiDou)",
    (1278.0, 1279.0):  "E6 (Galileo)",
    (1603.0, 1604.0):  "G1 (GLONASS 1603)",
    (1604.0, 1605.0):  "G1 (GLONASS 1604)",
    (1602.0, 1603.0):  "G1 (GLONASS 1602)",
}


def _freq_to_band(freq_hz: Optional[float]) -> Optional[str]:
    """Convert carrier frequency in Hz to a human‑readable band name."""
    if freq_hz is None or pd.isna(freq_hz):
        return None
    mhz = freq_hz / 1e6
    for (lo, hi), name in _BAND_MAP_MHZ.items():
        if lo <= mhz <= hi:
            return name
    return f"{mhz:.1f} MHz"


# ──────────────────────── Result Dataclass ────────────────────────────────────

@dataclass
class ParseResult:
    tables: Dict[str, pd.DataFrame] = field(default_factory=dict)
    quarantine: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=["line_no", "raw_line", "reason"])
    )
    device_meta: Dict[str, str] = field(default_factory=dict)
    source_file: str = ""
    display_tables: Dict[str, pd.DataFrame] = field(default_factory=dict)

    def get_display_table(self, name: str) -> pd.DataFrame:
        """Return a copy of the table guaranteed safe for Streamlit/JS display."""
        if name in self.display_tables:
            return self.display_tables[name]
        if name in self.tables:
            return sanitize_for_display(self.tables[name])
        return pd.DataFrame()


# ──────────────────────── Header Parser ───────────────────────────────────────

_HEADER_RE = re.compile(r"^#\s+(\w+),(.+)$")
_META_RE = re.compile(
    r"Version:\s*(\S+)\s+Platform:\s*(\S+)\s+Manufacturer:\s*(\S+)\s+"
    r"Model:\s*(\S+)\s+GNSS Hardware Model Name:\s*(.+)$"
)


def _parse_headers(lines: List[str]) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Return (schemas, device_meta) by scanning comment lines."""
    schemas: Dict[str, List[str]] = {}
    device_meta: Dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        # Device metadata line
        m = _META_RE.search(stripped)
        if m:
            device_meta = {
                "Version": m.group(1),
                "Platform": m.group(2),
                "Manufacturer": m.group(3),
                "Model": m.group(4),
                "GnssHardwareModel": m.group(5).strip(),
            }
            continue
        # Schema definition
        m = _HEADER_RE.match(stripped)
        if m:
            table_name = m.group(1)
            cols = [c.strip() for c in m.group(2).split(",")]
            schemas[table_name] = cols
    return schemas, device_meta


# ──────────────────────── Line Parser ─────────────────────────────────────────


def _safe_cast(value: str) -> object:
    """Try int → float → string.  Empty strings → None."""
    v = value.strip()
    if v == "":
        return None
    # Fast‑path: ints (no dot, no 'e')
    if v.lstrip("-").isdigit():
        return int(v)
    try:
        return float(v)
    except ValueError:
        return v


def _parse_data_lines(
    lines: List[str],
    schemas: Dict[str, List[str]],
) -> Tuple[Dict[str, List[list]], List[dict]]:
    """Parse every non‑comment line.  Returns (table_rows, quarantine_rows)."""
    table_rows: Dict[str, List[list]] = {k: [] for k in schemas}
    quarantine: List[dict] = []

    for line_no_0, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        parts = stripped.split(",")
        prefix = parts[0]

        if prefix not in schemas:
            quarantine.append({
                "line_no": line_no_0,
                "raw_line": raw.rstrip("\n\r"),
                "reason": f"Unknown record prefix '{prefix}'",
            })
            continue

        expected_cols = schemas[prefix]
        data_values = parts[1:]  # everything after the prefix
        n_expected = len(expected_cols)

        if len(data_values) < n_expected:
            # Truncated line – pad with None so downstream gets correct width
            data_values.extend([""] * (n_expected - len(data_values)))
            quarantine.append({
                "line_no": line_no_0,
                "raw_line": raw.rstrip("\n\r"),
                "reason": (
                    f"Truncated: expected {n_expected} fields, got {len(parts) - 1}. "
                    f"Padded with nulls."
                ),
            })
        elif len(data_values) > n_expected:
            # Extra fields (could happen if a value contains commas, e.g. ExtraKeyValuePairs)
            # Strategy: keep first n_expected - 1 values, and join the rest as the last column
            overflow = ",".join(data_values[n_expected - 1:])
            data_values = data_values[: n_expected - 1] + [overflow]

        row = [_safe_cast(v) for v in data_values]
        table_rows[prefix].append(row)

    return table_rows, quarantine


# ──────────────────────── Enrichment ──────────────────────────────────────────


def _enrich_table(name: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    Add derived columns.
    STRICT DATA SEPARATION: All added columns are prefixed with `[DERIVED] `
    so original raw columns remain completely untouched and pristine.
    """
    if df.empty:
        return df

    # ── UTC datetime from UnixTimeMillis or utcTimeMillis ─────────────────
    time_col = None
    for candidate in ("UnixTimeMillis", "utcTimeMillis"):
        if candidate in df.columns:
            time_col = candidate
            break
    if time_col is not None:
        df["[DERIVED] Time_UTC"] = pd.to_datetime(
            pd.to_numeric(df[time_col], errors="coerce"),
            unit="ms",
            utc=True,
            errors="coerce",
        )

    # ── Constellation name ────────────────────────────────────────────────
    if "ConstellationType" in df.columns:
        ct = pd.to_numeric(df["ConstellationType"], errors="coerce")
        df["[DERIVED] ConstellationName"] = ct.map(CONSTELLATION_MAP).fillna("UNKNOWN")

    # ── Band name from CarrierFrequencyHz ─────────────────────────────────
    if "CarrierFrequencyHz" in df.columns:
        df["[DERIVED] Band"] = pd.to_numeric(
            df["CarrierFrequencyHz"], errors="coerce"
        ).apply(_freq_to_band)

    # ── Fix: parse ExtraKeyValuePairs ─────────────────────────────────────
    if name == "Fix" and "ExtraKeyValuePairs" in df.columns:
        kv_series = df["ExtraKeyValuePairs"].fillna("")
        # Discover all keys across the series
        all_keys: set = set()
        for val in kv_series:
            if isinstance(val, str) and val:
                for pair in val.split("|"):
                    if "=" in pair:
                        k, _ = pair.split("=", 1)
                        all_keys.add(k.strip())
        # Build derived columns
        for key in sorted(all_keys):
            col_name = f"[DERIVED] {key}"

            def _extract(s, _key=key):
                if not isinstance(s, str) or not s:
                    return None
                for pair in s.split("|"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        if k.strip() == _key:
                            try:
                                return float(v.strip())
                            except ValueError:
                                return v.strip()
                return None

            df[col_name] = kv_series.apply(_extract)

    # ── Fix: datetime for UnixTimeMillis (Fix uses different header) ──────
    if name == "Fix" and "UnixTimeMillis" in df.columns and "[DERIVED] Time_UTC" not in df.columns:
        df["[DERIVED] Time_UTC"] = pd.to_datetime(
            pd.to_numeric(df["UnixTimeMillis"], errors="coerce"),
            unit="ms",
            utc=True,
            errors="coerce",
        )

    return df


# ──────────────────────── Public API ──────────────────────────────────────────


def parse_gnss_file(filepath: str | Path | io.TextIOBase, source_name: str = "") -> ParseResult:
    """
    Parse a single Android GNSS Logger .txt file.

    Parameters
    ----------
    filepath : path‑like or file‑like
        The GNSS Logger text file.
    source_name : str
        A label for the source (used in reporting).  Defaults to the filename.

    Returns
    -------
    ParseResult
    """
    if isinstance(filepath, (str, Path)):
        path = Path(filepath)
        source_name = source_name or path.name
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    else:
        # File‑like (e.g. Streamlit UploadedFile wrapped in TextIOWrapper)
        source_name = source_name or "uploaded_file"
        lines = filepath.readlines()
        if lines and isinstance(lines[0], bytes):
            lines = [l.decode("utf-8", errors="replace") for l in lines]

    schemas, device_meta = _parse_headers(lines)

    if not schemas:
        # No schemas found – everything goes to quarantine
        quarantine_rows = [
            {"line_no": i + 1, "raw_line": l.rstrip("\n\r"), "reason": "No schemas found in header"}
            for i, l in enumerate(lines)
            if l.strip() and not l.strip().startswith("#")
        ]
        return ParseResult(
            quarantine=pd.DataFrame(quarantine_rows),
            device_meta=device_meta,
            source_file=source_name,
            display_tables={},
        )

    table_rows, quarantine_rows = _parse_data_lines(lines, schemas)

    # Build DataFrames
    tables: Dict[str, pd.DataFrame] = {}
    for tname, cols in schemas.items():
        rows = table_rows.get(tname, [])
        if rows:
            df = pd.DataFrame(rows, columns=cols)
            df = _enrich_table(tname, df)
            tables[tname] = df
        else:
            tables[tname] = pd.DataFrame(columns=cols)

    quarantine_df = pd.DataFrame(quarantine_rows) if quarantine_rows else pd.DataFrame(
        columns=["line_no", "raw_line", "reason"]
    )

    # Build sanitized display tables safe for Streamlit/JavaScript (2^53 integer protection)
    display_tables: Dict[str, pd.DataFrame] = {
        tname: sanitize_for_display(df) for tname, df in tables.items()
    }

    return ParseResult(
        tables=tables,
        quarantine=quarantine_df,
        device_meta=device_meta,
        source_file=source_name,
        display_tables=display_tables,
    )


def parse_gnss_folder(folder: str | Path) -> List[ParseResult]:
    """Parse all .txt files in a folder.  Returns a list of ParseResult."""
    folder = Path(folder)
    results = []
    for fp in sorted(folder.glob("*.txt")):
        results.append(parse_gnss_file(fp))
    return results
