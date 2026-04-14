# Journal & CT Cache Design - Updated Architecture

## 📋 Current Issues (Before Fix)

### AS400 Journal Cache Issues:
1. ❌ Aggregated from cache BEFORE fetching new entries
2. ❌ Used non-existent `--from-sequence` option
3. ❌ Cache never updated with new entries

### MSSQL CT Cache Issues:
1. ❌ Returns from cache immediately without fetching new data
2. ❌ No incremental update logic (always full query)
3. ❌ Doesn't track last version for resumption
4. ❌ Only caches summary, not individual changes

---

## ✅ Corrected Design

### Core Principle: **Fetch First, Then Aggregate**

Both Journal and CT caches must follow this pattern:

```
1. Fetch NEW entries from source (incremental update)
2. Update cache with new entries
3. Aggregate from cache (if time window specified)
4. Return aggregated result
```

**WRONG** (old approach):
```python
if cache exists:
    return aggregate_from_cache()  # ← Stale data!
fetch_from_source()
```

**CORRECT** (new approach):
```python
fetch_result = fetch_from_source()  # ← Always fetch first!
if cache exists:
    update_cache(fetch_result)
    return aggregate_from_cache()  # ← Now has fresh data!
return fetch_result
```

---

## 🔄 AS400 Journal Cache Flow

### Incremental Fetch Strategy:

**Metadata Tracking**:
```json
{
  "last_sequence": 35037,
  "last_timestamp": "2026-04-14 01:34:22.526272",
  "cached_at": "2026-04-14 08:56:45",
  "entry_count": 200,
  "cache_level": "full"
}
```

**Fetch Command**:
```bash
# First run (no cache):
qadmcli journal entries -t CUSTOMERS -l GSLIBTST --format json

# Subsequent runs (incremental):
qadmcli journal entries -t CUSTOMERS -l GSLIBTST --format json \
  --from-time "2026-04-14 01:34:22.526272"
```

**Note**: qadmcli does NOT support `--from-sequence`, so we use `--from-time`.

**Cache Update**:
```python
# Append new entries to existing cache
new_count = cache.append_entries(
    table,
    new_entries,
    last_timestamp=entries[-1]['entry_timestamp'],
    last_sequence=entries[-1]['entry_number']
)
```

**Time-Windowed Aggregation**:
```python
# After cache is updated, aggregate by time window
entries_in_window = [
    e for e in cached_entries
    if e['entry_timestamp'] >= window_start
]

return {
    'total': len(entries_in_window),
    'inserts': count_type(entries_in_window, 'PT'),
    'updates': count_type(entries_in_window, 'UP', 'UB'),
    'deletes': count_type(entries_in_window, 'DL')
}
```

---

## 🔄 MSSQL CT Cache Flow

### Incremental Fetch Strategy:

**Metadata Tracking**:
```json
{
  "last_version": 12345,
  "last_timestamp": "2026-04-14 01:34:22.526272",
  "cached_at": "2026-04-14 08:56:45",
  "entry_count": 100,
  "cache_level": "full"
}
```

**Fetch Command**:
```bash
# First run (no cache):
qadmcli mssql ct changes -t CUSTOMERS -s dbo --format json

# Subsequent runs (incremental):
qadmcli mssql ct changes -t CUSTOMERS -s dbo --format json \
  --since-version 12345
```

**Note**: CT DOES support `--since-version` (unlike AS400 journal)!

**Cache Update**:
```python
# Append new changes to existing cache
new_count = cache.append_ct_entries(
    table,
    new_changes,
    last_version=changes[-1]['sys_change_version'],
    last_timestamp=changes[-1]['sys_change_timestamp']
)
```

**Time-Windowed Aggregation**:
```python
# After cache is updated, aggregate by time window
changes_in_window = [
    c for c in cached_changes
    if c['sys_change_timestamp'] >= window_start
]

return {
    'total': len(changes_in_window),
    'inserts': count_operation(changes_in_window, 'I'),
    'updates': count_operation(changes_in_window, 'U'),
    'deletes': count_operation(changes_in_window, 'D')
}
```

