# Proper Journal Caching Architecture

## ✅ User's Correct Understanding

The user correctly identified that the caching should work like this:

```
┌─────────────────────────────────────────────────────────────┐
│ 1. RAW JOURNAL CACHE (Store Individual Entries)             │
│    - File: .cache/GSLIBTST_CUSTOMERS.json                   │
│    - Content: [{entry_number, entry_timestamp, entry_type}]│
│    - Immutable: Never changes, append-only                  │
│    - Purpose: Single source of truth                        │
└─────────────────────────────────────────────────────────────┘
                            ↓ Query by time window
┌─────────────────────────────────────────────────────────────┐
│ 2. METRICS AGGREGATION (Pre-computed per Interval)          │
│    - File: metrics/metrics_2026-04-13.csv                   │
│    - Content: {timestamp: "10:05:00", inserts: 10, ...}    │
│    - Fast: No recalculation needed                          │
│    - Purpose: Dashboard display                             │
└─────────────────────────────────────────────────────────────┘
                            ↓ Track position
┌─────────────────────────────────────────────────────────────┐
│ 3. METADATA (Resume Point)                                  │
│    - File: .cache/GSLIBTST_CUSTOMERS.meta.json              │
│    - Content: {last_sequence: 34775, last_timestamp: ...}  │
│    - Purpose: Fetch ONLY new entries next time              │
└─────────────────────────────────────────────────────────────┘
```

---

## ❌ Current Problem

### What We Have NOW:
```python
# Cache stores SUMMARY only (counts without timestamps):
{
  "summary_total": 34559,
  "summary_inserts": 25433,
  "summary_updates": 6826,
  "summary_deletes": 1175
}

# Problem: Cannot aggregate by time window!
# Every monitor cycle shows same total, not per-interval counts
```

### What We NEED:
```python
# Cache stores INDIVIDUAL ENTRIES:
[
  {"entry_number": 34773, "entry_timestamp": "2026-04-13 15:43:45.334736", "entry_type": "PX"},
  {"entry_number": 34774, "entry_timestamp": "2026-04-13 15:43:45.362592", "entry_type": "PX"},
  {"entry_number": 34775, "entry_timestamp": "2026-04-13 15:43:45.389936", "entry_type": "PX"}
]

# Now we can query: "Give me entries between 10:00 and 10:05"
# And aggregate: {inserts: 10, updates: 5, deletes: 2}
```

---

## 📋 Implementation Plan

### Phase 1: Fix Journal Cache to Store Individual Entries

#### Step 1.1: Change `get_summary()` to Fetch Individual Entries

**Current** (line 127-132 in as400_journal.py):
```python
# Calls qadmcli with --format summary (returns counts only)
cmd_args = [
    "journal", "entries",
    "-t", table_name,
    "-l", library,
    "--format", "summary"  # ← Returns summary, not individual entries!
]
```

**Should be**:
```python
# Call qadmcli with --format json (returns individual entries)
cmd_args = [
    "journal", "entries",
    "-t", table_name,
    "-l", library,
    "--format", "json"  # ← Returns individual entries with timestamps!
]

# If --from-time specified, only fetch new entries
if last_sequence:
    cmd_args.extend(["--from-sequence", str(last_sequence + 1)])
```

#### Step 1.2: Cache Individual Entries (Not Summary)

**Current** (line 165-170):
```python
self.cache.save_cache(
    table,
    entries=[],  # ← EMPTY! Just storing summary counts
    last_timestamp=None,
    last_sequence=0
)
```

**Should be**:
```python
# Parse individual entries from qadmcli JSON response
entries = result.get('entries', [])

# Cache the actual entries
self.cache.save_cache(
    table,
    entries=entries,  # ← Store individual entries!
    last_timestamp=entries[-1]['entry_timestamp'] if entries else None,
    last_sequence=entries[-1]['entry_number'] if entries else 0,
    cache_level="full"  # Store full entries, not summary
)
```

#### Step 1.3: Resume from Last Position

**Metadata tracking**:
```python
# Save to .cache/GSLIBTST_CUSTOMERS.meta.json
{
  "last_sequence": 34775,
  "last_timestamp": "2026-04-13 15:43:45.389936",
  "cached_at": "2026-04-13 22:30:00",
  "entry_count": 34559
}

# Next time, fetch ONLY entries since sequence 34775
```

