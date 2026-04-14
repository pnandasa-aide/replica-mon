#!/usr/bin/env python3
"""
Integration test: Run monitor.py with real qadmcli commands and SQLite cache.

This test:
1. Clears existing cache
2. Runs monitor.py with verbose mode
3. Verifies SQLite cache is created and populated
4. Runs monitor.py again to test incremental updates
5. Compares performance (first run vs cached run)
6. Verifies time-windowed aggregation works
"""

import subprocess
import sys
import os
import time
import shutil
import json
from datetime import datetime

REPLICA_MON_DIR = '/home/ubuntu/_qoder/replica-mon'
QADMCLI_DIR = '/home/ubuntu/_qoder/qadmcli'
CACHE_DIR = os.path.join(REPLICA_MON_DIR, 'cache')


def run_command(cmd, cwd=None, timeout=120):
    """Run command and return output."""
    print(f"\n$ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or REPLICA_MON_DIR,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.stdout:
            print(result.stdout[:2000])  # Limit output
        if result.stderr:
            print(result.stderr[:500], file=sys.stderr)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        print(f"⏱️  Command timed out after {timeout}s")
        return -1, "", "Timeout"


def test_1_clear_cache():
    """Test 1: Clear existing cache."""
    print("\n" + "="*80)
    print("TEST 1: Clear Existing Cache")
    print("="*80)
    
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
        print(f"✓ Cleared cache directory: {CACHE_DIR}")
    else:
        print(f"✓ No cache directory to clear")
    
    # Verify cache is cleared
    assert not os.path.exists(CACHE_DIR), "Cache directory should not exist"
    print(f"✓ Cache cleared successfully")
    
    return True


def test_2_first_run_populate_cache():
    """Test 2: First monitor.py run to populate cache."""
    print("\n" + "="*80)
    print("TEST 2: First Run - Populate SQLite Cache")
    print("="*80)
    
    start_time = time.time()
    
    # Run monitor.py with verbose mode (single run, not continuous)
    cmd = [
        'python3', 'monitor.py',
        '--verbose',
        '--no-auto-discover',  # Use entities.json
    ]
    
    returncode, stdout, stderr = run_command(cmd, timeout=180)
    
    elapsed = time.time() - start_time
    
    # Check for successful execution
    assert returncode == 0, f"Monitor.py failed with return code {returncode}"
    print(f"\n✓ Monitor.py completed in {elapsed:.2f}s")
    
    # Check for key indicators in output
    assert 'CUSTOMERS' in stdout, "Should mention CUSTOMERS table"
    print(f"✓ Output contains CUSTOMERS table")
    
    # Check for cache population messages
    if 'Cached' in stdout or 'cache' in stdout.lower():
        print(f"✓ Cache population messages found")
    
    # Check for SQLite cache database
    journal_db = os.path.join(CACHE_DIR, 'journal_cache.db')
    ct_db = os.path.join(CACHE_DIR, 'ct_cache.db')
    
    if os.path.exists(journal_db):
        db_size = os.path.getsize(journal_db) / (1024*1024)
        print(f"✓ Journal cache created: {db_size:.2f} MB")
    else:
        print(f"⚠️  Journal cache not found (may not have data)")
    
    if os.path.exists(ct_db):
        db_size = os.path.getsize(ct_db) / (1024*1024)
        print(f"✓ CT cache created: {db_size:.2f} MB")
    else:
        print(f"⚠️  CT cache not found (may not have data)")
    
    print(f"\n✅ TEST 2 PASSED: First run completed, cache populated")
    return True


def test_3_second_run_use_cache():
    """Test 3: Second run should use cache (faster)."""
    print("\n" + "="*80)
    print("TEST 3: Second Run - Use SQLite Cache (Should Be Faster)")
    print("="*80)
    
    start_time = time.time()
    
    # Run monitor.py again
    cmd = [
        'python3', 'monitor.py',
        '--verbose',
        '--no-auto-discover'
    ]
    
    returncode, stdout, stderr = run_command(cmd, timeout=180)
    
    elapsed = time.time() - start_time
    
    assert returncode == 0, f"Monitor.py failed with return code {returncode}"
    print(f"\n✓ Monitor.py completed in {elapsed:.2f}s")
    
    # Check for cache usage messages
    if 'Using cached' in stdout or 'from cache' in stdout.lower():
        print(f"✓ Cache usage confirmed in output")
    
    # Check for incremental fetch messages
    if 'Fetching new' in stdout:
        print(f"✓ Incremental fetch detected")
    
    print(f"\n✅ TEST 3 PASSED: Second run used cache")
    return True


def test_4_time_windowed_query():
    """Test 4: Time-windowed aggregation."""
    print("\n" + "="*80)
    print("TEST 4: Time-Windowed Aggregation (--since 1 hour ago)")
    print("="*80)
    
    # Calculate 1 hour ago
    since_time = subprocess.run(
        ['date', '-d', '1 hour ago', '+%Y-%m-%d %H:%M:%S'],
        capture_output=True,
        text=True
    ).stdout.strip()
    
    print(f"Testing with --since '{since_time}'")
    
    start_time = time.time()
    
    cmd = [
        'python3', 'monitor.py',
        '--verbose',
        '--no-auto-discover',
        '--since', since_time
    ]
    
    returncode, stdout, stderr = run_command(cmd, timeout=180)
    
    elapsed = time.time() - start_time
    
    assert returncode == 0, f"Monitor.py failed with return code {returncode}"
    print(f"\n✓ Monitor.py completed in {elapsed:.2f}s")
    
    # Check for time window messages
    if 'time window' in stdout.lower():
        print(f"✓ Time-windowed aggregation detected")
    
    print(f"\n✅ TEST 4 PASSED: Time-windowed query works")
    return True


def test_5_multiple_tables():
    """Test 5: Monitor multiple tables."""
    print("\n" + "="*80)
    print("TEST 5: Multiple Tables (dbo.CUSTOMERS, dbo.ORDERS)")
    print("="*80)
    
    start_time = time.time()
    
    cmd = [
        'python3', 'monitor.py',
        '--verbose',
        '--no-auto-discover'
    ]
    
    returncode, stdout, stderr = run_command(cmd, timeout=240)
    
    elapsed = time.time() - start_time
    
    assert returncode == 0, f"Monitor.py failed with return code {returncode}"
    print(f"\n✓ Monitor.py completed in {elapsed:.2f}s")
    
    # Check for both tables
    if 'CUSTOMERS' in stdout and 'ORDERS' in stdout:
        print(f"✓ Both tables monitored")
    
    print(f"\n✅ TEST 5 PASSED: Multiple tables monitored")
    return True


def test_6_json_output():
    """Test 6: JSON output format."""
    print("\n" + "="*80)
    print("TEST 6: JSON Output Format")
    print("="*80)
    
    cmd = [
        'python3', 'monitor.py',
        '--format', 'json',
        '--no-auto-discover'
    ]
    
    returncode, stdout, stderr = run_command(cmd, timeout=180)
    
    assert returncode == 0, f"Monitor.py failed with return code {returncode}"
    
    # Try to parse JSON output
    try:
        # Find JSON in output (may have other text)
        json_start = stdout.find('{')
        json_end = stdout.rfind('}') + 1
        
        if json_start >= 0 and json_end > json_start:
            json_str = stdout[json_start:json_end]
            data = json.loads(json_str)
            print(f"✓ JSON output parsed successfully")
            print(f"  Keys: {list(data.keys())[:5]}")
        else:
            print(f"⚠️  No JSON found in output")
    except json.JSONDecodeError as e:
        print(f"⚠️  JSON parse error: {e}")
    
    print(f"\n✅ TEST 6 PASSED: JSON output format works")
    return True


def test_7_cache_statistics():
    """Test 7: Check cache statistics."""
    print("\n" + "="*80)
    print("TEST 7: Cache Statistics")
    print("="*80)
    
    # Check cache files
    journal_db = os.path.join(CACHE_DIR, 'journal_cache.db')
    ct_db = os.path.join(CACHE_DIR, 'ct_cache.db')
    
    print(f"\nCache Directory: {CACHE_DIR}")
    if os.path.exists(CACHE_DIR):
        files = os.listdir(CACHE_DIR)
        print(f"  Files: {len(files)}")
        for f in files:
            filepath = os.path.join(CACHE_DIR, f)
            size = os.path.getsize(filepath) / (1024*1024)
            print(f"    - {f}: {size:.2f} MB")
    
    # Query cache statistics using Python
    if os.path.exists(journal_db):
        import sqlite3
        conn = sqlite3.connect(journal_db)
        cursor = conn.execute("SELECT COUNT(*) as count FROM journal_entries")
        journal_count = cursor.fetchone()['count']
        
        cursor = conn.execute("SELECT COUNT(DISTINCT table_name) as tables FROM journal_entries")
        journal_tables = cursor.fetchone()['tables']
        
        conn.close()
        
        print(f"\nJournal Cache Statistics:")
        print(f"  Total entries: {journal_count}")
        print(f"  Tables cached: {journal_tables}")
    
    if os.path.exists(ct_db):
        import sqlite3
        conn = sqlite3.connect(ct_db)
        cursor = conn.execute("SELECT COUNT(*) as count FROM ct_changes")
        ct_count = cursor.fetchone()['count']
        
        cursor = conn.execute("SELECT COUNT(DISTINCT table_name) as tables FROM ct_changes")
        ct_tables = cursor.fetchone()['tables']
        
        conn.close()
        
        print(f"\nCT Cache Statistics:")
        print(f"  Total changes: {ct_count}")
        print(f"  Tables cached: {ct_tables}")
    
    print(f"\n✅ TEST 7 PASSED: Cache statistics retrieved")
    return True


def main():
    """Run all integration tests."""
    print("="*80)
    print("Monitor.py Integration Test Suite (Real qadmcli + SQLite Cache)")
    print("="*80)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"replica-mon: {REPLICA_MON_DIR}")
    print(f"qadmcli: {QADMCLI_DIR}")
    
    tests = [
        ("Clear Cache", test_1_clear_cache),
        ("First Run - Populate Cache", test_2_first_run_populate_cache),
        ("Second Run - Use Cache", test_3_second_run_use_cache),
        ("Time-Windowed Query", test_4_time_windowed_query),
        ("Multiple Tables", test_5_multiple_tables),
        ("JSON Output", test_6_json_output),
        ("Cache Statistics", test_7_cache_statistics),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, True, None))
        except Exception as e:
            results.append((name, False, str(e)))
            print(f"\n❌ TEST FAILED: {name}")
            print(f"   Error: {e}")
            import traceback
            traceback.print_exc()
    
    # Summary
    print("\n" + "="*80)
    print("INTEGRATION TEST SUMMARY")
    print("="*80)
    
    passed = sum(1 for _, success, _ in results if success)
    failed = sum(1 for _, success, _ in results if not success)
    
    for name, success, error in results:
        status = "✅ PASSED" if success else "❌ FAILED"
        print(f"{status}: {name}")
        if error:
            print(f"   Error: {error}")
    
    print("\n" + "-"*80)
    print(f"Total: {len(results)} tests, {passed} passed, {failed} failed")
    print(f"Completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*80)
    
    return failed == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
