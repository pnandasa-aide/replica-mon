#!/usr/bin/env python3
"""
Test per-entity tracking with sample data.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))

from sqlite_journal_cache import SQLiteJournalCache

# Initialize cache
cache_dir = '/home/ubuntu/_qoder/replica-mon/cache'
cache = SQLiteJournalCache(cache_dir, retention_days=7)

print("=" * 80)
print("POPULATING SAMPLE DATA FOR PER-ENTITY TRACKING TEST")
print("=" * 80)

# Sample journal entries for different tables
sample_entries = [
    # CUSTOMERS entries
    {
        'table_name': 'CUSTOMERS',
        'entry_number': 34001,
        'entry_timestamp': '2026-04-14 08:00:00',
        'entry_type': 'IR',  # Insert
        'object_library': 'GSLIBTST',
        'object_name': 'CUSTOMERS',
        'object_type': '*FILE',
        'job_name': 'QZDASOINIT',
        'job_user': 'QUSER',
        'job_number': '123456',
        'program_name': 'QSYS/QSQROUTE',
        'before_image': None,
        'after_image': b'customer data 1',
        'raw_entry_data': b'raw data 1'
    },
    {
        'table_name': 'CUSTOMERS',
        'entry_number': 34500,
        'entry_timestamp': '2026-04-14 08:30:00',
        'entry_type': 'UP',  # Update
        'object_library': 'GSLIBTST',
        'object_name': 'CUSTOMERS',
        'object_type': '*FILE',
        'job_name': 'QZDASOINIT',
        'job_user': 'QUSER',
        'job_number': '123456',
        'program_name': 'QSYS/QSQROUTE',
        'before_image': b'old data',
        'after_image': b'new data',
        'raw_entry_data': b'raw data 2'
    },
    {
        'table_name': 'CUSTOMERS',
        'entry_number': 35036,
        'entry_timestamp': '2026-04-14 09:30:15',
        'entry_type': 'IR',
        'object_library': 'GSLIBTST',
        'object_name': 'CUSTOMERS',
        'object_type': '*FILE',
        'job_name': 'QZDASOINIT',
        'job_user': 'QUSER',
        'job_number': '123456',
        'program_name': 'QSYS/QSQROUTE',
        'before_image': None,
        'after_image': b'customer data 3',
        'raw_entry_data': b'raw data 3'
    },
    
    # ORDERS entries
    {
        'table_name': 'ORDERS',
        'entry_number': 34001,
        'entry_timestamp': '2026-04-14 08:05:00',
        'entry_type': 'IR',
        'object_library': 'GSLIBTST',
        'object_name': 'ORDERS',
        'object_type': '*FILE',
        'job_name': 'QZDASOINIT',
        'job_user': 'QUSER',
        'job_number': '123456',
        'program_name': 'QSYS/QSQROUTE',
        'before_image': None,
        'after_image': b'order data 1',
        'raw_entry_data': b'raw data 4'
    },
    {
        'table_name': 'ORDERS',
        'entry_number': 34800,
        'entry_timestamp': '2026-04-14 09:00:00',
        'entry_type': 'UP',
        'object_library': 'GSLIBTST',
        'object_name': 'ORDERS',
        'object_type': '*FILE',
        'job_name': 'QZDASOINIT',
        'job_user': 'QUSER',
        'job_number': '123456',
        'program_name': 'QSYS/QSQROUTE',
        'before_image': b'old order',
        'after_image': b'new order',
        'raw_entry_data': b'raw data 5'
    },
    {
        'table_name': 'ORDERS',
        'entry_number': 35037,
        'entry_timestamp': '2026-04-14 09:30:20',
        'entry_type': 'DL',  # Delete
        'object_library': 'GSLIBTST',
        'object_name': 'ORDERS',
        'object_type': '*FILE',
        'job_name': 'QZDASOINIT',
        'job_user': 'QUSER',
        'job_number': '123456',
        'program_name': 'QSYS/QSQROUTE',
        'before_image': b'order to delete',
        'after_image': None,
        'raw_entry_data': b'raw data 6'
    },
    
    # PRODUCTS entries
    {
        'table_name': 'PRODUCTS',
        'entry_number': 34567,
        'entry_timestamp': '2026-04-14 08:15:00',
        'entry_type': 'IR',
        'object_library': 'GSLIBTST',
        'object_name': 'PRODUCTS',
        'object_type': '*FILE',
        'job_name': 'QZDASOINIT',
        'job_user': 'QUSER',
        'job_number': '123456',
        'program_name': 'QSYS/QSQROUTE',
        'before_image': None,
        'after_image': b'product data 1',
        'raw_entry_data': b'raw data 7'
    },
    {
        'table_name': 'PRODUCTS',
        'entry_number': 35032,
        'entry_timestamp': '2026-04-14 09:29:45',
        'entry_type': 'UP',
        'object_library': 'GSLIBTST',
        'object_name': 'PRODUCTS',
        'object_type': '*FILE',
        'job_name': 'QZDASOINIT',
        'job_user': 'QUSER',
        'job_number': '123456',
        'program_name': 'QSYS/QSQROUTE',
        'before_image': b'old product',
        'after_image': b'new product',
        'raw_entry_data': b'raw data 8'
    },
]

# Store entries
print(f"\nInserting {len(sample_entries)} sample journal entries...")
cache.store_entries('GSLIBTST', sample_entries)

print("✅ Sample data inserted!")

# Now show per-entity report
print("\n" + "=" * 80)
print("PER-ENTITY TRACKING REPORT")
print("=" * 80)

from per_entity_tracker import PerEntityTracker

tracker = PerEntityTracker(cache_dir)
print(tracker.format_per_entity_report("GSLIBTST"))

print("\n" + "=" * 80)
print("✅ Test complete!")
print("=" * 80)
