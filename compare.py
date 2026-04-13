#!/usr/bin/env python3
"""
Replication Comparison Report

Compares AS400 journal entries with MSSQL Change Tracking to detect discrepancies.
Handles timezone differences automatically (AS400: UTC+0, MSSQL: UTC+7).
"""

import json
import sys
import os
from datetime import datetime
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from lib.as400_journal import AS400JournalReader
from lib.mssql_ct import MSSQLCTReader
from lib.comparator import ChangeComparator
from lib.timezone import (
    detect_as400_timezone,
    detect_mssql_timezone,
    normalize_to_as400_time,
    normalize_to_mssql_time,
    get_timezone_info,
    format_timezone_report
)
from lib.journal_cache import JournalCache


def detect_qadmcli_path() -> str:
    """Auto-detect qadmcli.sh path."""
    # Try relative to this script (replica-mon/ -> _qoder/ -> qadmcli/)
    script_dir = Path(__file__).parent
    base_dir = script_dir.parent
    qadmcli_path = base_dir / "qadmcli" / "qadmcli.sh"
    
    if qadmcli_path.exists():
        return str(qadmcli_path)
    
    # Fallback to relative path
    return "../qadmcli/qadmcli.sh"


def generate_report(
    source_table: str,
    target_table: str,
    since: str = None,
    output_format: str = "text",
    show_timezone: bool = True,
    use_cache: bool = True
):
    """
    Generate replication comparison report.
    
    Args:
        source_table: AS400 table in format "LIBRARY.TABLE"
        target_table: MSSQL table in format "SCHEMA.TABLE"
        since: Optional timestamp filter (in user's local timezone, assumed MSSQL timezone)
        output_format: "text" or "json"
        show_timezone: Whether to display timezone information
    """
    # Auto-detect qadmcli path
    qadmcli_path = detect_qadmcli_path()
    
    # Detect timezones
    if show_timezone:
        tz_info = get_timezone_info(qadmcli_path)
        as400_tz_offset = tz_info['as400']['utc_offset']
        mssql_tz_offset = tz_info['mssql']['utc_offset']
    else:
        # Fallback to known values
        as400_tz_offset = 0  # UTC+0
        mssql_tz_offset = 7  # UTC+7
        tz_info = None
    
    # Normalize timestamp for each database
    if since:
        # User provides time in local/MSSQL timezone
        # Convert to AS400 timezone for AS400 query
        since_for_as400 = normalize_to_as400_time(since, mssql_tz_offset, as400_tz_offset)
        # MSSQL uses the original time (already in MSSQL timezone)
        since_for_mssql = since
    else:
        since_for_as400 = None
        since_for_mssql = None
    
    print("=" * 70)
    print("REPLICATION COMPARISON REPORT")
    print("=" * 70)
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Source (AS400): {source_table}")
    print(f"Target (MSSQL): {target_table}")
    if since:
        print(f"Since (Local/MSSQL): {since_for_mssql}")
        if since_for_as400 != since_for_mssql:
            print(f"Since (AS400):       {since_for_as400}")
    
    # Display timezone info
    if tz_info:
        print(format_timezone_report(tz_info))
    
    # 1. Get AS400 journal summary (using normalized timestamp)
    print("[1/3] Querying AS400 journal...")
    journal_reader = AS400JournalReader(qadmcli_path=qadmcli_path, use_cache=use_cache)
    try:
        journal_summary = journal_reader.get_summary(source_table, since_for_as400)
        print(f"  ✓ Retrieved {journal_summary.get('total', 0)} journal entries")
        if journal_summary.get('from_cache'):
            print(f"  ℹ️  Data served from cache")
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return
    
    # 2. Get MSSQL CT summary (using original timestamp)
    print("[2/3] Querying MSSQL Change Tracking...")
    ct_reader = MSSQLCTReader(qadmcli_path=qadmcli_path)
    
    # Check if CT is enabled first
    ct_enabled = ct_reader.is_ct_enabled(target_table)
    if not ct_enabled:
        print(f"  ⚠️  Change Tracking is NOT enabled on {target_table}")
        print(f"  ⚠️  Falling back to COUNT(*) comparison (no CT log to cross-check)")
        print()
        
        # Fallback to row count comparison
        print("=" * 70)
        print("FALLBACK MODE: Row Count Comparison (CT Not Enabled)")
        print("=" * 70)
        print()
        print("⚠️  LIMITATION: Without Change Tracking, we can only compare")
        print("    current row counts, not operation history.")
        print("    This cannot detect: missed updates, deleted & reinserted rows,")
        print("    or operations that cancelled each other out.")
        print()
        
        # Import and use row count comparison
        try:
            from lib.row_count import compare_row_counts
            row_comparison = compare_row_counts(source_table, target_table)
            
            print(f"{'Table':<25} {'Row Count':>12} {'Status':>10}")
            print("-" * 50)
            print(f"{'Source (AS400)':<25} {row_comparison['source_count']:>12} {'✅':>10}")
            print(f"{'Target (MSSQL)':<25} {row_comparison['target_count']:>12} {'✅':>10}")
            print("-" * 50)
            
            diff = row_comparison['difference']
            status = "✅" if diff == 0 else "❌"
            print(f"{'Difference':<25} {diff:>+12} {status:>10}")
            print("=" * 50)
            
            if row_comparison['match']:
                print("\n✅ Row counts match!")
            else:
                print(f"\n⚠️  Row count mismatch: {diff} rows")
            print()
        except ImportError:
            print("  ✗ Row count comparison not available")
            print("  Install with: pip install row_count module")
        return
    
    try:
        ct_summary = ct_reader.get_summary(target_table, since)
        print(f"  ✓ Retrieved {ct_summary.get('total', 0)} CT changes")
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return
    
    # 3. Compare
    print("[3/3] Comparing...")
    comparator = ChangeComparator()
    comparison = comparator.compare(journal_summary, ct_summary)
    
    # 4. Generate report
    if output_format == "json":
        report = {
            "timestamp": datetime.now().isoformat(),
            "source_table": source_table,
            "target_table": target_table,
            "since": since,
            "journal_summary": journal_summary,
            "ct_summary": ct_summary,
            "comparison": comparison
        }
        print("\n" + json.dumps(report, indent=2))
    else:
        print("\n" + "=" * 70)
        print("COMPARISON RESULTS")
        print("=" * 70)
        
        print(f"\n{'Operation':<15} {'AS400 Journal':>15} {'MSSQL CT':>15} {'Difference':>12} {'Status':>10}")
        print("-" * 70)
        
        # Extract counts
        j_inserts = journal_summary.get('inserts', 0)
        j_updates = journal_summary.get('updates', 0)
        j_deletes = journal_summary.get('deletes', 0)
        j_total = journal_summary.get('total', 0)
        
        c_inserts = ct_summary.get('inserts', 0)
        c_updates = ct_summary.get('updates', 0)
        c_deletes = ct_summary.get('deletes', 0)
        c_total = ct_summary.get('total', 0)
        
        # Print rows
        for op_name, j_count, c_count in [
            ("INSERT", j_inserts, c_inserts),
            ("UPDATE", j_updates, c_updates),
            ("DELETE", j_deletes, c_deletes),
            ("TOTAL", j_total, c_total)
        ]:
            diff = j_count - c_count
            status = "✅" if diff == 0 else "❌"
            print(f"{op_name:<15} {j_count:>15} {c_count:>15} {diff:>+12} {status:>10}")
        
        print("=" * 70)
        
        # Overall status
        if comparison.get('match', False):
            print("\n✅ REPLICATION VERIFIED: All operations match!")
        else:
            print("\n❌ DISCREPANCY DETECTED!")
            print("\nDiscrepancies:")
            for disc in comparison.get('discrepancies', []):
                print(f"  - {disc}")
            
            # Auto-flag table for full caching
            if use_cache:
                cache = JournalCache()
                print(f"\n  🚨 Auto-flagging {source_table} for full journal caching...")
                cache.mark_requires_attention(
                    source_table,
                    reason=f"Discrepancy detected: {len(comparison.get('discrepancies', []))} mismatches"
                )
                print(f"  ℹ️  Full journal entries will be cached for investigation")
                print(f"  ℹ️  Use: python3 compare.py --cache-info --source {source_table}")
                print(f"  ℹ️  To reset: python3 compare.py --reset-attention --source {source_table}")
        
        print()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Generate replication comparison report",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Timezone Handling:
  The script automatically detects and handles timezone differences:
  - AS400: UTC+0
  - MSSQL: UTC+7 (Asia/Bangkok)
  - User timestamps are assumed to be in local/MSSQL timezone
  - Timestamps are automatically normalized for each database

