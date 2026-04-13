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
        
        # Skip log lines and find JSON
        lines = output.split('\n')
        json_lines = []
        in_json = False
        
        for line in lines:
            stripped = line.strip()
            
            # Skip empty lines
            if not stripped:
                continue
            
            # Skip ANSI-colored log lines (contain INFO, WARNING, ERROR with timestamps)
            if 'INFO' in stripped or 'WARNING' in stripped or 'ERROR' in stripped:
                continue
            
            # Skip emoji lines
            if stripped.startswith('📦') or stripped.startswith('🚀'):
                continue
            
            # Check if this looks like JSON
            if stripped.startswith('[') or stripped.startswith('{') or in_json:
                in_json = True
                json_lines.append(stripped)
                
                # Check if JSON has ended by counting brackets across all lines
                full_json = '\n'.join(json_lines)
                if full_json.count('[') == full_json.count(']') and full_json.count('{') == full_json.count('}'):
                    if full_json.count('[') > 0 or full_json.count('{') > 0:
                        break
        
        json_str = '\n'.join(json_lines).strip()
        
        if not json_str:
            return None
        
        # Try to parse as JSON
        return json.loads(json_str)
            
    except json.JSONDecodeError as e:
        # JSON parsing failed - return None
        return None
    except Exception as e:
        print(f"  ⚠️  Unexpected error: {e}")
        return None

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
    # Handle --help flag
    if "--help" in sys.argv or "-h" in sys.argv:
        print("="*80)
        print("REPLICA-MON TRAFFIC GENERATOR")
        print("="*80)
        print()
        print("Usage:")
        print("  python3 generate_test_traffic.py [operation] [count]")
        print()
        print("Operations:")
        print("  all      Generate mixed operations (50% insert, 30% update, 20% delete)")
        print("  insert   Generate INSERT operations only")
        print("  update   Generate UPDATE operations only")
        print("  delete   Generate DELETE operations only")
        print()
        print("Examples:")
        print("  python3 generate_test_traffic.py              # 10 transactions, mixed")
        print("  python3 generate_test_traffic.py all 50       # 50 transactions, mixed")
        print("  python3 generate_test_traffic.py insert 20    # 20 inserts only")
        print("  python3 generate_test_traffic.py update 10    # 10 updates only")
        print()
        print("This script uses qadmcli mockup to generate realistic test data")
        print("with automatic schema detection and intelligent field patterns.")
        print()
        return
    
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
