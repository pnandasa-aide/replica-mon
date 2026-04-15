# Per-Entity Tracking & Reconciliation - Complete Strategy

## Your Two Questions Answered:

### Q1: How do we track per-entity progress when GlueSync only tracks journal-level?

### Q2: Can SQLite cache be re-run to fill missing gaps/records for reconciliation?

---

## ✅ Answer 1: Per-Entity Tracking Strategy

### The Challenge:

```
AS400 Journal (CUSTJRN) - ONE journal for MANY tables
├─ Seq 35035: ORDERS INSERT
├─ Seq 35036: CUSTOMERS UPDATE  
├─ Seq 35037: ORDERS DELETE
├─ Seq 35038: PRODUCTS INSERT
└─ Seq 35039: CUSTOMERS INSERT

GlueSync Checkpoint: "I'm at sequence 35039" (JOURNAL-LEVEL ONLY)
```

**Problem**: We don't know where each TABLE is, only where the JOURNAL is.

### The Solution: SQLite Cache Already Does This! 🎯

Our **SQLite journal cache** tracks every single journal entry with:
- Sequence number
- **Object (table name)** ← This is the key!
- Operation type
- Timestamp
- Full binary data

```sql
-- Query per-entity progress
SELECT 
    object as table_name,
    COUNT(*) as total_changes,
    MIN(sequence_number) as first_sequence,
    MAX(sequence_number) as last_sequence,
    MIN(timestamp) as first_change,
    MAX(timestamp) as last_change
FROM journal_entries
WHERE library = 'GSLIBTST'
GROUP BY object
ORDER BY object;

Results:
┌──────────┬───────────────┬────────────────┬───────────────┬─────────────────────┐
│ table    │ total_changes │ first_sequence │ last_sequence │ last_change         │
├──────────┼───────────────┼────────────────┼───────────────┼─────────────────────┤
│ CUSTOMERS│ 456           │ 34123          │ 35036         │ 2026-04-14 09:30:15 │
│ ORDERS   │ 789           │ 34001          │ 35037         │ 2026-04-14 09:30:20 │
│ PRODUCTS │ 234           │ 34567          │ 35032         │ 2026-04-14 09:29:45 │
└──────────┴───────────────┴────────────────┴───────────────┴─────────────────────┘
```

### Per-Entity Tracking Architecture:

```
┌─────────────────────────────────────────────────────────┐
│ AS400 Journal (Interleaved - All Tables Mixed)          │
│ Seq 35035: ORDERS                                       │
│ Seq 35036: CUSTOMERS                                    │
│ Seq 35037: ORDERS                                       │
│ Seq 35038: PRODUCTS                                     │
└─────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│ GlueSync Checkpoint (Journal-Level Only)                │
│ "I processed up to sequence 35037"                      │
│ ❌ No per-table information                             │
└─────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│ replica-mon SQLite Cache (Per-Entity!) ✅               │
│ ┌───────────────────────────────────────────────────┐   │
│ │ CUSTOMERS:                                        │   │
│ │   - 456 changes                                   │   │
│ │   - Sequences: 34123 → 35036                      │   │
│ │   - Last change: 09:30:15                         │   │
│ │                                                   │   │
│ │ ORDERS:                                           │   │
│ │   - 789 changes                                   │   │
│ │   - Sequences: 34001 → 35037                      │   │
│ │   - Last change: 09:30:20                         │   │
│ │                                                   │   │
│ │ PRODUCTS:                                         │   │
│ │   - 234 changes                                   │   │
│ │   - Sequences: 34567 → 35032                      │   │
│ │   - Last change: 09:29:45                         │   │
│ └───────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│ MSSQL Change Tracking (Per-Entity!) ✅                  │
│ ┌───────────────────────────────────────────────────┐   │
│ │ dbo.CUSTOMERS:                                    │   │
│ │   - 450 changes                                   │   │
│ │   - Versions: 12001 → 12450                       │   │
│ │   - Last change: 09:30:18                         │   │
│ │                                                   │   │
│ │ dbo.ORDERS:                                       │   │
│ │   - 785 changes                                   │   │
│ │   - Versions: 15001 → 15785                       │   │
│ │   - Last change: 09:30:22                         │   │
│ │                                                   │   │
│ │ dbo.PRODUCTS:                                     │   │
│ │   - 230 changes                                   │   │
│ │   - Versions: 8001 → 8230                         │   │
│ │   - Last change: 09:29:48                         │   │
│ └───────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Monitoring Output Example:

```
Entity Monitoring (Last 15 Minutes):
==================================================================

CUSTOMERS:
  AS400 Journal:  +12 changes (seq 35025 → 35036)
  MSSQL CT:       +10 changes (ver 12441 → 12450)
  Lag:            2 changes behind
  Status:         ⚠️  Minor lag

