#!/usr/bin/env python3
"""
Replication Comparison Report

Compares AS400 journal entries with MSSQL Change Tracking to detect discrepancies.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from lib.as400_journal import AS400JournalReader
from lib.mssql_ct import MSSQLCTReader
from lib.comparator import ChangeComparator


def generate_report(
    source_table: str,
    target_table: str,
    since: str = None,
    output_format: str = "text"
):
    """
    Generate replication comparison report.
    
    Args:
        source_table: AS400 table in format "LIBRARY.TABLE"
        target_table: MSSQL table in format "SCHEMA.TABLE"
        since: Optional timestamp filter
        output_format: "text" or "json"
    """
    print("=" * 70)
    print("REPLICATION COMPARISON REPORT")
    print("=" * 70)
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Source (AS400): {source_table}")
    print(f"Target (MSSQL): {target_table}")
    if since:
        print(f"Since: {since}")
    print()
    
    # 1. Get AS400 journal summary
    print("[1/3] Querying AS400 journal...")
    journal_reader = AS400JournalReader()
    try:
        journal_summary = journal_reader.get_summary(source_table, since)
        print(f"  ✓ Retrieved {journal_summary.get('total', 0)} journal entries")
    except Exception as e:
        print(f"  ✗ Error: {e}")
        return
    
    # 2. Get MSSQL CT summary
    print("[2/3] Querying MSSQL Change Tracking...")
    ct_reader = MSSQLCTReader()
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
            print("\n⚠️  DISCREPANCY DETECTED!")
            print("\nDiscrepancies:")
            for disc in comparison.get('discrepancies', []):
                print(f"  - {disc}")
        
        print()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Generate replication comparison report")
    parser.add_argument("--source", required=True, help="AS400 table (LIBRARY.TABLE)")
    parser.add_argument("--target", required=True, help="MSSQL table (SCHEMA.TABLE)")
    parser.add_argument("--since", help="Filter since timestamp (YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    
    args = parser.parse_args()
    
    generate_report(
        source_table=args.source,
        target_table=args.target,
        since=args.since,
        output_format=args.format
    )
