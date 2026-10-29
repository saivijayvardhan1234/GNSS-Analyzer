"""
app.py – Streamlit dashboard for Android GNSS Logger data.

Launch
------
    streamlit run app.py

Architecture
────────────
┌─────────────────────────────────┐
│  Streamlit UI  (this file)      │
│  ┌────────────┐  ┌────────────┐ │
│  │  Sidebar    │  │  Main Area │ │
│  │  - Upload   │  │  - Audit   │ │
│  │  - Folder   │  │  - Charts  │ │
│  │  - Exports  │  │  - Tables  │ │
│  └────────────┘  └────────────┘ │
├─────────────────────────────────┤
│  gnss_parser.py   (parsing)     │
│  gnss_export.py   (exporting)   │
└─────────────────────────────────┘

The UI uses `st.session_state` to cache parsed data across reruns, and DuckDB
for any heavy analytical queries on large DataFrames.
"""

from __future__ import annotations

import io
import tempfile
import time
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from gnss_parser import ParseResult, parse_gnss_file, parse_gnss_folder

# Safely import or fallback to local implementation for JavaScript 2^53 integer protection
try:
    from gnss_parser import sanitize_for_display
except ImportError:
    JS_MAX_SAFE_INT = 9_007_199_254_740_991
    JS_MIN_SAFE_INT = -9_007_199_254_740_991

    def sanitize_for_display(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        df_out = df.copy()
        for col in df_out.columns:
            s = df_out[col]
            if pd.api.types.is_integer_dtype(s.dtype):
                v_min, v_max = s.min(), s.max()
                if pd.notna(v_min) and (v_min < JS_MIN_SAFE_INT or v_max > JS_MAX_SAFE_INT):
                    df_out[col] = s.astype(str)
            elif pd.api.types.is_object_dtype(s.dtype) or "Nanos" in col:
                has_unsafe = any(
                    isinstance(v, (int, float)) and (v > JS_MAX_SAFE_INT or v < JS_MIN_SAFE_INT)
                    for v in s.dropna()
                )
                if has_unsafe:
                    df_out[col] = s.apply(
                        lambda x: str(int(x)) if pd.notna(x) and isinstance(x, (int, float)) and (x > JS_MAX_SAFE_INT or x < JS_MIN_SAFE_INT)
                        else (str(x) if isinstance(x, int) and (x > JS_MAX_SAFE_INT or x < JS_MIN_SAFE_INT) else x)
                    )
        return df_out
from gnss_export import (
    build_quality_report,
    build_table_summary,
    export_excel,
    table_to_csv_bytes,
    table_to_parquet_bytes,
)

# Safely import or fallback to local implementation for build_completeness_report
try:
    from gnss_export import build_completeness_report
except ImportError:
    def build_completeness_report(result: ParseResult) -> pd.DataFrame:
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

# ─────────────────────── Page Config ──────────────────────────────────────────

st.set_page_config(
    page_title="GNSS Log Analyzer",
    page_icon="🛰️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────── Custom CSS ───────────────────────────────────────────

st.markdown("""
<style>
    /* Tighter spacing */
    .block-container { padding-top: 1.5rem; }
    /* Metric cards */
    div[data-testid="stMetric"] {
        background: linear-gradient(135deg, #0f2027 0%, #203a43 50%, #2c5364 100%);
        border-radius: 10px;
        padding: 12px 16px;
        color: white;
        border: 1px solid rgba(255,255,255,0.08);
    }
    div[data-testid="stMetric"] label { color: #8ec8e8 !important; }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] { color: #ffffff !important; }
    /* Tab styling */
    .stTabs [data-baseweb="tab"] { font-weight: 600; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────── Session State Init ───────────────────────────────────

if "parse_results" not in st.session_state:
    st.session_state.parse_results: list[ParseResult] = []
if "active_result_idx" not in st.session_state:
    st.session_state.active_result_idx = 0


def _set_results(results: list[ParseResult]):
    st.session_state.parse_results = results
    st.session_state.active_result_idx = 0


# ─────────────────────── Sidebar: Data Ingestion ──────────────────────────────

with st.sidebar:
    st.markdown("## 🛰️ GNSS Log Analyzer")
    st.caption("Parse · Visualize · Export")
    st.divider()

    input_mode = st.radio(
        "**Input method**",
        ["📁 Upload file(s)", "📂 Local folder path"],
        horizontal=True,
    )

    if input_mode == "📁 Upload file(s)":
        uploaded = st.file_uploader(
            "Upload GNSS Logger .txt file(s)",
            type=["txt"],
            accept_multiple_files=True,
        )
        if uploaded and st.button("🚀 Parse uploaded file(s)", type="primary", use_container_width=True):
            results = []
            progress = st.progress(0, text="Parsing…")
            for i, f in enumerate(uploaded):
                text_io = io.TextIOWrapper(io.BytesIO(f.read()), encoding="utf-8", errors="replace")
                res = parse_gnss_file(text_io, source_name=f.name)
                results.append(res)
                progress.progress((i + 1) / len(uploaded), text=f"Parsed {f.name}")
            _set_results(results)
            progress.empty()
            st.success(f"✅ Parsed {len(results)} file(s)")

    else:
        folder_path = st.text_input(
            "Enter absolute folder path",
            placeholder=r"C:\Users\...\gnss_logs",
        )
        if folder_path and st.button("🚀 Parse folder", type="primary", use_container_width=True):
            p = Path(folder_path)
            if not p.is_dir():
                st.error("❌ Path is not a valid directory")
            else:
                txt_files = list(p.glob("*.txt"))
                if not txt_files:
                    st.warning("⚠️ No .txt files found in that directory")
                else:
                    results = []
                    progress = st.progress(0, text="Parsing…")
                    for i, fp in enumerate(txt_files):
                        res = parse_gnss_file(fp)
                        results.append(res)
                        progress.progress((i + 1) / len(txt_files), text=f"Parsed {fp.name}")
                    _set_results(results)
                    progress.empty()
                    st.success(f"✅ Parsed {len(results)} file(s)")

    st.divider()

    # File selector (if multiple files loaded)
    results = st.session_state.parse_results
    if len(results) > 1:
        file_labels = [r.source_file for r in results]
        idx = st.selectbox("**Active file**", range(len(file_labels)),
                           format_func=lambda i: file_labels[i])
        st.session_state.active_result_idx = idx

# ─────────────────────── Guard: No Data ───────────────────────────────────────

if not st.session_state.parse_results:
    st.markdown("""
    # 🛰️ Android GNSS Logger Analyzer

    **Upload a GNSS Logger `.txt` file** or provide a folder path in the sidebar
    to get started.

    ### Features
    | Feature | Description |
    |---|---|
    | 🔍 **Dynamic Parsing** | Auto‑discovers schemas from header comments |
    | 📊 **Interactive Charts** | Select table, axes, grouping, chart type |
    | 📋 **Data Quality Audit** | Per‑column missingness & quarantine report |
    | 📥 **Multi‑format Export** | CSV, Parquet, and structured Excel workbook |
    | 🛡️ **Bulletproof** | Quarantines malformed lines; never crashes |
    """)
    st.stop()

# ─────────────────────── Active ParseResult ───────────────────────────────────

result: ParseResult = st.session_state.parse_results[st.session_state.active_result_idx]
tables = result.tables
non_empty_tables = {k: v for k, v in tables.items() if not v.empty}

# ─────────────────────── Header Metrics ───────────────────────────────────────

st.markdown(f"### 📡 **{result.source_file}**")
if result.device_meta:
    meta = result.device_meta
    st.caption(
        f"📱 {meta.get('Manufacturer', '?')} {meta.get('Model', '?')}  ·  "
        f"GNSS HW: {meta.get('GnssHardwareModel', '?')}  ·  "
        f"Logger: {meta.get('Version', '?')}"
    )

cols = st.columns(len(non_empty_tables) + 1)
for i, (tname, df) in enumerate(non_empty_tables.items()):
    cols[i].metric(tname, f"{len(df):,} rows")
cols[-1].metric("Quarantine", f"{len(result.quarantine):,} lines")

# ─────────────────────── Tabs ─────────────────────────────────────────────────

tab_audit, tab_chart, tab_explore, tab_export = st.tabs([
    "📋 Data Quality / Audit",
    "📊 Dynamic Charts",
    "🗂️ Table Explorer",
    "📥 Exports",
])

# ═══════════════════════ TAB: Data Quality ════════════════════════════════════

# ═══════════════════════ TAB: Data Quality ════════════════════════════════════

with tab_audit:
    subtab_completeness, subtab_missingness, subtab_summary, subtab_quarantine = st.tabs([
        "✅ Data Completeness",
        "🔍 Column Missingness",
        "📊 Table Summary",
        "🛡️ Quarantine Log",
    ])

    # ── Subtab 1: Data Completeness ──────────────────────────────────────────
    with subtab_completeness:
        st.subheader("Data Completeness Matrix")
        st.caption(
            "Exact percentage of non-null (populated) recorded rows across every active table and column. "
            "Raw data columns and `[DERIVED]` columns are clearly distinguished."
        )
        comp_df = build_completeness_report(result)
        if not comp_df.empty:
            c1, c2, c3 = st.columns([1.5, 1.2, 1.3])
            with c1:
                c_table = st.selectbox("Filter by table", ["(All)"] + list(non_empty_tables.keys()), key="c_table")
            with c2:
                c_origin = st.selectbox("Column type", ["(All)", "Raw only", "Derived only"], key="c_origin")
            with c3:
                c_min_pct = st.slider("Min Completeness %", 0, 100, 0, key="c_min_pct")

            c_mask = comp_df["CompletenessPct"] >= c_min_pct
            if c_table != "(All)":
                c_mask = c_mask & (comp_df["Table"] == c_table)
            if c_origin == "Raw only":
                c_mask = c_mask & (comp_df["Origin"] == "Raw")
            elif c_origin == "Derived only":
                c_mask = c_mask & (comp_df["Origin"] == "Derived")

            filtered_comp = comp_df[c_mask]

            # High-level completeness metrics
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Evaluated Columns", len(filtered_comp))
            m2.metric("100% Populated", int((filtered_comp["CompletenessPct"] == 100.0).sum()))
            m3.metric("Partial (0-100%)", int(((filtered_comp["CompletenessPct"] > 0) & (filtered_comp["CompletenessPct"] < 100.0)).sum()))
            m4.metric("Empty (0%)", int((filtered_comp["CompletenessPct"] == 0.0).sum()))

            st.dataframe(
                sanitize_for_display(filtered_comp),
                column_config={
                    "CompletenessPct": st.column_config.ProgressColumn(
                        "Completeness %",
                        help="Percentage of non-null recorded rows",
                        format="%.2f%%",
                        min_value=0,
                        max_value=100,
                    ),
                    "TotalRows": st.column_config.NumberColumn(format="%d"),
                    "PopulatedRows": st.column_config.NumberColumn(format="%d"),
                    "NullCount": st.column_config.NumberColumn(format="%d"),
                    "NullPct": st.column_config.NumberColumn(format="%.2f%%"),
                },
                use_container_width=True,
                hide_index=True,
                height=360,
            )

            # Completeness bar chart
            if not filtered_comp.empty:
                chart_comp = filtered_comp.sort_values("CompletenessPct", ascending=True)
                if len(chart_comp) > 30:
                    chart_comp = chart_comp.head(30)
                    st.caption("Showing 30 columns with lowest completeness for rapid triage:")
                fig_comp = px.bar(
                    chart_comp,
                    x="CompletenessPct",
                    y="Column",
                    color="Table" if c_table == "(All)" else "Origin",
                    orientation="h",
                    title=f"Data Completeness % by Column ({c_table})",
                    labels={"CompletenessPct": "Completeness %", "Column": "Column Name"},
                    range_x=[0, 100],
                )
                fig_comp.update_layout(
                    template="plotly_dark",
                    height=max(260, len(chart_comp) * 22),
                    margin=dict(l=10, r=10, t=40, b=10),
                )
                st.plotly_chart(fig_comp, use_container_width=True)
        else:
            st.info("No completeness data available.")

    # ── Subtab 2: Column Missingness ─────────────────────────────────────────
    with subtab_missingness:
        st.subheader("Column Missingness Audit")
        quality_df = build_quality_report(result)
        if not quality_df.empty:
            qc1, qc2 = st.columns(2)
            with qc1:
                q_table = st.selectbox("Filter by table", ["(All)"] + list(non_empty_tables.keys()), key="q_table")
            with qc2:
                q_thresh = st.slider("Show columns with ≥ N% missing", 0, 100, 0, key="q_thresh")

            mask = quality_df["NullPct"] >= q_thresh
            if q_table != "(All)":
                mask = mask & (quality_df["Table"] == q_table)

            filtered_qdf = quality_df[mask]

            st.dataframe(
                sanitize_for_display(filtered_qdf),
                column_config={
                    "NullPct": st.column_config.ProgressColumn(
                        "Missing %",
                        help="Percentage of missing / null values",
                        format="%.2f%%",
                        min_value=0,
                        max_value=100,
                    ),
                    "TotalRows": st.column_config.NumberColumn(format="%d"),
                    "NullCount": st.column_config.NumberColumn(format="%d"),
                },
                use_container_width=True,
                hide_index=True,
                height=350,
            )

            # Plotly visual breakdown of columns with missing data
            high_nulls = filtered_qdf[filtered_qdf["NullCount"] > 0].sort_values("NullPct", ascending=True)
            if not high_nulls.empty:
                chart_data = high_nulls.tail(25)
                fig_missing = px.bar(
                    chart_data,
                    x="NullPct",
                    y="Column",
                    color="Table" if q_table == "(All)" else None,
                    orientation="h",
                    title=f"Columns with Missing Values ({q_table})",
                    labels={"NullPct": "Missing %", "Column": "Column Name"},
                    range_x=[0, 100],
                )
                fig_missing.update_layout(
                    template="plotly_dark",
                    height=max(260, len(chart_data) * 22),
                    margin=dict(l=10, r=10, t=40, b=10),
                )
                st.plotly_chart(fig_missing, use_container_width=True)
            else:
                st.success("No missing values found for the selected filter!")
        else:
            st.info("No data to report.")

    # ── Subtab 3: Table Summary ──────────────────────────────────────────────
    with subtab_summary:
        st.subheader("Table Summary")
        summary_df = build_table_summary(result)
        st.dataframe(sanitize_for_display(summary_df), use_container_width=True, hide_index=True)

    # ── Subtab 4: Quarantine Log ─────────────────────────────────────────────
    with subtab_quarantine:
        st.subheader("Quarantined Lines")
        if result.quarantine.empty:
            st.success("🎉 No quarantined lines – all data parsed cleanly!")
        else:
            st.warning(f"⚠️ {len(result.quarantine)} lines quarantined")
            st.dataframe(sanitize_for_display(result.quarantine), use_container_width=True, hide_index=True, height=300)

# ═══════════════════════ TAB: Dynamic Charts ══════════════════════════════════

with tab_chart:
    if not non_empty_tables:
        st.info("No data tables available for charting.")
    else:
        chart_mode = st.radio(
            "Visualization Mode",
            ["📈 Cross-Table Timeline (Dual-Axis)", "📊 Single Table Chart"],
            horizontal=True,
            key="viz_mode",
        )

        # ── Cross‑Table Timeline Mode ─────────────────────────────────────────
        if chart_mode.startswith("📈"):
            st.subheader("Cross-Table Timeline")
            st.caption(
                "Compare parameters across tables with different sampling rates (e.g. 1Hz Fix vs 40Hz Status/Raw) "
                "on a synchronized timeline. The X-axis is forced to `[DERIVED] Time_UTC` with independent dual Y-axes."
            )

            # Discover all tables with a populated [DERIVED] Time_UTC column
            time_tables = [
                t for t, df in non_empty_tables.items()
                if "[DERIVED] Time_UTC" in df.columns and not df["[DERIVED] Time_UTC"].isna().all()
            ]

            if not time_tables:
                st.warning("No parsed tables contain a valid `[DERIVED] Time_UTC` timestamp.")
            else:
                row_t1, row_t2 = st.columns(2)
                with row_t1:
                    st.markdown("#### 🔵 Series 1 (Left Y-Axis)")
                    def_a_idx = time_tables.index("Fix") if "Fix" in time_tables else 0
                    table_a = st.selectbox("Table A", time_tables, index=def_a_idx, key="xt_a")
                    df_a_raw = non_empty_tables[table_a]
                    num_cols_a = list(df_a_raw.select_dtypes(include="number").columns)
                    if not num_cols_a:
                        st.warning(f"No numeric columns in {table_a}")
                        y1_col = None
                    else:
                        def_y1 = "AccuracyMeters" if "AccuracyMeters" in num_cols_a else num_cols_a[0]
                        y1_col = st.selectbox("Metric Y1 (Axis 1)", num_cols_a, index=num_cols_a.index(def_y1), key="xy1")

                with row_t2:
                    st.markdown("#### 🟠 Series 2 (Right Y-Axis)")
                    def_b_idx = 0
                    for pref in ["Raw", "Status", "Agc"]:
                        if pref in time_tables and pref != table_a:
                            def_b_idx = time_tables.index(pref)
                            break
                    table_b = st.selectbox("Table B", time_tables, index=def_b_idx, key="xt_b")
                    df_b_raw = non_empty_tables[table_b]
                    num_cols_b = list(df_b_raw.select_dtypes(include="number").columns)
                    if not num_cols_b:
                        st.warning(f"No numeric columns in {table_b}")
                        y2_col = None
                    else:
                        def_y2_candidates = ["Cn0DbHz", "AgcDb", "SpeedMps", "ElevationDegrees", "BasebandCn0DbHz"]
                        def_y2 = num_cols_b[0]
                        for c in def_y2_candidates:
                            if c in num_cols_b:
                                def_y2 = c
                                break
                        y2_col = st.selectbox("Metric Y2 (Axis 2)", num_cols_b, index=num_cols_b.index(def_y2), key="xy2")

                if y1_col and y2_col:
                    with st.expander("⚙️ Timeline Aggregation & Sampling Options", expanded=False):
                        ag1, ag2 = st.columns(2)
                        with ag1:
                            agg_a = st.selectbox(
                                f"{table_a} aggregation per timestamp",
                                ["Mean per timestamp", "Max per timestamp", "Median per timestamp", "Raw Points"],
                                key="agg_a",
                            )
                        with ag2:
                            agg_b = st.selectbox(
                                f"{table_b} aggregation per timestamp",
                                ["Mean per timestamp", "Max per timestamp", "Median per timestamp", "Raw Points"],
                                key="agg_b",
                            )

                    # Prepare Series A
                    df_a = df_a_raw.dropna(subset=["[DERIVED] Time_UTC", y1_col]).copy()
                    if agg_a != "Raw Points" and len(df_a) > 0:
                        fn_a = "mean" if "Mean" in agg_a else ("max" if "Max" in agg_a else "median")
                        df_a = df_a.groupby("[DERIVED] Time_UTC", as_index=False)[y1_col].agg(fn_a)
                    elif len(df_a) > 25_000:
                        df_a = df_a.sample(25_000, random_state=42).sort_values("[DERIVED] Time_UTC")

                    # Prepare Series B
                    df_b = df_b_raw.dropna(subset=["[DERIVED] Time_UTC", y2_col]).copy()
                    if agg_b != "Raw Points" and len(df_b) > 0:
                        fn_b = "mean" if "Mean" in agg_b else ("max" if "Max" in agg_b else "median")
                        df_b = df_b.groupby("[DERIVED] Time_UTC", as_index=False)[y2_col].agg(fn_b)
                    elif len(df_b) > 25_000:
                        df_b = df_b.sample(25_000, random_state=42).sort_values("[DERIVED] Time_UTC")

                    fig_cross = make_subplots(specs=[[{"secondary_y": True}]])
                    fig_cross.add_trace(
                        go.Scattergl(
                            x=df_a["[DERIVED] Time_UTC"],
                            y=df_a[y1_col],
                            name=f"{table_a}: {y1_col}",
                            mode="lines+markers" if len(df_a) < 1500 else "lines",
                            line=dict(color="#00d4ff", width=2),
                            marker=dict(size=4),
                        ),
                        secondary_y=False,
                    )
                    fig_cross.add_trace(
                        go.Scattergl(
                            x=df_b["[DERIVED] Time_UTC"],
                            y=df_b[y2_col],
                            name=f"{table_b}: {y2_col}",
                            mode="lines+markers" if len(df_b) < 1500 else "lines",
                            line=dict(color="#ff9900", width=2),
                            marker=dict(size=4),
                        ),
                        secondary_y=True,
                    )

                    fig_cross.update_xaxes(title_text="Time UTC ([DERIVED] Time_UTC)")
                    fig_cross.update_yaxes(title_text=f"{table_a} · {y1_col}", secondary_y=False)
                    fig_cross.update_yaxes(title_text=f"{table_b} · {y2_col}", secondary_y=True)
                    fig_cross.update_layout(
                        template="plotly_dark",
                        title=f"Cross-Table Timeline: {table_a} ({y1_col}) vs {table_b} ({y2_col})",
                        hovermode="x unified",
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                        margin=dict(l=50, r=50, t=60, b=40),
                        height=540,
                    )
                    st.plotly_chart(fig_cross, use_container_width=True)
                    st.caption(f"Series 1: {len(df_a):,} points ({table_a})  ·  Series 2: {len(df_b):,} points ({table_b})")

        # ── Single Table Chart Mode ───────────────────────────────────────────
        else:
            st.subheader("Build your chart")

            cc1, cc2 = st.columns(2)
            with cc1:
                chart_table = st.selectbox("📋 Table", list(non_empty_tables.keys()), key="ct")
            chart_df = non_empty_tables[chart_table]

            # For very large tables, offer sampling
            MAX_CHART_ROWS = 200_000
            sampled = False
            if len(chart_df) > MAX_CHART_ROWS:
                with cc2:
                    sample_opt = st.selectbox(
                        f"⚡ Table has {len(chart_df):,} rows",
                        [f"Sample {MAX_CHART_ROWS:,} rows", "Use all (may be slow)"],
                        key="sample_opt",
                    )
                if sample_opt.startswith("Sample"):
                    chart_df = chart_df.sample(n=MAX_CHART_ROWS, random_state=42).sort_index()
                    sampled = True

            all_cols = list(chart_df.columns)
            numeric_cols = list(chart_df.select_dtypes(include="number").columns)
            datetime_cols = list(chart_df.select_dtypes(include=["datetime", "datetimetz"]).columns)
            plottable_x = datetime_cols + numeric_cols
            cat_cols = [c for c in all_cols if c not in numeric_cols and c not in datetime_cols]

            c1, c2, c3, c4 = st.columns(4)
            with c1:
                chart_type = st.selectbox(
                    "Chart type",
                    ["Scatter", "Line", "Histogram", "Box", "Violin", "Bar (aggregated)"],
                    key="chart_type",
                )
            with c2:
                x_col = st.selectbox(
                    "X‑axis",
                    plottable_x if plottable_x else all_cols,
                    key="x_col",
                )
            with c3:
                y_options = ["(count)"] + numeric_cols if chart_type in ("Histogram", "Bar (aggregated)") else numeric_cols
                y_col = st.selectbox("Y‑axis", y_options, key="y_col")
            with c4:
                color_col = st.selectbox(
                    "Color / Group",
                    ["(none)"] + cat_cols + [c for c in numeric_cols if c != y_col],
                    key="color_col",
                )

            color = color_col if color_col != "(none)" else None

            # Build chart
            try:
                if chart_type == "Scatter":
                    fig = px.scatter(chart_df, x=x_col, y=y_col, color=color,
                                     opacity=0.6, render_mode="webgl")
                elif chart_type == "Line":
                    fig = px.line(chart_df, x=x_col, y=y_col, color=color)
                elif chart_type == "Histogram":
                    fig = px.histogram(chart_df, x=x_col, color=color, nbins=80)
                elif chart_type == "Box":
                    fig = px.box(chart_df, x=color, y=y_col)
                elif chart_type == "Violin":
                    fig = px.violin(chart_df, x=color, y=y_col, box=True)
                elif chart_type == "Bar (aggregated)":
                    if y_col == "(count)":
                        agg = chart_df.groupby(x_col).size().reset_index(name="count")
                        fig = px.bar(agg, x=x_col, y="count", color=color)
                    else:
                        agg = chart_df.groupby(x_col, as_index=False)[y_col].mean()
                        fig = px.bar(agg, x=x_col, y=y_col, color=color)
                else:
                    fig = px.scatter(chart_df, x=x_col, y=y_col, color=color)

                fig.update_layout(
                    template="plotly_dark",
                    margin=dict(l=40, r=20, t=40, b=40),
                    height=520,
                )
                if sampled:
                    st.caption(f"⚡ Showing a {MAX_CHART_ROWS:,}‑row sample for responsiveness")
                st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.error(f"Chart error: {e}")

            # ── Quick‑stat summary via DuckDB ─────────────────────────────────
            with st.expander("📈 Quick numeric stats (via DuckDB)"):
                try:
                    con = duckdb.connect()
                    con.register("tbl", chart_df)
                    stats = con.execute("SELECT * FROM (SUMMARIZE tbl)").fetchdf()
                    st.dataframe(sanitize_for_display(stats), use_container_width=True, hide_index=True)
                except Exception as e:
                    st.warning(f"DuckDB stats unavailable: {e}")

# ═══════════════════════ TAB: Table Explorer ══════════════════════════════════

with tab_explore:
    if not non_empty_tables:
        st.info("No tables to explore.")
    else:
        explore_table = st.selectbox("Select table", list(non_empty_tables.keys()), key="et")
        edf = non_empty_tables[explore_table]

        # Column filter
        show_cols = st.multiselect(
            "Show columns (leave blank for all)",
            edf.columns.tolist(),
            key="show_cols",
        )
        if show_cols:
            edf = edf[show_cols]

        # Optional DuckDB SQL filter
        with st.expander("🔎 Advanced: DuckDB SQL filter"):
            sql_query = st.text_area(
                "Enter a WHERE clause (applied to the selected table)",
                placeholder='e.g.  Cn0DbHz > 30 AND "[DERIVED] ConstellationName" = \'GPS\'',
                key="sql_filter",
            )
            if sql_query:
                try:
                    con = duckdb.connect()
                    con.register("tbl", edf)
                    edf = con.execute(f"SELECT * FROM tbl WHERE {sql_query}").fetchdf()
                    st.caption(f"Filtered to {len(edf):,} rows")
                except Exception as e:
                    st.error(f"SQL error: {e}")

        st.dataframe(sanitize_for_display(edf), use_container_width=True, hide_index=True, height=500)
        st.caption(f"{len(edf):,} rows × {len(edf.columns)} columns")

# ═══════════════════════ TAB: Exports ═════════════════════════════════════════

with tab_export:
    st.subheader("Download parsed data")

    if not non_empty_tables:
        st.info("No data to export.")
    else:
        # ── Per‑table CSV / Parquet ───────────────────────────────────────
        st.markdown("#### Per‑table downloads")
        exp_table = st.selectbox("Select table", list(non_empty_tables.keys()), key="exp_t")
        exp_df = non_empty_tables[exp_table]

        ec1, ec2 = st.columns(2)
        with ec1:
            st.download_button(
                f"⬇️ {exp_table}.csv",
                data=table_to_csv_bytes(exp_df),
                file_name=f"{exp_table}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with ec2:
            st.download_button(
                f"⬇️ {exp_table}.parquet",
                data=table_to_parquet_bytes(exp_df),
                file_name=f"{exp_table}.parquet",
                mime="application/octet-stream",
                use_container_width=True,
            )

        st.divider()

        # ── Multi‑sheet Excel ─────────────────────────────────────────────
        st.markdown("#### 📘 Multi‑Sheet Excel Workbook")
        st.caption(
            "Includes: Device metadata, Table summary, Data Completeness matrix, "
            "Data Quality audit, Quarantine log, and data sheets. Tables exceeding 1M rows "
            "are safely omitted and noted in a `_SKIPPED_TABLES` sheet."
        )

        # Show which tables will be included / skipped
        inc, skip = [], []
        for tname, df in non_empty_tables.items():
            if len(df) > 1_000_000:
                skip.append(f"❌ **{tname}** ({len(df):,} rows) – too large, skipped")
            else:
                inc.append(f"✅ **{tname}** ({len(df):,} rows)")
        st.markdown("  \n".join(inc + skip))

        if st.button("🔨 Generate Excel workbook", type="primary", use_container_width=True):
            with st.spinner("Building Excel workbook…"):
                excel_bytes = export_excel(result)
            st.download_button(
                "⬇️ Download workbook (.xlsx)",
                data=excel_bytes,
                file_name=f"GNSS_Report_{result.source_file.replace('.txt', '')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        st.divider()

        # ── Bulk export to folder ─────────────────────────────────────────
        st.markdown("#### 💾 Bulk export to local folder")
        bulk_path = st.text_input(
            "Output folder path",
            placeholder=r"C:\Users\...\gnss_exports",
            key="bulk_path",
        )
        bc1, bc2 = st.columns(2)
        if bulk_path:
            from gnss_export import export_csvs, export_parquets
            out = Path(bulk_path)
            with bc1:
                if st.button("Export all CSVs", use_container_width=True):
                    paths = export_csvs(result, out / "csv")
                    st.success(f"Wrote {len(paths)} CSV files to `{out / 'csv'}`")
            with bc2:
                if st.button("Export all Parquets", use_container_width=True):
                    paths = export_parquets(result, out / "parquet")
                    st.success(f"Wrote {len(paths)} Parquet files to `{out / 'parquet'}`")