---

## 📊 Complete Monitoring Flow

### Cycle 1 (First Run):

```
┌─────────────────────────────────────────────────────────┐
│ AS400 Journal                                           │
├─────────────────────────────────────────────────────────┤
│ 1. Check cache: No cache                               │
│ 2. Fetch ALL entries from AS400                        │
│    → Command: journal entries -t CUSTOMERS -l GSLIBTST │
│      --format json                                     │
│    → Returns: 100 entries (seq 34663-34775)            │
│    → Time: ~8 seconds                                  │
│ 3. Save to cache                                       │
│ 4. Metadata: {last_seq: 34775, last_ts: "..."}        │
│ 5. Aggregate all (no time window)                      │
│ 6. Result: Journal: 100                                │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ MSSQL CT                                                │
├─────────────────────────────────────────────────────────┤
│ 1. Check cache: No cache                               │
│ 2. Fetch ALL changes from MSSQL                        │
│    → Command: mssql ct changes -t CUSTOMERS -s dbo     │
│      --format json                                     │
│    → Returns: 95 changes (ver 12300-12395)             │
│    → Time: ~2 seconds                                  │
│ 3. Save to cache                                       │
│ 4. Metadata: {last_ver: 12395, last_ts: "..."}        │
│ 5. Aggregate all (no time window)                      │
│ 6. Result: CT: 95                                      │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Comparison                                              │
├─────────────────────────────────────────────────────────┤
│ Journal: 100  vs  CT: 95  →  Lag: 5                    │
│ Status: ❌ MISMATCH (replication catching up)           │
└─────────────────────────────────────────────────────────┘
```

### Cycle 2 (5 Minutes Later):

```
┌─────────────────────────────────────────────────────────┐
│ AS400 Journal                                           │
├─────────────────────────────────────────────────────────┤
│ 1. Check cache: Exists (100 entries)                   │
│ 2. Fetch NEW entries since last_timestamp              │
│    → Command: journal entries ... --from-time "..."    │
│    → Returns: 17 new entries (seq 34776-34792)         │
│    → Time: ~3 seconds                                  │
│ 3. Append to cache (now 117 total)                     │
│ 4. Metadata: {last_seq: 34792, last_ts: "..."}        │
│ 5. Aggregate in window [T-5min to now]                 │
│    → Filter: entries where timestamp >= window_start   │
│    → Result: {total: 17, inserts: 10, updates: 5, ...} │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ MSSQL CT                                                │
├─────────────────────────────────────────────────────────┤
│ 1. Check cache: Exists (95 changes)                    │
│ 2. Fetch NEW changes since last_version                │
│    → Command: mssql ct changes ... --since-version 12395│
│    → Returns: 15 new changes (ver 12396-12410)         │
│    → Time: ~1 second                                   │
│ 3. Append to cache (now 110 total)                     │
│ 4. Metadata: {last_ver: 12410, last_ts: "..."}        │
│ 5. Aggregate in window [T-5min to now]                 │
│    → Filter: changes where timestamp >= window_start   │
│    → Result: {total: 15, inserts: 9, updates: 5, ...}  │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│ Comparison (Delta for THIS interval)                    │
├─────────────────────────────────────────────────────────┤
│ Journal: 17  vs  CT: 15  →  Lag: 2                     │
│ Status: ❌ MISMATCH (lag decreasing: 5 → 2, healthy!)  │
└─────────────────────────────────────────────────────────┘
```

---

## 🎯 Key Differences: AS400 vs MSSQL

| Feature | AS400 Journal | MSSQL CT |
|---------|---------------|----------|
| **Incremental by** | `--from-time` (timestamp) | `--since-version` (version number) |
| **Sequence tracking** | `entry_number` | `sys_change_version` |
| **Timestamp field** | `entry_timestamp` | `sys_change_timestamp` |
| **Operation codes** | PT, UP, UB, DL | I, U, D |
| **Cache append** | `append_entries()` | `append_ct_entries()` (NEW) |
| **Performance** | ~8s full, ~3s incremental | ~2s full, ~1s incremental |