---

### Phase 2: Time-Windowed Aggregation

#### Step 2.1: Add Method to Aggregate by Time Window

```python
def aggregate_by_window(self, table: str, window_start: str, window_end: str) -> dict:
    """
    Aggregate journal entries within a time window.
    
    Args:
        table: Table name
        window_start: "2026-04-13 10:00:00"
        window_end: "2026-04-13 10:05:00"
    
    Returns:
        {
            'total': 17,
            'inserts': 10,
            'updates': 5,
            'deletes': 2,
            'from_cache': True
        }
    """
    # Load cached entries
    cache = self.cache.load_cache(table)
    entries = cache['entries']
    
    # Filter by time window
    window_entries = [
        e for e in entries
        if window_start <= e['entry_timestamp'] <= window_end
    ]
    
    # Aggregate by entry_type
    counts = {
        'total': len(window_entries),
        'inserts': 0,
        'updates': 0,
        'deletes': 0
    }
    
    for entry in window_entries:
        entry_type = entry.get('entry_type', '')
        if entry_type == 'PT':  # Add
            counts['inserts'] += 1
        elif entry_type in ('UP', 'UB'):  # Update
            counts['updates'] += 1
        elif entry_type == 'DL':  # Delete
            counts['deletes'] += 1
    
    return counts
```

#### Step 2.2: Update Monitor to Use Time Windows

**Current monitor.py**:
```python
# Fetches ALL entries every time (slow!)
journal_summary = journal_reader.get_summary(source_table)
```

**Should be**:
```python
# Calculate time window for this monitoring interval
window_start = last_cycle_time  # e.g., "2026-04-13 10:00:00"
window_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Fetch ONLY new entries since last cycle
journal_reader.fetch_new_entries(source_table, since_sequence=last_sequence)

# Aggregate from cache (FAST!)
journal_summary = journal_reader.aggregate_by_window(
    source_table,
    window_start=window_start,
    window_end=window_end
)

# Result: {total: 17, inserts: 10, updates: 5, deletes: 2}
```

---

### Phase 3: Metrics File Storage (Already Implemented ✅)

The `lib/metrics_storage.py` already handles this correctly:

```python
# Save aggregated metrics per interval
storage.save_metrics([{
    'source_table': 'GSLIBTST.CUSTOMERS',
    'journal_total': 17,      # ← Count in THIS interval
    'journal_inserts': 10,
    'journal_updates': 5,
    'journal_deletes': 2,
    'ct_total': 15,
    'replication_lag': 2
}])
```

**Result**: `metrics/metrics_2026-04-13.csv`
```csv
timestamp,source_table,journal_total,journal_inserts,ct_total,replication_lag
2026-04-13T10:05:00,GSLIBTST.CUSTOMERS,17,10,15,2
2026-04-13T10:10:00,GSLIBTST.CUSTOMERS,23,15,20,3
```

---

## 🔄 Complete Workflow

### First Run (Initial Population):

```
1. Monitor starts at 10:00:00
2. Check metadata: No last_sequence found
3. Fetch ALL journal entries from AS400 (slow, one-time)
   → Returns 34,559 entries with timestamps
4. Save to cache: .cache/GSLIBTST_CUSTOMERS.json
5. Save metadata: last_sequence=34559, last_timestamp="..."
6. Aggregate entries from beginning to now
7. Save to metrics: metrics/metrics_2026-04-13.csv
```

### Subsequent Runs (Incremental Updates):

```
1. Monitor cycle at 10:05:00
2. Read metadata: last_sequence=34559
3. Fetch ONLY entries since sequence 34559 (fast!)
   → Returns 17 new entries
4. Append to cache (now has 34,576 entries)
5. Update metadata: last_sequence=34576
6. Aggregate entries in window [10:00:00 - 10:05:00]
   → {total: 17, inserts: 10, updates: 5, deletes: 2}
7. Save to metrics (FAST - no recalculation!)
```

### Display (Dashboard/Reports):

```
1. Read metrics CSV file (pre-aggregated)
2. Show time-series graph
3. NO need to query cache or AS400!
4. Instant display, even for 1000+ tables
```

