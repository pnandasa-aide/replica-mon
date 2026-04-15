# SQLite Cache Gap-Filling & Reconciliation Strategy

## Problem Statement

When replica-mon detects mismatch between source (AS400) and target (MSSQL):
- Source shows 1,234 changes for CUSTOMERS
- Target shows 1,200 changes for dbo.CUSTOMERS
- **Missing: 34 changes**

**Question**: Can we use SQLite cache to identify and fill the missing records?

---

## ✅ YES! Here's How:

### Architecture for Reconciliation:

```
┌─────────────────────────────────────────────────────────────┐
│ Step 1: DETECT MISMATCH                                     │
│ replica-mon compares source vs target counts                │
│ Finds: CUSTOMERS has 34 missing records                     │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 2: QUERY SQLITE CACHE                                  │
│ Find which sequences/versions are missing                   │
│ Cache has: All journal entries with sequences               │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 3: IDENTIFY GAPS                                       │
│ Compare cache sequences vs MSSQL CT versions                │
│ Missing: versions 12,401-12,434                             │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 4: REPLAY MISSING CHANGES                              │
│ Option A: Re-fetch from AS400 journal (via qadmcli)         │
│ Option B: Use cached data if still in SQLite                │
│ Option C: Generate SQL/commands to apply missing changes    │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│ Step 5: VERIFY RECONCILIATION                               │
│ Re-count: Source = Target ✅                                │
│ Mark as reconciled in monitoring report                     │
└─────────────────────────────────────────────────────────────┘
```

---

## Implementation: Reconciliation Script

### Step 1: Detect Missing Sequences

```python
#!/usr/bin/env python3
"""
Identify gaps between AS400 journal and MSSQL Change Tracking.
"""

import sqlite3
from collections import defaultdict

def identify_gaps(library, table_name):
    """
    Find sequences in AS400 that haven't been applied to MSSQL.
    
    Returns: List of missing sequence numbers
    """
    
    # 1. Get AS400 sequences from SQLite cache
    journal_db = 'cache/journal_cache.db'
    conn = sqlite3.connect(journal_db)
    
    cursor = conn.execute("""
        SELECT sequence_number, timestamp, operation 
        FROM journal_entries 
        WHERE object = ? 
        AND library = ?
        ORDER BY sequence_number
    """, (table_name, library))
    
    as400_sequences = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    print(f"AS400 journal sequences for {table_name}: {len(as400_sequences)}")
    print(f"Range: {min(as400_sequences)} → {max(as400_sequences)}")
    
    # 2. Get MSSQL CT versions from cache
    ct_db = 'cache/ct_cache.db'
    conn = sqlite3.connect(ct_db)
    
    cursor = conn.execute("""
        SELECT sys_change_version 
        FROM ct_dbo_{table}
        ORDER BY sys_change_version
    """.format(table=table_name))
    
    mssql_versions = [row[0] for row in cursor.fetchall()]
    conn.close()
    
    print(f"MSSQL CT versions for dbo.{table_name}: {len(mssql_versions)}")
    print(f"Range: {min(mssql_versions)} → {max(mssql_versions)}")
    
    # 3. Find gaps
    # Note: We need a mapping between AS400 sequences and MSSQL versions
    # This requires GlueSync to maintain the mapping, or we infer by timestamp
    
    return {
        'as400_sequences': as400_sequences,
        'mssql_versions': mssql_versions,
        'count_diff': len(as400_sequences) - len(mssql_versions)
    }

# Example usage
gaps = identify_gaps('GSLIBTST', 'CUSTOMERS')
print(f"\nMissing records: {gaps['count_diff']}")
```

### Step 2: Replay Missing Changes

