#!/usr/bin/env python3
"""
Test script for new journal caching with time-windowed aggregation.

This script tests:
1. Initial cache population (fetches all entries)
2. Incremental fetch (resumes from last_sequence)
3. Time-windowed aggregation (counts per interval)
4. Cache performance (should be fast after initial load)
"""

import sys
import os
import time
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from lib.as400_journal import AS400JournalReader

# Configuration
QADMCLI_PATH = "../qadmcli/qadmcli.sh"
TEST_TABLE = "GSLIBTST.CUSTOMERS"

def clear_cache(table):
    """Clear existing cache for fresh test."""
    from lib.journal_cache import JournalCache
    cache = JournalCache()
    cache.clear_cache(table)
    print(f"✓ Cleared cache for {table}")

def test_initial_load(reader, table):
    """Test 1: Initial cache population."""
    print("\n" + "="*80)
    print("TEST 1: Initial Cache Population")
    print("="*80)
    
    start = time.time()
    summary = reader.get_summary(table, use_time_window=False)
    elapsed = time.time() - start
    
    print(f"\nResults:")
    print(f"  Total entries: {summary.get('total', 0)}")
    print(f"  Inserts: {summary.get('inserts', 0)}")
    print(f"  Updates: {summary.get('updates', 0)}")
    print(f"  Deletes: {summary.get('deletes', 0)}")
    print(f"  From cache: {summary.get('from_cache', False)}")
    print(f"  Time taken: {elapsed:.2f} seconds")
    
    # Check cache was created
    from lib.journal_cache import JournalCache
    cache_info = reader.cache.get_cache_info(table)
    print(f"\nCache Info:")
    print(f"  Cached: {cache_info['cached']}")
    print(f"  Entry count: {cache_info['entry_count']}")
    print(f"  Cache level: {cache_info.get('cache_level', 'unknown')}")
    print(f"  Last sequence: {cache_info.get('last_sequence', 0)}")
    
    return summary

def test_incremental_fetch(reader, table):
    """Test 2: Incremental fetch (should be fast)."""
    print("\n" + "="*80)
    print("TEST 2: Incremental Fetch (Resume from Last Sequence)")
    print("="*80)
    
    start = time.time()
    # This should only fetch new entries since last_sequence
    summary = reader.get_summary(table, use_time_window=False)
    elapsed = time.time() - start
    
    print(f"\nResults:")
    print(f"  Total entries: {summary.get('total', 0)}")
    print(f"  From cache: {summary.get('from_cache', False)}")
    print(f"  Time taken: {elapsed:.2f} seconds")
    
    if elapsed < 5:
        print(f"  ✓ FAST! (Incremental fetch working)")
    else:
        print(f"  ⚠️  Slow - may have re-fetched all entries")

def test_time_window_aggregation(reader, table):
    """Test 3: Time-windowed aggregation."""
    print("\n" + "="*80)
    print("TEST 3: Time-Windowed Aggregation")
    print("="*80)
    
    # Use a time 1 hour ago as window start
    from datetime import timedelta
    window_start = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    
    print(f"  Time window: {window_start} to now")
    
    start = time.time()
    summary = reader.get_summary(table, since=window_start, use_time_window=True)
    elapsed = time.time() - start
    
    print(f"\nResults:")
    print(f"  Total entries in window: {summary.get('total', 0)}")
    print(f"  Inserts: {summary.get('inserts', 0)}")
    print(f"  Updates: {summary.get('updates', 0)}")
    print(f"  Deletes: {summary.get('deletes', 0)}")
    print(f"  From cache: {summary.get('from_cache', False)}")
    print(f"  Time taken: {elapsed:.2f} seconds")
    
    if summary.get('from_cache', False) and elapsed < 1:
        print(f"  ✓ FAST! (Time-windowed aggregation working)")
    else:
        print(f"  ⚠️  May not be using cache properly")

def test_multiple_windows(reader, table):
    """Test 4: Multiple time windows (simulate monitoring cycles)."""
    print("\n" + "="*80)
    print("TEST 4: Multiple Time Windows (Simulate Monitoring)")
    print("="*80)
    
    from datetime import timedelta
    
    # Simulate 3 monitoring cycles with 5-minute intervals
    now = datetime.now()
    windows = [
        (now - timedelta(minutes=15), now - timedelta(minutes=10)),
        (now - timedelta(minutes=10), now - timedelta(minutes=5)),
        (now - timedelta(minutes=5), now)
    ]
    
    for i, (start_time, end_time) in enumerate(windows, 1):
        window_start = start_time.strftime("%Y-%m-%d %H:%M:%S")
        window_end = end_time.strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"\n  Window {i}: {window_start} to {window_end}")
        
        start = time.time()
        summary = reader.get_summary(table, since=window_start, use_time_window=True)
        elapsed = time.time() - start
        
        print(f"    Entries: {summary.get('total', 0)} (from cache: {summary.get('from_cache', False)})")
        print(f"    Time: {elapsed:.3f} seconds")

def main():
    print("="*80)
    print("Journal Caching Test Suite")
    print("="*80)
    print(f"Test table: {TEST_TABLE}")
    print(f"qadmcli path: {QADMCLI_PATH}")
    
    # Initialize reader
    reader = AS400JournalReader(qadmcli_path=QADMCLI_PATH, use_cache=True)
    
    # Ask user if they want to clear cache
    print("\nOptions:")
    print("  1. Run tests with existing cache")
    print("  2. Clear cache and run tests (will do initial load)")
    
    choice = input("\nYour choice (1/2): ").strip()
    
    if choice == "2":
        print("\nClearing cache...")
        clear_cache(TEST_TABLE)
    else:
        print("\nUsing existing cache...")
    
    # Run tests
    try:
        test_initial_load(reader, TEST_TABLE)
        test_incremental_fetch(reader, TEST_TABLE)
        test_time_window_aggregation(reader, TEST_TABLE)
        test_multiple_windows(reader, TEST_TABLE)
        
        print("\n" + "="*80)
        print("✅ All tests completed!")
        print("="*80)
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