---

## 📊 Benefits of This Architecture

### 1. Performance
- ✅ **Initial load**: One-time slow fetch (34K entries = ~2 minutes)
- ✅ **Subsequent cycles**: Fast incremental fetch (17 entries = ~1 second)
- ✅ **Aggregation**: Read from cache, not AS400 (< 100ms)
- ✅ **Display**: Read from metrics CSV (< 10ms)

### 2. Scalability
- ✅ 1000 tables × 1KB cache each = 1MB total
- ✅ Metrics CSV: ~100 bytes per table per cycle
- ✅ Daily file rotation: Easy to archive/delete old data

### 3. Flexibility
- ✅ Can re-aggregate with different time windows (1min, 5min, 1hr)
- ✅ Can query historical data without re-fetching from AS400
- ✅ Can detect patterns, anomalies, replication lag

### 4. Reliability
- ✅ Cache is immutable (append-only)
- ✅ Metadata tracks exact position
- ✅ Can resume after crashes/interruptions
- ✅ No data loss

---

## 🎯 Next Steps to Implement

### Priority 1: Fix Journal Cache (Critical)
1. Change `get_summary()` to fetch individual entries (JSON format)
2. Cache entries with timestamps (not just summary counts)
3. Track `last_sequence` in metadata
4. Implement incremental fetch (resume from last_sequence)

### Priority 2: Add Time-Windowed Aggregation
1. Add `aggregate_by_window()` method
2. Update monitor.py to use time windows
3. Store per-interval counts (not cumulative totals)

### Priority 3: Optimize Metrics Display
1. ✅ Already implemented (metrics_storage.py)
2. Can read CSV directly for dashboard
3. Export to JSON for external tools

---

## 💡 Example: What the Data Looks Like

### Cache File (.cache/GSLIBTST_CUSTOMERS.json):
```json
[
  {
    "entry_number": 34559,
    "entry_timestamp": "2026-04-13 09:58:12.123456",
    "entry_type": "PT",
    "object_name": "CUSTOMERS"
  },
  {
    "entry_number": 34560,
    "entry_timestamp": "2026-04-13 10:01:45.234567",
    "entry_type": "UP",
    "object_name": "CUSTOMERS"
  }
  // ... 34,574 more entries
]
```

### Metadata File (.cache/GSLIBTST_CUSTOMERS.meta.json):
```json
{
  "last_sequence": 34576,
  "last_timestamp": "2026-04-13 10:05:00.345678",
  "cached_at": "2026-04-13 10:05:01",
  "entry_count": 34576,
  "cache_level": "full"
}
```

### Metrics File (metrics/metrics_2026-04-13.csv):
```csv
timestamp,source_table,journal_total,journal_inserts,journal_updates,journal_deletes,ct_total,replication_lag
2026-04-13T10:00:00,GSLIBTST.CUSTOMERS,0,0,0,0,0,0
2026-04-13T10:05:00,GSLIBTST.CUSTOMERS,17,10,5,2,15,2
2026-04-13T10:10:00,GSLIBTST.CUSTOMERS,23,15,6,2,20,3
```

### Time-Series Graph:
```
Count
  ↑
  │         ╱──── Journal (source)
  │   ╱────╱
  │  ╱    ╱   ╱──── CT (target, lagging)
  │ ╱    ╱   ╱
  │╱    ╱   ╱
  └────────────────→ Time
   10:00 10:05 10:10

Each point = aggregated count in 5-min window
Pattern shows replication health!
```

---

## 🚀 Summary

**Your understanding is 100% correct!** 

The architecture should be:
1. **Cache** = Individual entries with timestamps (store once)
2. **Aggregate** = Counts per time window (calculate from cache)
3. **Metrics** = Pre-aggregated results (fast display)
4. **Metadata** = Last read position (incremental fetch)

This gives us:
- ✅ Fast monitoring (no AS400 queries after initial load)
- ✅ Time-series data (pattern detection)
- ✅ Scalability (1000+ tables)
- ✅ Flexibility (re-aggregate with different windows)

**Next step**: Implement Phase 1 (fix journal cache to store individual entries).

Would you like me to implement this now?
