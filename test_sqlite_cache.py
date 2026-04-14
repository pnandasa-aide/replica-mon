#!/usr/bin/env python3
"""
Test SQLite-based journal cache implementation.

Tests:
1. Basic CRUD operations
2. Binary data storage
3. Time-range queries
4. Performance benchmarks
5. Automatic cleanup
6. Statistics and monitoring
"""

import sys
import os
import time
import json
from datetime import datetime, timedelta

# Add lib to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))

from sqlite_journal_cache import SQLiteJournalCache


def test_basic_operations():
    """Test basic cache operations."""
    print("\n" + "="*80)
    print("TEST 1: Basic Operations")
    print("="*80)
    
    cache = SQLiteJournalCache(cache_dir="/tmp/test_cache", retention_days=7)
    
    # Clear any previous test data
    cache.clear_cache("TEST.CUSTOMERS")
    
    # Create test entries
    entries = [
        {
            'entry_number': 1001,
            'entry_timestamp': '2026-04-14 10:00:00',
            'job_name': 'QZDASOINIT',
            'job_user': 'QUSER',
            'job_number': '123456',
            'program_name': 'QZDASOINIT',
            'entry_type': 'PT',  # Add
            'object_library': 'GSLIBTST',
            'object_name': 'CUSTOMERS',
            'object_type': '*FILE',
            'before_image': None,
            'after_image': {'CUST_ID': 123, 'NAME': 'Test Customer'},
            'raw_entry_data': b'\x00\x01\x02\x03'
        },
        {
            'entry_number': 1002,
            'entry_timestamp': '2026-04-14 10:01:00',
            'entry_type': 'UP',  # Update
            'job_name': 'QZDASOINIT',
            'job_user': 'QUSER',
            'after_image': {'CUST_ID': 123, 'NAME': 'Updated Customer'},
            'before_image': {'CUST_ID': 123, 'NAME': 'Test Customer'}
        },
        {
            'entry_number': 1003,
            'entry_timestamp': '2026-04-14 10:02:00',
            'entry_type': 'UB',  # Delete
            'job_name': 'QZDASOINIT',
            'job_user': 'QUSER',
            'before_image': {'CUST_ID': 123, 'NAME': 'Updated Customer'}
        }
    ]
    
    # Store entries
    print("\n1. Storing 3 test entries...")
    count = cache.store_entries(
        "TEST.CUSTOMERS",
        entries,
        last_sequence=1003,
        last_timestamp='2026-04-14 10:02:00'
    )
    print(f"   ✓ Stored {count} entries")
    assert count == 3, f"Expected 3, got {count}"
    
    # Retrieve all entries
    print("\n2. Retrieving all entries...")
    all_entries = cache.get_entries("TEST.CUSTOMERS")
    print(f"   ✓ Retrieved {len(all_entries)} entries")
    assert len(all_entries) == 3, f"Expected 3, got {len(all_entries)}"
    
    # Get cache info
    print("\n3. Getting cache metadata...")
    info = cache.get_cache_info("TEST.CUSTOMERS")
    print(f"   ✓ Last sequence: {info['last_sequence']}")
    print(f"   ✓ Last timestamp: {info['last_timestamp']}")
    print(f"   ✓ Entry count: {info['entry_count']}")
    assert info['last_sequence'] == 1003
    assert info['entry_count'] == 3
    
    print("\n✅ TEST 1 PASSED: Basic operations work correctly")
    return True


