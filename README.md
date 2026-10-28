# 🛰️ GNSS Log Analyzer

A **production-ready, local-first tool** for parsing, enriching, visualizing, and
exporting [Android GNSS Logger](https://github.com/google/gps-measurement-tools)
`.txt` data files — from 10-minute samples to multi-day, multi-device, multi-GB
collections.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     Streamlit UI  (app.py)                   │
│                                                              │
│  ┌──────────────┐  ┌──────────────────────────────────────┐  │
│  │   Sidebar     │  │           Main Area                  │  │
│  │  ─ Upload     │  │  📋 Data Quality / Audit             │  │
│  │  ─ Folder     │  │  📊 Dynamic Charts (Plotly WebGL)    │  │
│  │  ─ File sel.  │  │  🗂️ Table Explorer (DuckDB SQL)      │  │
│  └──────────────┘  │  📥 Exports (CSV / Parquet / Excel)   │  │
│                     └──────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────┤
│  gnss_parser.py     Pure-Python parser & enrichment engine   │
│  gnss_export.py     CSV · Parquet · Multi-Sheet Excel        │
└──────────────────────────────────────────────────────────────┘
```

### Tech Stack Rationale

| Component | Choice | Why |
|-----------|--------|-----|
| **UI** | Streamlit ≥ 1.38 | Fastest path from zero to interactive dashboard; native file-upload, download buttons, and wide layout |
| **DataFrames** | Pandas | Universal compatibility; small-to-medium tables fit entirely in RAM |
| **Analytical Queries** | DuckDB (in-process) | Column-oriented SQL engine for SUMMARIZE, filtering, and future GB-scale work — no server needed |
| **Charts** | Plotly (WebGL) | Hardware-accelerated scatter plots for 200K+ points; interactive zoom/pan |
| **Parquet I/O** | PyArrow | Industry-standard columnar format; 5–10× compression vs CSV |
| **Excel** | openpyxl | Multi-sheet .xlsx with formatting; handles the 1M-row limit gracefully |

---

## Project Structure

```
gnss/
├── app.py              # Streamlit dashboard (launch point)
├── gnss_parser.py      # Core parser + enrichment engine
├── gnss_export.py      # CSV / Parquet / Excel exporters
├── requirements.txt    # Python dependencies
├── test_parser.py      # Smoke-test script
├── sample_gnss_log.txt # (your sample data file)
└── README.md           # This file
```

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Launch the dashboard
streamlit run app.py

# 3. Open http://localhost:8501 in your browser
```

---

## Features in Detail

### 1. Ingestion & Parsing (`gnss_parser.py`)

| Feature | Implementation |
|---------|----------------|
| **Two input methods** | UI file upload (drag-and-drop) and local folder path for batch processing |
| **Dynamic schema discovery** | Reads `# Raw,col1,col2…` header comments at the top of every file to build per-table schemas. **No hard-coded column lists.** If Google adds columns in a future logger version, the parser auto-adapts. |
| **Null preservation** | Empty fields → `None`. Never interpolated, never filled with defaults. |
| **Quarantine system** | Truncated lines are padded with nulls and logged. Unknown record prefixes are captured. All malformed lines are routed to a `quarantine` DataFrame with `line_no`, `raw_line`, and `reason` — the parser never crashes. |
| **Device metadata** | Extracts `Version`, `Platform`, `Manufacturer`, `Model`, `GnssHardwareModel` from the header. |

### 2. Strict Data Separation & Enrichment

Raw data columns remain completely pristine and untouched. All enriched columns are explicitly prefixed with `[DERIVED] ` so there is zero ambiguity:

| Derived Column | Source | Description |
|-----------------|--------|-------------|
| `[DERIVED] Time_UTC` | `utcTimeMillis` / `UnixTimeMillis` | Human-readable UTC datetime |
| `[DERIVED] ConstellationName` | `ConstellationType` | GPS, GLONASS, Galileo, BeiDou, QZSS, IRNSS, SBAS |
| `[DERIVED] Band` | `CarrierFrequencyHz` | L1, L5, L2, B1I, E6, etc. |
| `[DERIVED] maxCn0` | `ExtraKeyValuePairs` (Fix table) | Parsed from `maxCn0=43\|meanCn0=29\|...` |
| `[DERIVED] meanCn0` | `ExtraKeyValuePairs` (Fix table) | Mean C/N₀ |
| `[DERIVED] satellites` | `ExtraKeyValuePairs` (Fix table) | Satellite count used in fix |

> **Key design**: All KV-pair keys are auto-discovered. If your device logs
> `hdop=1.2|vdop=2.1`, columns `[DERIVED] hdop` and `[DERIVED] vdop` appear
> automatically.

### 3. Dashboard UI (`app.py`)

The dashboard has **four tabs**:

#### 📋 Data Quality / Audit
- **Data Completeness Matrix**: Sub-tab showing every column across active tables with exact non-null populated row percentages, raw vs derived filters, progress bars, and triage charts
- **Column Missingness**: Interactive missingness audit with threshold filter and Plotly breakdown
- **Table Summary**: Row count, raw vs derived column count, and time ranges per table
- **Quarantine Viewer**: Malformed/truncated lines with line numbers and reasons

#### 📊 Dynamic Charts
- **📈 Cross-Table Timeline (Dual-Axis)**: Synchronized timeline forced to `[DERIVED] Time_UTC` on the X-axis. Plots Y1 from Table A (e.g., Fix accuracy) against Y2 from Table B (e.g., Raw/Status C/N₀ or AGC) on independent dual Y-axes, naturally accommodating different sampling rates (1Hz vs 40Hz) with selectable epoch aggregations.
- **📊 Single Table Chart**: Flexible multi-chart builder (Scatter WebGL, Line, Histogram, Box, Violin, Aggregated Bar) with auto-sampling for large tables.
- **DuckDB Quick Stats**: Instant `SUMMARIZE` statistics for plottable metrics.

#### 🗂️ Table Explorer
- Browse any parsed table with column selection
- **2^53 UI Safe Rendering**: High-precision nanosecond integer columns (`FullBiasNanos`, `TimeNanos`, etc.) are sanitized to strings for safe JavaScript/Streamlit rendering without truncating raw 64-bit numbers in exports.
- **DuckDB SQL filter**: Write custom WHERE clauses like `Cn0DbHz > 30 AND "[DERIVED] ConstellationName" = 'GPS'`

#### 📥 Exports
- **Per-table CSV & Parquet** download buttons
- **Multi-Sheet Excel Workbook** (structured `.xlsx` with completeness and audit sheets)
- **Bulk export** to a local directory

### 4. Multi-Sheet Excel Workbook

Designed for structured archiving and reporting:

| Sheet | Contents |
|-------|----------|
| `_DeviceMeta` | Logger version, device manufacturer, model, GNSS hardware |
| `_TableSummary` | One row per table: row count, column counts, time range |
| `_DataCompleteness` | Per-column completeness % (Table, Column, Origin, PopulatedRows, CompletenessPct) |
| `_DataQuality` | Per-column missingness (Table, Column, Origin, NullCount, NullPct) |
| `_Quarantine` | All malformed/truncated lines with reasons |
| `Fix` | Full Fix table (position solutions) |
| `Status` | Full Status table (satellite tracking) |
| `Agc` | Full AGC table (gain control) |
| `_SKIPPED_TABLES` | Tables exceeding 1M rows (e.g., Raw at 1Hz) with a note to use CSV/Parquet |

> **Excel safety**: Tables exceeding 1,048,576 rows are automatically excluded
> from the workbook and listed in `_SKIPPED_TABLES` with their row counts.

---

## Handling Large Data (Multi-GB)

For production datasets spanning days and multiple devices:

1. **Use the folder-path input** to batch-parse all `.txt` files in a directory.
2. **Export to Parquet** — columnar format with 5–10× compression, much faster
   to re-load than CSV.
3. **DuckDB SQL filter** in the Table Explorer to query subsets without loading
   everything into the chart.
4. **Auto-sampling** in charts prevents browser freezes on large tables.
5. For datasets that exceed RAM, consider loading the exported Parquet files
   directly with DuckDB:
   ```python
   import duckdb
   con = duckdb.connect()
   df = con.execute("SELECT * FROM 'exports/Raw.parquet' WHERE Cn0DbHz > 30").fetchdf()
   ```

---

## Constellation & Band Mappings

### ConstellationType → Name

| ID | Name |
|----|------|
| 0 | UNKNOWN |
| 1 | GPS |
| 2 | SBAS |
| 3 | GLONASS |
| 4 | QZSS |
| 5 | BDS (BeiDou) |
| 6 | Galileo |
| 7 | IRNSS (NavIC) |

### CarrierFrequencyHz → Band

| Frequency (MHz) | Band |
|-----------------|------|
| 1575.42 | L1 |
| 1176.45 | L5 |
| 1227.60 | L2 |
| 1561.098 | B1I (BeiDou) |
| 1207.14 | B2a/E5b |
| 1278.75 | E6 (Galileo) |
| 1600–1606 | G1 (GLONASS) |
| 1246–1257 | G2 (GLONASS) |

---

## Programmatic Usage

The parser can be used as a standalone Python library, independent of Streamlit:

```python
from gnss_parser import parse_gnss_file, parse_gnss_folder
from gnss_export import export_excel, export_parquets

# Parse a single file
result = parse_gnss_file("gnss_log_2024_03_15.txt")

# Access tables
raw_df   = result.tables["Raw"]       # 19,928 rows × 56 cols
fix_df   = result.tables["Fix"]       # 1,500 rows × 21 cols
status   = result.tables["Status"]    # 26,292 rows × 16 cols

# Check quarantine
print(f"Quarantined: {len(result.quarantine)} lines")

# Export
export_parquets(result, "output/parquet/")
excel_bytes = export_excel(result, "output/report.xlsx")

# Batch parse a folder
results = parse_gnss_folder("path/to/logs/")
```

---

## Verified Test Results

Tested against a real 15.5 MB Samsung Galaxy S25 GNSS Logger file:

```
Parsed in 1.86s
Device: samsung SM-S921E, Qualcomm GNSS GEN9, Logger v3.1.1.3
Quarantined lines: 0

Tables:
  Raw       19,928 rows × 56 cols  (+ [DERIVED] Time_UTC, [DERIVED] ConstellationName, [DERIVED] Band)
  Fix        1,500 rows × 21 cols  (+ [DERIVED] Time_UTC, [DERIVED] maxCn0, [DERIVED] meanCn0, [DERIVED] satellites)
  Status    26,292 rows × 16 cols  (+ [DERIVED] Time_UTC, [DERIVED] ConstellationName, [DERIVED] Band)
  Agc        7,156 rows × 16 cols  (+ [DERIVED] Time_UTC, [DERIVED] ConstellationName, [DERIVED] Band)

Excel workbook: 8,524,629 bytes (8.1 MB) — all checks passed
```

---

## License

MIT — use freely for research and education.
#   G N S S - A n a l y z e r  
 