---

## 📁 Cache File Structure

### AS400 Journal Cache:
```
.cache/
├── GSLIBTST_CUSTOMERS.json          # Individual journal entries
├── GSLIBTST_CUSTOMERS.meta.json     # Metadata with last_sequence/timestamp
├── dbo_CUSTOMERS.json               # CT cache (different prefix)
└── dbo_CUSTOMERS.meta.json          # CT metadata
```

**Journal Entry Format**:
```json
[
  {
    "entry_number": 34775,
    "entry_timestamp": "2026-04-13 15:43:45.389936",
    "entry_type": "PX",
    "object_name": "CUSTOMERS",
    "job_name": "QZDASOINIT"
  }
]
```

**CT Change Format**:
```json
[
  {
    "sys_change_version": 12395,
    "sys_change_operation": "I",
    "sys_change_timestamp": "2026-04-13 15:43:45.389936",
    "CUST_ID": 1042
  }
]
```

---

## 🔧 Implementation Changes Required

### 1. AS400 Journal (✅ Already Fixed):

**File**: `lib/as400_journal.py`

**Changes**:
- ✅ `get_summary()`: Fetch before aggregate
- ✅ `_fetch_from_as400()`: Use `--from-time` with last_timestamp
- ✅ `_aggregate_from_cache()`: Time-windowed aggregation

### 2. MSSQL CT (❌ Needs Fix):

**File**: `lib/mssql_ct.py`

**Required Changes**:
- ❌ `get_summary()`: Fetch before aggregate (same pattern as journal)
- ❌ `_fetch_from_mssql()`: NEW - Fetch and cache individual changes
- ❌ `_aggregate_ct_from_cache()`: NEW - Time-windowed aggregation
- ❌ Use `--since-version` for incremental fetch
- ❌ Update JournalCache to support CT entry appending

### 3. JournalCache (❌ Needs Enhancement):

**File**: `lib/journal_cache.py`

**Required Changes**:
- ❌ `append_ct_entries()`: NEW - Append CT changes to cache
- ❌ `load_ct_cache()`: Return individual changes (not just summary)
- ❌ `get_ct_entries_since()`: Filter CT changes by time window

---

## 📈 Metrics Storage

Both Journal and CT metrics should be saved to the same CSV:

**File**: `metrics/metrics_2026-04-14.csv`

```csv
timestamp,source_table,target_table,journal_total,journal_inserts,journal_updates,journal_deletes,ct_total,ct_inserts,ct_updates,ct_deletes,replication_lag
2026-04-14T09:00:00,GSLIBTST.CUSTOMERS,dbo.CUSTOMERS,17,10,5,2,15,9,5,1,2
2026-04-14T09:05:00,GSLIBTST.CUSTOMERS,dbo.CUSTOMERS,23,15,6,2,20,12,6,2,3
```

---

## ✅ Benefits of Corrected Design

### 1. **Always Fresh Data**
- Cache is updated BEFORE aggregation
- No stale data returned
- Incremental fetches keep cache current

### 2. **Fast Performance**
- AS400: 8s → 3s (62.5% faster)
- MSSQL: 2s → 1s (50% faster)
- Aggregation: <100ms (from cache)

### 3. **Accurate Delta Counting**
- Each monitoring cycle shows counts for THAT interval
- Not cumulative totals
- Enables pattern detection for replication lag

### 4. **Scalable**
- 1000+ tables monitored efficiently
- Each table: ~3-4 seconds per cycle
- Total: ~50 minutes for 1000 tables (can parallelize)

### 5. **Resumable**
- Crash/interruption recovery
- Resume from last position
- No data loss

---

## 🚀 Next Steps

1. **Fix MSSQL CT cache** (same pattern as journal)
2. **Add CT entry appending** to JournalCache
3. **Update monitor.py** to use time windows for CT too
4. **Test end-to-end** with real traffic
5. **Update qadmcli documentation** for retrieve entries features

---

**Status**: 
- AS400 Journal: ✅ Fixed
- MSSQL CT: ❌ Needs same fix
- Documentation: ❌ Needs update
