#!/usr/bin/env python3
"""
Test traffic generator for replica-mon testing.
Uses qadmcli mockup to generate realistic test data.
"""

import subprocess
import json
import time
import sys
from datetime import datetime

# Configuration
QADMCLI_PATH = "../qadmcli/qadmcli.sh"

TABLES = [
    {"library": "GSLIBTST", "table": "ORDERS"},
    {"library": "GSLIBTST", "table": "CUSTOMERS"},
    {"library": "GSLIBTST", "table": "CUSTOMERS2"},
]

def run_qadmcli(args, timeout=30):
    """Run qadmcli command and return output."""
    cmd = [QADMCLI_PATH] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    
    if result.returncode != 0:
        print(f"  ⚠️  Command failed: {' '.join(cmd)}")
        print(f"  Error: {result.stderr[:200] if result.stderr else result.stdout[:200]}")
        return None
    
    # Parse JSON output
    try:
        output = result.stdout.strip()
        # Find JSON in output (skip log messages)
        start_idx = output.find('[')  # JSON arrays start with [
        if start_idx < 0:
            start_idx = output.find('{')  # Or objects start with {
        
        if start_idx >= 0:
            json_str = output[start_idx:]
            return json.loads(json_str)
    except Exception as e:
        print(f"  ⚠️  JSON parse error: {e}")
        pass
    
    return result.stdout

def generate_traffic(library, table, count=10, insert_ratio=50, update_ratio=30, delete_ratio=20):
    """Generate test traffic using qadmcli mockup."""
    print(f"\n  📝 Generating {count} transactions for {library}.{table}...")
    print(f"     Ratios: {insert_ratio}% insert, {update_ratio}% update, {delete_ratio}% delete")
    
    cmd = [
        QADMCLI_PATH,
        "mockup", "generate",
        "-t", table,
        "-l", library,
        "-r", str(count),
        "--insert-ratio", str(insert_ratio),
        "--update-ratio", str(update_ratio),
        "--delete-ratio", str(delete_ratio)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    
    if result.returncode == 0:
        # Parse summary from output
        output = result.stdout
        
        # Look for summary lines
        for line in output.split('\n'):
            if any(keyword in line for keyword in ['INSERT', 'UPDATE', 'DELETE', 'Total', 'transactions']):
                print(f"     {line.strip()}")
        
        return True
    else:
        print(f"  ⚠️  Failed to generate traffic")
        print(f"     Error: {result.stderr[:200] if result.stderr else result.stdout[:200]}")
        return False

def get_row_count(library, table):
    """Get current row count for a table."""
    sql = f"SELECT COUNT(*) AS CNT FROM {library}.{table}"
    
    result = run_qadmcli([
        "sql", "execute",
        "-q", sql.strip(),
        "--format", "json"
    ])
    
    if result and isinstance(result, list) and len(result) > 0:
        return result[0].get('CNT', 0)
    
    return None

def run_monitor_check():
    """Run monitor.py to check replication status."""
    print(f"\n{'='*80}")
    print("📊 Running monitor.py to check replication status...")
    print(f"{'='*80}\n")
    
    result = subprocess.run(
        ["python3", "monitor.py"],
        capture_output=True,
        text=True,
        timeout=180
    )
    
    # Show the results table
    print(result.stdout)
    
    if result.returncode != 0:
        print(f"\n⚠️  Monitor exited with code: {result.returncode}")
        print(f"Error: {result.stderr[:500]}")

def main():
    print("="*80)
    print("REPLICA-MON TRAFFIC GENERATOR (using qadmcli mockup)")
    print("="*80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Parse arguments
    operation = "all"  # default
    count = 10
    
    if len(sys.argv) > 1:
        operation = sys.argv[1]  # insert, update, delete, all, check
    
    if len(sys.argv) > 2:
        count = int(sys.argv[2])
    
    # Set operation ratios based on mode
    if operation == "insert":
        insert_ratio, update_ratio, delete_ratio = 100, 0, 0
    elif operation == "update":
        insert_ratio, update_ratio, delete_ratio = 0, 100, 0
    elif operation == "delete":
        insert_ratio, update_ratio, delete_ratio = 0, 0, 100
    elif operation == "all":
        insert_ratio, update_ratio, delete_ratio = 50, 30, 20
    else:
        print(f"⚠️  Unknown operation: {operation}")
        print("Use: insert, update, delete, or all")
        sys.exit(1)
    
    # Generate traffic for each table
    total_tables = len(TABLES)
    successful_tables = 0
    
    for i, table_info in enumerate(TABLES, 1):
        library = table_info["library"]
        table = table_info["table"]
        
        print(f"\n{'='*80}")
        print(f"📋 Table {i}/{total_tables}: {library}.{table}")
        print(f"{'='*80}")
        
        # Show initial count
        initial_count = get_row_count(library, table)
        print(f"Initial row count: {initial_count}")
        
        # Generate traffic
        success = generate_traffic(
            library, table, count,
            insert_ratio, update_ratio, delete_ratio
        )
        
        if success:
            successful_tables += 1
        
        # Show final count
        final_count = get_row_count(library, table)
        print(f"\nFinal row count: {final_count}")
        if final_count is not None and initial_count is not None:
            print(f"Net change: {final_count - initial_count:+d} rows")
    
    # Summary
    print(f"\n{'='*80}")
    print("📊 TRAFFIC GENERATION SUMMARY")
    print(f"{'='*80}")
    print(f"Tables processed: {successful_tables}/{total_tables}")
    print(f"Transactions per table: {count}")
    print(f"Operation mix: {insert_ratio}% insert, {update_ratio}% update, {delete_ratio}% delete")
    print(f"{'='*80}\n")
    
    # Wait for replication and run monitor
    if successful_tables > 0:
        wait_time = 30
        print(f"⏳ Waiting {wait_time} seconds for GlueSync to replicate changes...")
        time.sleep(wait_time)
        
        # Run monitor
        run_monitor_check()
    
    print(f"\n✅ Traffic generation completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    main()
