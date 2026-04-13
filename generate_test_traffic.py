#!/usr/bin/env python3
"""
Test traffic generator for replica-mon testing.
Generates INSERT, UPDATE, DELETE operations on AS400 tables.
"""

import subprocess
import json
import time
import sys
import random
from datetime import datetime

# Configuration
QADMCLI_PATH = "../qadmcli/qadmcli.sh"
CONFIG_FILE = "config.json"

TABLES = [
    {"library": "GSLIBTST", "table": "ORDERS", "pk_column": "ORDER_ID"},
    {"library": "GSLIBTST", "table": "CUSTOMERS", "pk_column": "CUST_ID"},
    {"library": "GSLIBTST", "table": "CUSTOMERS2", "pk_column": "CUST_ID"},
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

def generate_inserts(library, table, pk_column, count=10):
    """Generate INSERT operations."""
    print(f"\n  📝 Inserting {count} records into {library}.{table}...")
    
    for i in range(count):
        # Generate unique PK
        pk_value = int(time.time() * 1000) + i
        
        # Build INSERT statement based on table
        if table == "ORDERS":
            sql = f"""
                INSERT INTO {library}.{table} 
                ({pk_column}, ORDER_DATE, CUSTOMER_ID, AMOUNT, STATUS)
                VALUES ({pk_value}, CURRENT_TIMESTAMP, {random.randint(1000, 9999)}, {random.randint(100, 9999)}, 'ACTIVE')
            """
        elif table == "CUSTOMERS" or table == "CUSTOMERS2":
            sql = f"""
                INSERT INTO {library}.{table} 
                ({pk_column}, NAME, EMAIL, CREATED_DATE)
                VALUES ({pk_value}, 'TestUser{pk_value}', 'test{pk_value}@example.com', CURRENT_TIMESTAMP)
            """
        else:
            print(f"  ⚠️  Unknown table schema: {table}")
            return 0
        
        # Execute via qadmcli
        result = run_qadmcli([
            "sql", "execute",
            "-q", sql.strip()
        ])
        
        if result:
            print(f"    ✓ Inserted record {pk_value}")
        else:
            print(f"    ✗ Failed to insert record {pk_value}")
            return i  # Return count of successful inserts
        
        time.sleep(0.1)  # Small delay between inserts
    
    return count

def generate_updates(library, table, pk_column, count=5):
    """Generate UPDATE operations."""
    print(f"\n  ✏️  Updating {count} records in {library}.{table}...")
    
    # First, get some existing records to update
    select_sql = f"""
        SELECT {pk_column} 
        FROM {library}.{table} 
        ORDER BY {pk_column} DESC 
        FETCH FIRST {count} ROWS ONLY
    """
    
    result = run_qadmcli([
        "sql", "execute",
        "-q", select_sql.strip(),
        "--format", "json"
    ])
    
    if not result or not isinstance(result, list) or len(result) == 0:
        print(f"  ⚠️  No records found to update")
        return 0
    
    pks = [row[pk_column] for row in result]
    
    updated = 0
    for pk in pks:
        if table == "ORDERS":
            sql = f"""
                UPDATE {library}.{table} 
                SET AMOUNT = {random.randint(100, 9999)}, STATUS = 'UPDATED'
                WHERE {pk_column} = {pk}
            """
        else:  # CUSTOMERS or CUSTOMERS2
            sql = f"""
                UPDATE {library}.{table} 
                SET NAME = 'Updated{pk}', EMAIL = 'updated{pk}@example.com'
                WHERE {pk_column} = {pk}
            """
        
        result = run_qadmcli([
            "sql", "execute",
            "-q", sql.strip()
        ])
        
        if result:
            print(f"    ✓ Updated record {pk}")
            updated += 1
        else:
            print(f"    ✗ Failed to update record {pk}")
        
        time.sleep(0.1)
    
    return updated

def generate_deletes(library, table, pk_column, count=3):
    """Generate DELETE operations."""
    print(f"\n  🗑️  Deleting {count} records from {library}.{table}...")
    
    # Get oldest records to delete
    select_sql = f"""
        SELECT {pk_column} 
        FROM {library}.{table} 
        ORDER BY {pk_column} ASC 
        FETCH FIRST {count} ROWS ONLY
    """
    
    result = run_qadmcli([
        "sql", "execute",
        "-q", select_sql.strip(),
        "--format", "json"
    ])
    
    if not result or not isinstance(result, list) or len(result) == 0:
        print(f"  ⚠️  No records found to delete")
        return 0
    
    pks = [row[pk_column] for row in result]
    
    deleted = 0
    for pk in pks:
        sql = f"DELETE FROM {library}.{table} WHERE {pk_column} = {pk}"
        
        result = run_qadmcli([
            "sql", "execute",
            "-q", sql.strip()
        ])
        
        if result:
            print(f"    ✓ Deleted record {pk}")
            deleted += 1
        else:
            print(f"    ✗ Failed to delete record {pk}")
        
        time.sleep(0.1)
    
    return deleted

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
    print("REPLICA-MON TRAFFIC GENERATOR")
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
    
    # Generate traffic for each table
    total_inserts = 0
    total_updates = 0
    total_deletes = 0
    
    for table_info in TABLES:
        library = table_info["library"]
        table = table_info["table"]
        pk_column = table_info["pk_column"]
        
        print(f"\n{'='*80}")
        print(f"📋 Table: {library}.{table}")
        print(f"{'='*80}")
        
        # Show initial count
        initial_count = get_row_count(library, table)
        print(f"Initial row count: {initial_count}")
        
        if operation in ["insert", "all"]:
            inserts = generate_inserts(library, table, pk_column, count)
            total_inserts += inserts
        
        if operation in ["update", "all"]:
            updates = generate_updates(library, table, pk_column, min(count // 2, 5))
            total_updates += updates
        
        if operation in ["delete", "all"]:
            deletes = generate_deletes(library, table, pk_column, min(count // 3, 3))
            total_deletes += deletes
        
        # Show final count
        final_count = get_row_count(library, table)
        print(f"\nFinal row count: {final_count}")
        print(f"Net change: {final_count - initial_count if final_count and initial_count else 'unknown'}")
    
    # Summary
    print(f"\n{'='*80}")
    print("📊 TRAFFIC GENERATION SUMMARY")
    print(f"{'='*80}")
    print(f"Total Inserts:  {total_inserts}")
    print(f"Total Updates:  {total_updates}")
    print(f"Total Deletes:  {total_deletes}")
    print(f"Total Changes:  {total_inserts + total_updates + total_deletes}")
    print(f"{'='*80}\n")
    
    # Wait for replication
    if total_inserts + total_updates + total_deletes > 0:
        wait_time = 30
        print(f"⏳ Waiting {wait_time} seconds for GlueSync to replicate changes...")
        time.sleep(wait_time)
        
        # Run monitor
        run_monitor_check()
    
    print(f"\n✅ Traffic generation completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

if __name__ == "__main__":
    main()