ORDERS:
  AS400 Journal:  +18 changes (seq 35020 → 35037)
  MSSQL CT:       +18 changes (ver 15768 → 15785)
  Lag:            0 changes
  Status:         ✅ Current

PRODUCTS:
  AS400 Journal:  +5 changes (seq 35028 → 35032)
  MSSQL CT:       +4 changes (ver 8227 → 8230)
  Lag:            1 change behind
  Status:         ⚠️  Minor lag
```

---

## ✅ Answer 2: Gap-Filling & Reconciliation

### Can SQLite cache be re-run to fill missing gaps?

**YES!** Here's how:

### Scenario: Detected Mismatch

```
Monitoring detects:
  CUSTOMERS:
    AS400: 1,234 changes
    MSSQL: 1,200 changes
    ❌ MISSING: 34 changes!
```

### Reconciliation Process:

```
Step 1: Identify Missing Sequences
===================================
Query SQLite cache for AS400 sequences:
  CUSTOMERS: sequences 34001-35036 (1,234 entries)

Query SQLite cache for MSSQL versions:
  dbo.CUSTOMERS: versions 12001-12450 (1,200 entries)

Find the gap:
  Need to map sequences → versions
  Identify which 34 sequences haven't been applied
```

```
Step 2: Fetch Missing Journal Entries
======================================
Use qadmcli to fetch specific entries:

  qadmcli.sh journal entries \
      -t CUSTOMERS \
      -l GSLIBTST \
      --format json

Filter to missing sequences:
  [
    {"Sequence Number": 34801, "Operation": "IR", ...},
    {"Sequence Number": 34802, "Operation": "UP", ...},
    ...34 entries total
  ]
```

```
Step 3: Generate Reconciliation Commands
==========================================
Convert journal entries to SQL:

  Sequence 34801 (IR) → INSERT INTO dbo.CUSTOMERS (...) VALUES (...)
  Sequence 34802 (UP) → UPDATE dbo.CUSTOMERS SET ... WHERE PK = ...
  Sequence 34803 (DL) → DELETE FROM dbo.CUSTOMERS WHERE PK = ...
  
Data type conversion:
  AS400 CHAR(10) → MSSQL VARCHAR(10)
  AS400 DECIMAL(9,2) → MSSQL DECIMAL(9,2)
  AS400 DATE → MSSQL DATE
```

```
Step 4: Apply Missing Changes
==============================
Execute generated SQL on MSSQL:

  BEGIN TRANSACTION;
  
  INSERT INTO dbo.CUSTOMERS (CUSTID, NAME, ...) 
  VALUES (12345, 'John Doe', ...);
  
  UPDATE dbo.CUSTOMERS 
  SET EMAIL = 'new@email.com' 
  WHERE CUSTID = 12345;
  
  DELETE FROM dbo.CUSTOMERS WHERE CUSTID = 12346;
  
  COMMIT;
```

```
Step 5: Verify Reconciliation
==============================
Re-run monitoring:

  CUSTOMERS:
    AS400: 1,234 changes
    MSSQL: 1,234 changes
    ✅ RECONCILED!
```

---

## 🔑 Critical Challenge: Sequence-to-Version Mapping

### The Problem:

```
AS400 uses journal SEQUENCES: 34001, 34002, 34003...
MSSQL uses CT VERSIONS: 12001, 12002, 12003...

How do we know:
  Sequence 34001 = Version 12001?
  Sequence 34002 = Version 12002?
```

### Solution Options:

#### Option 1: Timestamp Matching (Approximate) ⭐ CURRENT BEST

```python
# Match by timestamp (within 5 second tolerance)
as400_time = datetime.fromisoformat(journal_entry['Timestamp'])
mssql_time = datetime.fromisoformat(ct_entry['sys_change_timestamp'])

if abs((as400_time - mssql_time).total_seconds()) < 5:
    # Likely the same change
    return True
```

**Pros**: 
- ✅ Works now, no changes needed
- ✅ Good enough for most cases

**Cons**:
- ❌ Not 100% accurate (changes can be processed out of order)
- ❌ Fails if clocks are out of sync

#### Option 2: Maintain Our Own Mapping Table ⭐ RECOMMENDED

```sql
-- Add to SQLite cache
CREATE TABLE sequence_version_mapping (
    table_name TEXT,
    as400_sequence INTEGER,
    mssql_version INTEGER,
    timestamp TEXT,
    detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (table_name, as400_sequence)
);

-- When we detect changes, record the mapping
INSERT INTO sequence_version_mapping 
    (table_name, as400_sequence, mssql_version, timestamp)
VALUES 
    ('CUSTOMERS', 34801, 12401, '2026-04-14 09:30:00'),
    ('CUSTOMERS', 34802, 12402, '2026-04-14 09:30:05');