```python
#!/usr/bin/env python3
"""
Replay missing journal entries to reconcile source and target.
"""

import subprocess
import json
from datetime import datetime

def replay_missing_changes(library, table_name, from_sequence, to_sequence):
    """
    Fetch missing journal entries and generate reconciliation commands.
    
    Args:
        library: AS400 library (e.g., GSLIBTST)
        table_name: Table name (e.g., CUSTOMERS)
        from_sequence: Start sequence number
        to_sequence: End sequence number
    """
    
    print(f"Fetching journal entries from {from_sequence} to {to_sequence}...")
    
    # Use qadmcli to fetch specific sequence range
    cmd = f"""
    /home/ubuntu/_qoder/qadmcli/qadmcli.sh journal entries \\
        -t {table_name} \\
        -l {library} \\
        --format json
    """
    
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"❌ Failed to fetch journal entries")
        return None
    
    entries = json.loads(result.stdout)
    
    # Filter to sequence range
    missing_entries = [
        e for e in entries
        if from_sequence <= e['Sequence Number'] <= to_sequence
    ]
    
    print(f"Found {len(missing_entries)} missing entries")
    
    # Generate reconciliation plan
    reconciliation_plan = []
    
    for entry in missing_entries:
        plan_entry = {
            'sequence': entry['Sequence Number'],
            'timestamp': entry['Timestamp'],
            'operation': entry['Operation Type'],
            'object': entry['Object'],
            'member': entry['Member'],
            'raw_data': entry.get('Input Data'),
            
            # Action to take
            'action': determine_action(entry['Operation Type']),
            'sql': generate_sql(entry),
        }
        reconciliation_plan.append(plan_entry)
    
    return reconciliation_plan

def determine_action(operation):
    """Determine what action to take based on journal operation."""
    
    actions = {
        'IR': 'INSERT',      # Insert Record
        'UP': 'UPDATE',      # Update Record
        'DL': 'DELETE',      # Delete Record
        'PT': 'SKIP',        # Physical Delete (already done)
        'NC': 'SKIP',        # No Change
    }
    
    return actions.get(operation, 'REVIEW')

def generate_sql(entry):
    """
    Generate SQL statement to replay the change.
    This is a simplified example - actual implementation depends on:
    - Table schema
    - Data type conversion (AS400 → MSSQL)
    - Primary key identification
    """
    
    operation = entry['Operation Type']
    table = entry['Object']
    raw_data = entry.get('Input Data', '')
    
    if operation == 'IR':
        return f"-- INSERT into dbo.{table} (requires data parsing)"
    elif operation == 'UP':
        return f"-- UPDATE dbo.{table} (requires data parsing)"
    elif operation == 'DL':
        return f"-- DELETE from dbo.{table} (requires PK identification)"
    else:
        return f"-- REVIEW: {operation}"

# Example usage
plan = replay_missing_changes('GSLIBTST', 'CUSTOMERS', 34800, 34834)

if plan:
    print(f"\nReconciliation Plan:")
    print(f"{'Seq':<10} {'Timestamp':<22} {'Op':<6} {'Action':<10}")
    print("-" * 60)
    
    for entry in plan:
        print(f"{entry['sequence']:<10} {entry['timestamp']:<22} {entry['operation']:<6} {entry['action']:<10}")
    
    print(f"\nTotal changes to replay: {len(plan)}")
```

---

## Key Challenges & Solutions:

### Challenge 1: Sequence ≠ Version Mapping

**Problem**: AS400 uses journal sequences, MSSQL uses CT versions. How do we map them?

**Solutions**:

#### Option A: Use Timestamps (Approximate)
```python
# Match by timestamp (within tolerance)
as400_time = datetime.fromisoformat(journal_entry['Timestamp'])
mssql_time = datetime.fromisoformat(ct_entry['sys_change_timestamp'])

if abs((as400_time - mssql_time).total_seconds()) < 5:
    # Likely the same change
    return True
```

#### Option B: Track Mapping in GlueSync
```python
# If GlueSync exposed mapping (it doesn't currently):
# sequence 34801 → version 12401
# sequence 34802 → version 12402
# etc.
```