Examples:
  # Compare with automatic timezone handling
  python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.Customers
  
  # Compare changes since specific time (in your local timezone)
  python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.Customers --since "2026-04-13 06:30:00"
  
  # Hide timezone information
  python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.Customers --no-timezone
        """
    )
    parser.add_argument("--source", help="AS400 table (LIBRARY.TABLE)")
    parser.add_argument("--target", help="MSSQL table (SCHEMA.TABLE)")
    parser.add_argument("--since", help="Filter since timestamp (YYYY-MM-DD HH:MM:SS, in local timezone)")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument("--no-timezone", action="store_true", help="Hide timezone information")
    parser.add_argument("--timezone-only", action="store_true", help="Only show timezone info and exit")
    parser.add_argument("--no-cache", action="store_true", help="Disable journal caching")
    parser.add_argument("--cache-info", action="store_true", help="Show cache information and exit")
    parser.add_argument("--clear-cache", action="store_true", help="Clear journal cache and exit")
    parser.add_argument("--reset-attention", action="store_true", help="Reset attention flag for table (downgrade to summary cache)")
    parser.add_argument("--list-attention", action="store_true", help="List all tables requiring attention")
    
    args = parser.parse_args()
    
    # Special case: show timezone info only
    if args.timezone_only:
        qadmcli_path = detect_qadmcli_path()
        tz_info = get_timezone_info(qadmcli_path)
        print("=" * 70)
        print("TIMEZONE INFORMATION")
        print("=" * 70)
        print(format_timezone_report(tz_info))
        sys.exit(0)
    
    # Special case: show cache info
    if args.cache_info:
        cache = JournalCache()
        if args.source:
            # Show info for specific table
            info = cache.get_cache_info(args.source)
            print("=" * 70)
            print(f"CACHE INFORMATION: {args.source}")
            print("=" * 70)
            print(f"  Cached: {info['cached']}")
            print(f"  Entry Count: {info['entry_count']}")
            print(f"  Last Timestamp: {info['last_timestamp'] or 'N/A'}")
            print(f"  Last Sequence: {info['last_sequence']}")
            print(f"  Cached At: {info['cached_at'] or 'N/A'}")
            print(f"  Cache Size: {info['cache_size_mb']} MB")
        else:
            # Show all cached tables
            print("=" * 70)
            print("CACHE INFORMATION (All Tables)")
            print("=" * 70)
            cache_files = list(cache.cache_dir.glob("*.meta.json"))
            if not cache_files:
                print("  No cached data found.")
            else:
                for meta_file in sorted(cache_files):
                    table_name = meta_file.stem.replace('_meta', '').replace('_', '.')
                    info = cache.get_cache_info(table_name)
                    print(f"\n  Table: {table_name}")
                    print(f"    Entries: {info['entry_count']}")
                    print(f"    Last Update: {info['cached_at'] or 'N/A'}")
                    print(f"    Size: {info['cache_size_mb']} MB")
        sys.exit(0)
    
    # Special case: clear cache
    if args.clear_cache:
        cache = JournalCache()
        if args.source:
            cache.clear_cache(args.source)
            print(f"✓ Cache cleared for {args.source}")
        else:
            cache.clear_cache()
            print("✓ All caches cleared")
        sys.exit(0)
    
    # Special case: reset attention flag
    if args.reset_attention:
        if not args.source:
            parser.error("--source is required with --reset-attention")
        cache = JournalCache()
        cache.reset_attention_flag(args.source, keep_full_cache=False)
        sys.exit(0)
    
    # Special case: list tables requiring attention
    if args.list_attention:
        cache = JournalCache()
        attention_tables = cache.get_tables_requiring_attention()
        print("=" * 70)
        print("TABLES REQUIRING ATTENTION")
        print("=" * 70)
        if not attention_tables:
            print("  No tables flagged for attention.")
        else:
            for table in attention_tables:
                info = cache.get_cache_info(table)
                print(f"\n  Table: {table}")
                print(f"    Flagged At: {info['discrepancy_detected_at'] or 'Unknown'}")
                print(f"    Reason: {info['full_cache_reason'] or 'Unknown'}")
                print(f"    Cache Level: {info['cache_level']}")
                print(f"    Entries Cached: {info['entry_count']}")
        sys.exit(0)
    
    # Validate required arguments for comparison
    if not args.source or not args.target:
        parser.error("--source and --target are required for comparison (or use --timezone-only, --cache-info, --clear-cache)")
    
    generate_report(
        source_table=args.source,
        target_table=args.target,
        since=args.since,
        output_format=args.format,
        show_timezone=not args.no_timezone,
        use_cache=not args.no_cache
    )
