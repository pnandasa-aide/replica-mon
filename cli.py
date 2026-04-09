#!/usr/bin/env python3
"""ReplicaMon CLI - Replication monitoring and reconciliation."""

import argparse
import json
import sys
from datetime import datetime

from lib.as400_journal import AS400JournalReader
from lib.mssql_ct import MSSQLCTReader
from lib.gluesync_mapper import GlueSyncMapper
from lib.comparator import ChangeComparator


def cmd_compare(args):
    """Compare source vs target changes."""
    print(f"=== Replication Comparison ===")
    print(f"Pipeline: {args.pipeline}")
    print(f"Entity:   {args.entity}")
    print(f"Since:    {args.since}")
    print()
    
    # 1. Get entity mapping from GlueSync
    print("Step 1: Getting entity mapping from GlueSync...")
    mapper = GlueSyncMapper()
    try:
        mapping = mapper.get_entity_mapping(args.pipeline, args.entity)
        source_table = mapping['source']
        target_table = mapping['target']
        print(f"  Source: {source_table}")
        print(f"  Target: {target_table}")
        print()
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        sys.exit(1)
    
    # 2. Query AS400 journal entries
    print("Step 2: Querying AS400 journal entries...")
    as400 = AS400JournalReader()
    try:
        source_changes = as400.get_changes(
            table=source_table,
            since=args.since
        )
        print(f"  ✓ Found {source_changes['total']} changes:")
        print(f"    - INSERT: {source_changes['inserts']}")
        print(f"    - UPDATE: {source_changes['updates']}")
        print(f"    - DELETE: {source_changes['deletes']}")
        print()
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        source_changes = None
    
    # 3. Query MSSQL CT changes
    print("Step 3: Querying MSSQL Change Tracking...")
    mssql = MSSQLCTReader()
    try:
        target_changes = mssql.get_changes(
            table=target_table,
            since=args.since
        )
        print(f"  ✓ Found {target_changes['total']} changes:")
        print(f"    - INSERT: {target_changes['inserts']}")
        print(f"    - UPDATE: {target_changes['updates']}")
        print(f"    - DELETE: {target_changes['deletes']}")
        print()
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        print("  Note: CT may not be enabled on target table")
        target_changes = None
    
    # 4. Compare and report
    print("Step 4: Comparing changes...")
    if source_changes and target_changes:
        comparator = ChangeComparator()
        result = comparator.compare(source_changes, target_changes)
        
        print(f"\n=== Comparison Result ===")
        print(f"Source changes: {source_changes['total']}")
        print(f"Target changes: {target_changes['total']}")
        print(f"Difference: {result['difference']}")
        
        if result['discrepancies']:
            print(f"\n⚠ Discrepancies found:")
            for disc in result['discrepancies']:
                print(f"  - {disc}")
        else:
            print(f"\n✓ No discrepancies found")
    else:
        print("\n⚠ Cannot compare - missing data from source or target")
    
    # Output JSON if requested
    if args.json:
        output = {
            'pipeline': args.pipeline,
            'entity': args.entity,
            'since': args.since,
            'source': source_changes,
            'target': target_changes,
            'comparison': result if source_changes and target_changes else None
        }
        print(f"\n=== JSON Output ===")
        print(json.dumps(output, indent=2, default=str))


def cmd_reconcile(args):
    """Reconcile specific primary key."""
    print(f"=== PK Reconciliation ===")
    print(f"Pipeline: {args.pipeline}")
    print(f"Entity:   {args.entity}")
    print(f"PK:       {args.pk}")
    print()
    
    # Get entity mapping
    mapper = GlueSyncMapper()
    try:
        mapping = mapper.get_entity_mapping(args.pipeline, args.entity)
        source_table = mapping['source']
        target_table = mapping['target']
        pk_column = mapping.get('pk_column', 'ID')
        print(f"Source: {source_table}")
        print(f"Target: {target_table}")
        print(f"PK Column: {pk_column}")
        print()
    except Exception as e:
        print(f"✗ Failed to get mapping: {e}")
        sys.exit(1)
    
    # Query source
    print(f"Querying source for PK {args.pk}...")
    as400 = AS400JournalReader()
    try:
        source_record = as400.get_record(source_table, pk_column, args.pk)
        if source_record:
            print(f"  ✓ Found in source")
        else:
            print(f"  ✗ Not found in source")
    except Exception as e:
        print(f"  ✗ Error: {e}")
        source_record = None
    
    # Query target
    print(f"Querying target for PK {args.pk}...")
    mssql = MSSQLCTReader()
    try:
        target_record = mssql.get_record(target_table, pk_column, args.pk)
        if target_record:
            print(f"  ✓ Found in target")
        else:
            print(f"  ✗ Not found in target")
    except Exception as e:
        print(f"  ✗ Error: {e}")
        target_record = None
    
    # Compare records
    if source_record and target_record:
        print(f"\n=== Record Comparison ===")
        comparator = ChangeComparator()
        diff = comparator.compare_records(source_record, target_record)
        if diff:
            print(f"Differences found:")
            for d in diff:
                print(f"  - {d}")
        else:
            print(f"✓ Records match")
    elif source_record and not target_record:
        print(f"\n⚠ Record exists in source but NOT in target")
        print(f"   Replication lag or missing sync")
    elif not source_record and target_record:
        print(f"\n⚠ Record exists in target but NOT in source")
        print(f"   May have been deleted from source")
    else:
        print(f"\n✗ Record not found in either source or target")


def main():
    parser = argparse.ArgumentParser(
        description="ReplicaMon - Replication Monitoring & Reconciliation"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # Compare command
    compare_parser = subparsers.add_parser(
        "compare", 
        help="Compare source vs target changes"
    )
    compare_parser.add_argument(
        "--pipeline", "-p", 
        required=True, 
        help="Pipeline ID"
    )
    compare_parser.add_argument(
        "--entity", "-e", 
        required=True, 
        help="Entity ID"
    )
    compare_parser.add_argument(
        "--since", "-s", 
        required=True, 
        help="Start timestamp (YYYY-MM-DD HH:MM:SS)"
    )
    compare_parser.add_argument(
        "--json", 
        action="store_true",
        help="Output results as JSON"
    )
    
    # Reconcile command
    reconcile_parser = subparsers.add_parser(
        "reconcile", 
        help="Reconcile specific PK"
    )
    reconcile_parser.add_argument(
        "--pipeline", "-p", 
        required=True, 
        help="Pipeline ID"
    )
    reconcile_parser.add_argument(
        "--entity", "-e", 
        required=True, 
        help="Entity ID"
    )
    reconcile_parser.add_argument(
        "--pk", 
        required=True, 
        help="Primary key value to reconcile"
    )
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    if args.command == "compare":
        cmd_compare(args)
    elif args.command == "reconcile":
        cmd_reconcile(args)


if __name__ == "__main__":
    main()