```

**How to build the mapping**:
1. Monitor changes in real-time
2. When we see AS400 change at time T with sequence S
3. And MSSQL change at time T±5s with version V
4. Record: S → V mapping

**Pros**:
- ✅ Accurate mapping
- ✅ Built over time automatically
- ✅ Enables precise reconciliation

**Cons**:
- ❌ Takes time to build (days/weeks)
- ❌ Requires continuous monitoring

#### Option 3: Use GlueSync's Internal Mapping (If Available)

GlueSync MUST maintain this mapping internally, but it's not exposed via API.

**Would require**:
- Modifying GlueSync to expose mapping
- Or capturing from GlueSync logs/metrics

---

## 📊 Implementation Roadmap:

### Phase 1: Enhanced Per-Entity Monitoring (1-2 hours) ✅ EASY

**What we have**:
- ✅ SQLite cache with all journal entries
- ✅ Per-entity data already stored
- ✅ Time-windowed queries working

**What to add**:
- [ ] Per-entity summary in monitoring output
- [ ] Show min/max sequence per table
- [ ] Show change count per table
- [ ] Compare AS400 vs MSSQL per table

**Code needed**:
```python
# Add to monitor.py
def show_per_entity_summary():
    """Show per-entity progress."""
    
    # Query SQLite cache
    cursor.execute("""
        SELECT object, COUNT(*), MIN(sequence_number), MAX(sequence_number)
        FROM journal_entries
        GROUP BY object
    """)
    
    # Display for each table
    for table, count, min_seq, max_seq in cursor:
        print(f"{table}: {count} changes, seq {min_seq}→{max_seq}")
```

### Phase 2: Gap Detection (2-3 hours) ✅ HIGH VALUE

- [ ] Compare AS400 count vs MSSQL count per entity
- [ ] Identify missing sequences/versions
- [ ] Report gaps in monitoring output
- [ ] Use timestamp matching for sequence-version mapping

**Example output**:
```
⚠️  GAP DETECTED: CUSTOMERS
  AS400: 1,234 changes (seq 34001-35036)
  MSSQL: 1,200 changes (ver 12001-12450)
  Missing: ~34 changes
  
  Likely missing sequences: 34801-34834
  Time range: 09:25:00 - 09:28:00
```

### Phase 3: Reconciliation Planning (3-4 hours)

- [ ] Fetch missing journal entries
- [ ] Generate SQL/commands for each missing change
- [ ] Create reconciliation plan JSON
- [ ] Add dry-run support

### Phase 4: Automated Reconciliation (4-6 hours)

- [ ] Implement data type conversion
- [ ] Apply changes to MSSQL
- [ ] Verify reconciliation
- [ ] Add to monitoring

### Phase 5: Sequence-Version Mapping (Future Enhancement)

- [ ] Build mapping table over time
- [ ] Improve gap detection accuracy
- [ ] Enable precise reconciliation

---

## 💡 Key Insights:

### 1. SQLite Cache is PERFECT for Both Tasks

✅ **Per-entity tracking**: Already works! Just need to query by table name  
✅ **Reconciliation**: Has all historical data needed  
✅ **Gap detection**: Can query sequence ranges  
✅ **Time-windowed**: Can filter by time periods  

### 2. GlueSync Limitation is NOT a Problem

❌ GlueSync only tracks journal-level  
✅ But we track per-entity in our own SQLite cache  
✅ We don't need GlueSync to do this for us  

### 3. Reconciliation is FEASIBLE

✅ We can fetch missing journal entries  
✅ We can convert data types (documented)  
✅ We can generate SQL/commands  
⚠️ Need sequence-version mapping (can use timestamps)  
⚠️ Need to handle edge cases (out-of-order, etc.)  

### 4. Recommended Starting Point

**Start with Phase 1 & 2** (3-5 hours total):
- Enhanced per-entity monitoring
- Gap detection
- Better reporting

This gives immediate value and sets foundation for reconciliation.

---

## 📁 Related Files:

- `demo_per_entity_tracking.py` - Demo of per-entity tracking
- `RECONCILIATION_STRATEGY.md` - Detailed reconciliation plan
- `lib/sqlite_journal_cache.py` - SQLite cache for AS400 journal
- `lib/sqlite_ct_cache.py` - SQLite cache for MSSQL CT
- `GLUESYNC_CACHE_ANALYSIS.md` - Analysis of GlueSync cache structure

---

## 🎯 Summary:

### Q1: Per-Entity Tracking?
**Answer**: Already working via SQLite cache! Each journal entry stores the table name (`object` field), so we can query per-entity progress. GlueSync's journal-level limitation doesn't affect us.

### Q2: Gap-Filling & Reconciliation?
**Answer**: YES! SQLite cache has all the historical data. We can:
1. Detect gaps (count mismatches)
2. Identify missing sequences
3. Fetch full journal entries
4. Generate reconciliation commands
5. Apply missing changes

**Critical need**: Sequence-to-version mapping (can use timestamp matching initially, build proper mapping over time).

**Bottom line**: Our SQLite cache architecture is PERFECT for both tasks! 🎉
