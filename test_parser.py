"""Quick smoke test – parse the sample file and print stats."""
import sys
import time

sys.path.insert(0, ".")
from gnss_parser import parse_gnss_file, sanitize_for_display
from gnss_export import build_completeness_report, build_quality_report, build_table_summary, export_excel

print("=" * 60)
print("GNSS Parser Smoke Test")
print("=" * 60)

t0 = time.perf_counter()
result = parse_gnss_file("sample_gnss_log.txt")
elapsed = time.perf_counter() - t0

print(f"\nParsed in {elapsed:.2f}s")
print(f"Source: {result.source_file}")
print(f"Device: {result.device_meta}")
print(f"Quarantined lines: {len(result.quarantine)}")

print("\n--- Tables ---")
for name, df in result.tables.items():
    if not df.empty:
        print(f"  {name:25s}  {len(df):>8,} rows × {len(df.columns):>3} cols")
        derived_cols = [c for c in df.columns if c.startswith("[DERIVED] ")]
        if derived_cols:
            print(f"    Derived cols: {derived_cols}")

print("\n--- Completeness Report (sample) ---")
cr = build_completeness_report(result)
if not cr.empty:
    print(cr.head(8).to_string(index=False))

print("\n--- Quality Report (top 10 by NullPct) ---")
qr = build_quality_report(result)
if not qr.empty:
    print(qr.sort_values("NullPct", ascending=False).head(10).to_string(index=False))

print("\n--- Table Summary ---")
ts = build_table_summary(result)
print(ts.to_string(index=False))

# Test Fix enrichment
if "Fix" in result.tables and not result.tables["Fix"].empty:
    fix = result.tables["Fix"]
    print("\n--- Fix sample (first 3 rows, select cols) ---")
    cols_of_interest = [c for c in fix.columns if c.startswith("[DERIVED] ") or c in ("Provider", "LatitudeDegrees", "LongitudeDegrees")]
    print(fix[cols_of_interest].head(3).to_string(index=False))

# Test 2^53 UI Display Sanitization
print("\n--- Testing 2^53 UI Display Sanitization ---")
raw_san = sanitize_for_display(result.tables["Raw"])
full_bias_type = type(raw_san["FullBiasNanos"].iloc[0])
print(f"  Raw.FullBiasNanos display type: {full_bias_type} (value: {raw_san['FullBiasNanos'].iloc[0]})")
assert full_bias_type is str, "FullBiasNanos must be str for safe UI display!"

# Test Excel generation
print("\n--- Testing Excel export ---")
try:
    excel_bytes = export_excel(result)
    print(f"  Excel workbook: {len(excel_bytes):,} bytes")
except Exception as e:
    print(f"  Excel export FAILED: {e}")
    raise

print("\n[OK] All checks passed!")