#### Option C: Maintain Our Own Mapping ⭐ RECOMMENDED
```python
# In SQLite cache, add a mapping table
CREATE TABLE sequence_version_map (
    as400_sequence INTEGER,
    mssql_version INTEGER,
    table_name TEXT,
    timestamp TEXT,
    processed_at TEXT DEFAULT CURRENT_TIMESTAMP
);

# When we detect changes, record the mapping
INSERT INTO sequence_version_map 
    (as400_sequence, mssql_version, table_name, timestamp)
VALUES (34801, 12401, 'CUSTOMERS', '2026-04-14 09:30:00');
```

### Challenge 2: Data Type Conversion

**Problem**: AS400 data types differ from MSSQL

**Solution**: Use existing conversion logic from GlueSync (documented in GLUESYNC_UDF_ANALYSIS.md)

```python
AS400 Type          → MSSQL Type
────────────────────────────────
CHAR(10)            → VARCHAR(10)
DECIMAL(9,2)        → DECIMAL(9,2)
DATE                → DATE
TIMESTAMP           → DATETIME2
ZONED(7,0)          → INT
VARCHAR(50)         → VARCHAR(50)
```

### Challenge 3: Binary Data in Journal

**Problem**: Journal entries contain binary data that needs parsing

**Solution**: Use qadmcli's `--format json` which already parses the data:
```bash
qadmcli.sh journal entries -t CUSTOMERS -l GSLIBTST --format json
# Returns parsed JSON with human-readable values
```

---

## Practical Reconciliation Workflow:

### Scenario: Detected 34 Missing Records

```bash
# Step 1: Run monitor to detect mismatch
python3 monitor.py --verbose

# Output shows:
# CUSTOMERS: AS400=1,234 changes, MSSQL=1,200 changes
# ⚠️  MISMATCH: 34 records missing!

# Step 2: Run reconciliation analysis
python3 reconcile.py --library GSLIBTST --table CUSTOMERS

# Output:
# Found 34 missing sequences: 34801-34834
# Generated reconciliation plan with 34 SQL statements

# Step 3: Review plan
cat reconciliation_plan.json

# Step 4: Apply changes (DRY RUN first)
python3 apply_reconciliation.py --plan reconciliation_plan.json --dry-run

# Step 5: Apply for real
python3 apply_reconciliation.py --plan reconciliation_plan.json --apply

# Step 6: Verify
python3 monitor.py --verbose
# Now shows: CUSTOMERS: AS400=1,234 changes, MSSQL=1,234 changes ✅
```

---

## Recommended Implementation Priority:

### Phase 1: Gap Detection (2-3 hours) ✅ HIGH VALUE
- [x] SQLite cache already stores all sequences ✅
- [ ] Add gap detection logic to compare AS400 vs MSSQL
- [ ] Report missing sequences/versions per entity
- [ ] Add to monitoring output

### Phase 2: Reconciliation Planning (3-4 hours)
- [ ] Fetch missing journal entries via qadmcli
- [ ] Generate SQL/commands for each missing change
- [ ] Create reconciliation plan JSON
- [ ] Add dry-run support

### Phase 3: Automated Reconciliation (4-6 hours)
- [ ] Implement data type conversion
- [ ] Apply changes to MSSQL
- [ ] Verify reconciliation
- [ ] Add to automated monitoring

### Phase 4: Sequence-Version Mapping (Future)
- [ ] Track mapping in SQLite cache
- [ ] Improve gap detection accuracy
- [ ] Enable precise reconciliation

---

## Summary:

### Can SQLite cache help with reconciliation? **YES!** ✅

**What we have**:
- ✅ All AS400 journal sequences in SQLite cache
- ✅ All MSSQL CT versions in SQLite cache
- ✅ Time-windowed queries work
- ✅ Per-entity tracking works

**What we need to add**:
- [ ] Sequence-to-version mapping (critical!)
- [ ] Gap detection logic
- [ ] Reconciliation plan generator
- [ ] Automated replay mechanism

**How it works**:
1. **Detect** mismatch via monitoring
2. **Query** SQLite cache to find missing sequences
3. **Fetch** full journal entries for missing sequences
4. **Generate** reconciliation commands
5. **Apply** missing changes to target
6. **Verify** source = target

**Bottom line**: SQLite cache is PERFECT for this - it has all the historical data we need! 🎯