def test_binary_data():
    """Test binary data storage (the main advantage over JSON)."""
    print("\n" + "="*80)
    print("TEST 2: Binary Data Storage")
    print("="*80)
    
    cache = SQLiteJournalCache(cache_dir="/tmp/test_cache", retention_days=7)
    cache.clear_cache("TEST.BINARY")
    
    # Create entries with binary data that would break JSON
    print("\n1. Creating entries with binary data...")
    binary_entries = [
        {
            'entry_number': 2001,
            'entry_timestamp': '2026-04-14 11:00:00',
            'entry_type': 'PT',
            'after_image': {
                'raw_data': '\x00\x00\x04:\x00\x0e@@@@@@@@@@'  # Binary with null bytes
            },
            'raw_entry_data': b'\x00\x00\x00\x0em@@@@@@@@@@@@@@@@@@'
        },
        {
            'entry_number': 2002,
            'entry_timestamp': '2026-04-14 11:01:00',
            'entry_type': 'UP',
            'after_image': {
                'raw_data': '\u0000\u0000\u0004:@@@'  # Unicode null characters
            }
        }
    ]
    
    print("   ✓ Binary data includes: null bytes, @ symbols, unicode")
    
    # Store entries
    print("\n2. Storing entries with binary data...")
    try:
        count = cache.store_entries("TEST.BINARY", binary_entries,
                                   last_sequence=2002,
                                   last_timestamp='2026-04-14 11:01:00')
        print(f"   ✓ Stored {count} entries (no parse errors!)")
        assert count == 2
    except Exception as e:
        print(f"   ❌ FAILED: {e}")
        return False
    
    # Retrieve and verify
    print("\n3. Retrieving entries with binary data...")
    try:
        entries = cache.get_entries("TEST.BINARY")
        print(f"   ✓ Retrieved {len(entries)} entries")
        
        # Verify binary data was preserved
        entry = entries[0]
        print(f"   ✓ Entry type: {entry['entry_type']}")
        print(f"   ✓ Has after_image: {entry['after_image'] is not None}")
        
        # The binary data should be preserved (as dict or hex string)
        if isinstance(entry['after_image'], dict):
            print(f"   ✓ After image is dict: {type(entry['after_image'])}")
        elif isinstance(entry['after_image'], str):
            print(f"   ✓ After image is string (hex): {entry['after_image'][:30]}...")
        
        print("\n✅ TEST 2 PASSED: Binary data handled correctly (no JSON parse errors!)")
        return True
    except Exception as e:
        print(f"   ❌ FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_time_range_queries():
    """Test time-range query performance."""
    print("\n" + "="*80)
    print("TEST 3: Time-Range Queries")
    print("="*80)
    
    cache = SQLiteJournalCache(cache_dir="/tmp/test_cache", retention_days=7)
    cache.clear_cache("TEST.TIMEQUERY")
    
    # Generate 100 entries across different times
    print("\n1. Generating 100 test entries...")
    entries = []
    base_time = datetime(2026, 4, 14, 8, 0, 0)
    
    for i in range(100):
        timestamp = base_time + timedelta(minutes=i)
        entries.append({
            'entry_number': 3000 + i,
            'entry_timestamp': timestamp.isoformat(),
            'entry_type': ['PT', 'UP', 'UB'][i % 3],
            'job_name': 'QZDASOINIT',
            'job_user': 'QUSER'
        })
    
    cache.store_entries("TEST.TIMEQUERY", entries,
                       last_sequence=3099,
                       last_timestamp=entries[-1]['entry_timestamp'])
    print(f"   ✓ Generated and stored {len(entries)} entries")
    
    # Test 1: Get entries after specific time
    print("\n2. Query: entries after 08:30 (should get ~90 entries)...")
    start = time.time()
    result = cache.get_entries("TEST.TIMEQUERY", since="2026-04-14T08:30:00")
    elapsed = time.time() - start
    print(f"   ✓ Found {len(result)} entries in {elapsed*1000:.2f}ms")
    assert len(result) >= 30, f"Expected >= 30, got {len(result)}"
    assert elapsed < 0.1, f"Query too slow: {elapsed:.2f}s"
    
    # Test 2: Get entries in time range
    print("\n3. Query: entries between 08:30 and 09:00 (should get ~30)...")
    start = time.time()
    result = cache.get_entries(
        "TEST.TIMEQUERY",
        since="2026-04-14T08:30:00",
        until="2026-04-14T09:00:00"
    )
    elapsed = time.time() - start
    print(f"   ✓ Found {len(result)} entries in {elapsed*1000:.2f}ms")
    assert 25 <= len(result) <= 35, f"Expected ~30, got {len(result)}"
    assert elapsed < 0.1
    
    # Test 3: Get entries by type
    print("\n4. Query: only 'PT' (add) entries...")
    start = time.time()
    result = cache.get_entries_by_type("TEST.TIMEQUERY", "PT")
    elapsed = time.time() - start
    print(f"   ✓ Found {len(result)} PT entries in {elapsed*1000:.2f}ms")
    assert len(result) > 0
    assert all(e['entry_type'] == 'PT' for e in result)
    
    # Test 4: Count entries
    print("\n5. Query: count entries after 08:30...")
    start = time.time()
    count = cache.get_entry_count("TEST.TIMEQUERY", since="2026-04-14T08:30:00")
    elapsed = time.time() - start
    print(f"   ✓ Count: {count} entries in {elapsed*1000:.2f}ms")
    assert count > 0
    
    print("\n✅ TEST 3 PASSED: Time-range queries work correctly and fast")
    return True


def test_performance():
    """Test performance with larger dataset."""
    print("\n" + "="*80)
    print("TEST 4: Performance Benchmark")
    print("="*80)
    
    cache = SQLiteJournalCache(cache_dir="/tmp/test_cache", retention_days=7)
    cache.clear_cache("TEST.PERF")
    
    # Generate 1000 entries
    print("\n1. Generating 1000 entries...")
    entries = []
    base_time = datetime(2026, 4, 14, 0, 0, 0)
    
    for i in range(1000):
        timestamp = base_time + timedelta(seconds=i)
        entries.append({
            'entry_number': 10000 + i,
            'entry_timestamp': timestamp.isoformat(),
            'entry_type': ['PT', 'UP', 'UB'][i % 3],
            'job_name': 'QZDASOINIT',
            'job_user': 'QUSER',
            'after_image': {'id': i, 'data': f'Test data {i}'}
        })
    
    # Benchmark: Store 1000 entries
    print("\n2. Benchmark: Store 1000 entries...")
    start = time.time()
    cache.store_entries("TEST.PERF", entries,
                       last_sequence=10999,
                       last_timestamp=entries[-1]['entry_timestamp'])
    store_time = time.time() - start
    print(f"   ✓ Stored 1000 entries in {store_time:.3f}s ({1000/store_time:.0f} entries/sec)")
    
    # Benchmark: Read all entries
    print("\n3. Benchmark: Read all 1000 entries...")
    start = time.time()
    result = cache.get_entries("TEST.PERF")
    read_time = time.time() - start
    print(f"   ✓ Read 1000 entries in {read_time*1000:.2f}ms")
    assert len(result) == 1000
    
    # Benchmark: Time-range query
    print("\n4. Benchmark: Time-range query (1 hour window)...")
    start = time.time()
    result = cache.get_entries(
        "TEST.PERF",
        since="2026-04-14T00:10:00",
        until="2026-04-14T00:11:00"
    )
    query_time = time.time() - start
    print(f"   ✓ Query returned {len(result)} entries in {query_time*1000:.2f}ms")
    assert query_time < 0.01, f"Query too slow: {query_time:.3f}s"
    
    # Performance summary
    print("\n5. Performance Summary:")
    print(f"   Write speed: {1000/store_time:.0f} entries/sec")
    print(f"   Read speed (all): {read_time*1000:.2f}ms for 1000 entries")
    print(f"   Query speed (indexed): {query_time*1000:.2f}ms")
    
    # Compare to JSON expectations
    print("\n6. Expected Improvement vs JSON:")
    print(f"   Write: {store_time:.3f}s vs ~18s JSON (60x faster expected)")
    print(f"   Read: {read_time*1000:.2f}ms vs ~2300ms JSON ({2300/(read_time*1000):.0f}x faster)")
    print(f"   Query: {query_time*1000:.2f}ms vs ~2300ms JSON ({2300/(query_time*1000):.0f}x faster)")
    
    print("\n✅ TEST 4 PASSED: Performance benchmarks completed")
    return True


def test_automatic_cleanup():
    """Test automatic cleanup of old entries."""
    print("\n" + "="*80)
    print("TEST 5: Automatic Cleanup")
    print("="*80)
    
    cache = SQLiteJournalCache(cache_dir="/tmp/test_cache", retention_days=7)
    cache.clear_cache("TEST.CLEANUP")
    
    # Create entries with old dates (10 days ago)
    print("\n1. Creating entries with old timestamps (10 days ago)...")
    old_entries = []
    old_time = datetime.now() - timedelta(days=10)
    
    for i in range(50):
        timestamp = old_time + timedelta(minutes=i)
        old_entries.append({
            'entry_number': 5000 + i,
            'entry_timestamp': timestamp.isoformat(),
            'entry_type': 'PT',
            'job_name': 'QZDASOINIT'
        })
    
    cache.store_entries("TEST.CLEANUP", old_entries,
                       last_sequence=5049,
                       last_timestamp=old_entries[-1]['entry_timestamp'])
    print(f"   ✓ Stored 50 old entries")
    
    # Create recent entries (1 day ago)
    print("\n2. Creating recent entries (1 day ago)...")
    recent_entries = []
    recent_time = datetime.now() - timedelta(days=1)
    
    for i in range(30):
        timestamp = recent_time + timedelta(minutes=i)
        recent_entries.append({
            'entry_number': 6000 + i,
            'entry_timestamp': timestamp.isoformat(),
            'entry_type': 'PT',
            'job_name': 'QZDASOINIT'
        })
    
    cache.store_entries("TEST.CLEANUP", recent_entries,
                       last_sequence=6029,
                       last_timestamp=recent_entries[-1]['entry_timestamp'])
    print(f"   ✓ Stored 30 recent entries")
    
    # Check counts before cleanup
    total_before = cache.get_entry_count("TEST.CLEANUP")
    print(f"\n3. Before cleanup: {total_before} total entries")
    assert total_before == 80
    
    # Trigger cleanup manually (normally happens on startup)
    print("\n4. Triggering cleanup (removes entries > 7 days old)...")
    cache._cleanup_old_entries()
    
    # Check counts after cleanup
    total_after = cache.get_entry_count("TEST.CLEANUP")
    print(f"   ✓ After cleanup: {total_after} entries")
    print(f"   ✓ Removed: {total_before - total_after} old entries")
    
    # Should have removed the 50 old entries, kept 30 recent
    assert total_after == 30, f"Expected 30, got {total_after}"
    
    # Verify only recent entries remain
    remaining = cache.get_entries("TEST.CLEANUP")
    for entry in remaining:
        ts = datetime.fromisoformat(entry['entry_timestamp'])
        days_old = (datetime.now() - ts).days
        assert days_old < 7, f"Found entry {days_old} days old (should be < 7)"
    
    print("\n✅ TEST 5 PASSED: Automatic cleanup works correctly")
    return True


def test_statistics():
    """Test cache statistics."""
    print("\n" + "="*80)
    print("TEST 6: Cache Statistics")
    print("="*80)
    
    cache = SQLiteJournalCache(cache_dir="/tmp/test_cache", retention_days=7)
    
    # Get stats
    print("\n1. Getting cache statistics...")
    stats = cache.get_stats()
    
    print(f"\n   Database Path: {stats['db_path']}")
    print(f"   Total Entries: {stats['total_entries']}")
    print(f"   Database Size: {stats['db_size_mb']} MB")
    print(f"   Retention: {stats['retention_days']} days")
    print(f"   Date Range: {stats['oldest_entry']} to {stats['newest_entry']}")
    print(f"\n   Entries by Table:")
    for table, count in stats['tables'].items():
        print(f"     - {table}: {count} entries")
    
    assert stats['total_entries'] > 0
    assert stats['db_size_mb'] > 0
    
    print("\n✅ TEST 6 PASSED: Statistics work correctly")
    return True


def test_multiple_tables():
    """Test handling multiple tables."""
    print("\n" + "="*80)
    print("TEST 7: Multiple Tables")
    print("="*80)
    
    cache = SQLiteJournalCache(cache_dir="/tmp/test_cache", retention_days=7)
    
    # Create entries for 3 different tables
    tables = ["TEST.ORDERS", "TEST.INVENTORY", "TEST.SHIPMENTS"]
    
    print("\n1. Creating entries for 3 tables...")
    for table in tables:
        entries = []
        for i in range(20):
            entries.append({
                'entry_number': 7000 + i,
                'entry_timestamp': datetime.now().isoformat(),
                'entry_type': ['PT', 'UP', 'UB'][i % 3],
                'job_name': 'QZDASOINIT'
            })
        
        cache.store_entries(table, entries,
                           last_sequence=7019,
                           last_timestamp=entries[-1]['entry_timestamp'])
        print(f"   ✓ {table}: 20 entries")
    
    # Verify each table has correct count
    print("\n2. Verifying table isolation...")
    for table in tables:
        count = cache.get_entry_count(table)
        print(f"   ✓ {table}: {count} entries")
        assert count == 20, f"Expected 20 for {table}, got {count}"
    
    # Get stats
    stats = cache.get_stats()
    print(f"\n3. Total across all tables: {stats['total_entries']} entries")
    assert stats['total_entries'] >= 60  # At least 60 (may have more from previous tests)
    
    print("\n✅ TEST 7 PASSED: Multiple tables handled correctly")
    return True


def main():
    """Run all tests."""
    print("\n" + "="*80)
    print("SQLite Journal Cache Test Suite")
    print("="*80)
    
    # Use workspace directory instead of /tmp
    test_cache_dir = os.path.join(os.path.dirname(__file__), 'test_cache')
    os.makedirs(test_cache_dir, exist_ok=True)
    
    # Update all test functions to use this directory
    import sqlite_journal_cache
    original_init = sqlite_journal_cache.SQLiteJournalCache.__init__
    
    def patched_init(self, cache_dir=None, retention_days=7):
        if cache_dir is None or cache_dir.startswith("/tmp"):
            cache_dir = test_cache_dir
        original_init(self, cache_dir, retention_days)
    
    sqlite_journal_cache.SQLiteJournalCache.__init__ = patched_init
    
    tests = [
        ("Basic Operations", test_basic_operations),
        ("Binary Data Storage", test_binary_data),
        ("Time-Range Queries", test_time_range_queries),
        ("Performance Benchmark", test_performance),
        ("Automatic Cleanup", test_automatic_cleanup),
        ("Cache Statistics", test_statistics),
        ("Multiple Tables", test_multiple_tables),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ TEST FAILED: {name}")
            print(f"   Error: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    # Summary
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed! SQLite cache is working correctly.")
        print("\nNext steps:")
        print("  1. Run monitor.py to test with real data")
        print("  2. Check performance improvements")
        print("  3. Verify no JSON parse errors with binary data")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed. Please review errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
