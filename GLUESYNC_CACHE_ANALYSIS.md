# GlueSync Cache Analysis: Can We Use It?

**Date**: 2026-04-14  
**Analysis**: GlueSync agent cache structure and feasibility for replica-mon

---

## GlueSync Cache Architecture

### Directory Structure

```
agents/dfb34af1/  (SOURCE agent)
├── bootstrap.json              # Agent identification
├── journal-checkpoints/        # Progress tracking (JSON - READABLE!)
│   └── GSLIBTST/
│       ├── CUSTJRN.cp         # Checkpoint for CUSTOMERS table journal
│       └── GSLIBTSJRN.cp      # Checkpoint for general journal
└── database-cache/             # Actual journal data (Binary - Chronicle Queue)
    ├── 2ac8c2a8/              # Hash-based directory (table-specific)
    │   └── -2584460013204809594/
    │       ├── metadata.cq4t  # Chronicle Queue metadata (binary)
    │       └── 20260413-15F.cq4  # Daily data files (80MB each)
    ├── 2f7b032d/              # Another table
    └── f21ce41a/              # Another table
```

---

## Key Findings

### 1. Journal Checkpoints ✅ READABLE

**Format**: JSON files with `.cp` extension  
**Location**: `journal-checkpoints/{library}/{journal}.cp`

**Content**:
```json
{
  "journalLibrary": "GSLIBTST",
  "journalName": "CUSTJRN",
  "receiverLibrary": "GSLIBTST",
  "receiverName": "CUSTRC0001",
  "sequenceNumber": "00000000000000035037",
  "timestamp": 1776130462526
}
```

**What it tracks**:
- Last processed journal sequence number
- Last processed timestamp
- Current journal receiver being read
- Journal name and library

**Current Status**:
- **CUSTJRN**: Sequence 35,037 (2026-04-14 08:34:22)
- **GSLIBTSJRN**: Sequence 4,474,014 (2026-04-14 08:34:43)

**Comparison with AS400**:
```
GlueSync checkpoint: 35,037
AS400 newest entry:  35,037  ✅ MATCHES!
```

**Conclusion**: GlueSync is up-to-date with AS400 journal!

---

### 2. Database Cache ⚠️ BINARY (Chronicle Queue)

**Format**: Chronicle Queue (.cq4 files)  
**Location**: `database-cache/{hash}/{negative_id}/`

**Characteristics**:
- Binary format (not human-readable)
- 80MB per day per table
- Uses Java-specific serialization
- Requires Chronicle Queue library to read

**File naming**: `YYYYMMDD-{counter}F.cq4`
- Example: `20260413-15F.cq4` = April 13, 2026, file #15

**Storage per table**:
- ~80MB per day
- Multiple files per day (rolled by size)
- Example: 4 files = 320MB for one day

**Total for 3 tables**: ~1GB+ of binary data

---

## Can We Read the Cache?

### Option 1: Read Checkpoints ✅ YES (Easy)

**Feasibility**: ✅ **HIGHLY FEASIBLE**

**What we can do**:
```python
import json
from pathlib import Path

checkpoint_path = Path("/path/to/journal-checkpoints/GSLIBTST/CUSTJRN.cp")
with open(checkpoint_path) as f:
    checkpoint = json.load(f)
    
last_sequence = int(checkpoint['sequenceNumber'])
last_timestamp = checkpoint['timestamp']

print(f"GlueSync processed up to sequence {last_sequence:,}")
print(f"Last processed: {datetime.fromtimestamp(last_timestamp/1000)}")
```

**Benefits**:
- ✅ Instant access (no AS400 query needed)
- ✅ Know exactly where GlueSync is
- ✅ Detect if replica-mon is ahead/behind GlueSync
- ✅ No binary parsing required

**Use Case**: 
- **Prerequisite check**: Verify GlueSync is running and processing
- **Sequence gap detection**: Compare our cache vs GlueSync's position
- **Lag calculation**: How far behind GlueSync are we?

---

### Option 2: Read Database Cache ❌ NO (Very Difficult)

**Feasibility**: ❌ **NOT FEASIBLE**

**Challenges**:
1. **Binary format**: Chronicle Queue is Java-specific
2. **No Python library**: No official Python reader for .cq4 files
3. **Complex structure**: Requires understanding internal serialization
4. **80MB files**: Large binary blobs, not efficient to parse
5. **Table mapping**: Hash-based directory names (can't tell which table)

**Would require**:
- Writing a Chronicle Queue reader in Python
- Reverse-engineering the binary format
- Maintaining compatibility with GlueSync updates

**Cost vs Benefit**:
- ❌ High development cost (weeks of work)
- ❌ Maintenance burden (GlueSync updates may break it)
- ❌ Minimal benefit (we already have AS400 access via qadmcli)

**Conclusion**: Not worth the effort!

---

## Recommended Strategy

### Hybrid Approach: Checkpoints + AS400

**Use GlueSync Checkpoints For**:
1. ✅ **Position tracking**: Know where GlueSync is
2. ✅ **Lag detection**: Calculate replication lag
3. ✅ **Health monitoring**: Verify GlueSync is running
4. ✅ **Sequence validation**: Detect gaps or jumps

**Use AS400 Journal For**:
1. ✅ **Actual data reading**: Get journal entries via qadmcli
2. ✅ **Time-windowed queries**: Flexible time ranges
3. ✅ **Fallback**: If GlueSync is behind or stopped

### Implementation Example

```python
from pathlib import Path
import json
from datetime import datetime

class GlueSyncMonitor:
    def __init__(self, gluesync_agent_path):
        self.checkpoint_dir = Path(gluesync_agent_path) / "journal-checkpoints"
    
    def get_gluesync_position(self, library, journal_name):
        """Read GlueSync checkpoint to get last processed position."""
        checkpoint_file = self.checkpoint_dir / library / f"{journal_name}.cp"
        
        if not checkpoint_file.exists():
            return None
        
        with open(checkpoint_file) as f:
            checkpoint = json.load(f)
        
        return {
            'sequence': int(checkpoint['sequenceNumber']),
            'timestamp': datetime.fromtimestamp(checkpoint['timestamp'] / 1000),
            'receiver': f"{checkpoint['receiverLibrary']}/{checkpoint['receiverName']}",
            'journal': f"{checkpoint['journalLibrary']}/{checkpoint['journalName']}"
        }
    
    def calculate_lag(self, library, journal_name, current_as400_sequence):
        """Calculate how far behind GlueSync is compared to AS400."""
        gluesync_pos = self.get_gluesync_position(library, journal_name)
        
        if not gluesync_pos:
            return None
        
        lag_sequences = current_as400_sequence - gluesync_pos['sequence']
        
        return {
            'gluesync_sequence': gluesync_pos['sequence'],
            'as400_sequence': current_as400_sequence,
            'lag_sequences': lag_sequences,
            'gluesync_timestamp': gluesync_pos['timestamp'],
            'is_current': lag_sequences < 100  # Within 100 entries
        }

# Usage
monitor = GlueSyncMonitor('/home/ubuntu/molo17/DB2-MSS_53e4/gluesync-docker/data/core-hub/agents/dfb34af1')

# Check GlueSync position
position = monitor.get_gluesync_position('GSLIBTST', 'CUSTJRN')
print(f"GlueSync last processed: sequence {position['sequence']:,}")
print(f"  Timestamp: {position['timestamp']}")

# Check if GlueSync is current
lag = monitor.calculate_lag('GSLIBTST', 'CUSTJRN', current_as400_sequence=35037)
print(f"Lag: {lag['lag_sequences']} sequences")
print(f"Is current: {lag['is_current']}")
```

---

## Integration with replica-mon

### Enhanced Monitor.py

```python
def check_gluesync_health(source_table, gluesync_agent_path):
    """
    Check if GlueSync is actively processing this table.
    
    Returns:
        dict with gluesync status and lag
    """
    library, table = source_table.split('.')
    
    # Determine journal name (from AS400 journal info)
    journal_info = get_journal_info(library, table)
    journal_name = journal_info['journal_name']
    
    # Read GlueSync checkpoint
    checkpoint_file = Path(gluesync_agent_path) / "journal-checkpoints" / library / f"{journal_name}.cp"
    
    if not checkpoint_file.exists():
        return {
            'gluesync_active': False,
            'reason': f'No checkpoint found for {journal_name}'
        }
    
    with open(checkpoint_file) as f:
        checkpoint = json.load(f)
    
    gluesync_sequence = int(checkpoint['sequenceNumber'])
    gluesync_timestamp = datetime.fromtimestamp(checkpoint['timestamp'] / 1000)
    
    # Get current AS400 position
    as400_newest = journal_info['newest_entry_sequence']
    
    # Calculate lag
    lag = as400_newest - gluesync_sequence
    
    return {
        'gluesync_active': True,
        'gluesync_sequence': gluesync_sequence,
        'as400_sequence': as400_newest,
        'lag_sequences': lag,
        'gluesync_timestamp': gluesync_timestamp,
        'is_current': lag < 100,  # Within 100 entries
        'warning': lag > 1000  # More than 1000 entries behind
    }
```

### Smart Cache Strategy

```python
def get_journal_entries_smart(table, since, use_gluesync_cache=True):
    """
    Intelligently fetch journal entries:
    1. Check GlueSync checkpoint first
    2. If GlueSync is current, trust our SQLite cache
    3. If GlueSync is behind, query AS400 directly
    4. Detect and report sequence gaps
    """
    library, table_name = table.split('.')
    
    if use_gluesync_cache:
        # Step 1: Check GlueSync position
        gluesync_status = check_gluesync_health(table, gluesync_agent_path)
        
        if gluesync_status['gluesync_active'] and gluesync_status['is_current']:
            # GlueSync is current - use our SQLite cache (fast!)
            print(f"  ✓ GlueSync is current (lag: {gluesync_status['lag_sequences']} sequences)")
            return get_from_sqlite_cache(table, since)
        else:
            # GlueSync is behind or not active - query AS400
            print(f"  ⚠️  GlueSync lag: {gluesync_status['lag_sequences']} sequences")
            print(f"  → Querying AS400 directly...")
            return get_from_as400(table, since)
    else:
        # No GlueSync check - always query AS400
        return get_from_as400(table, since)
```

---

## Benefits of Using Checkpoints

### 1. **Replication Lag Monitoring**
- See exactly how far behind GlueSync is
- Alert if GlueSync stops processing
- Track lag trends over time

### 2. **Cache Validation**
- If GlueSync sequence matches our cache, trust our cache
- If there's a gap, re-sync from AS400
- Prevent missing entries

### 3. **Health Checks**
- Monitor if GlueSync agent is alive
- Detect stuck receivers
- Identify journal receiver switches

### 4. **Performance Optimization**
- Skip AS400 queries if GlueSync is current
- Use SQLite cache when safe
- Only query AS400 when necessary

---

## What We CANNOT Do

### ❌ Read Actual Journal Data from GlueSync Cache

**Why not**:
- Chronicle Queue binary format
- No Python library available
- Would require reverse-engineering
- High maintenance cost

**Alternative**:
- ✅ Use qadmcli to query AS400 directly
- ✅ Cache results in SQLite (what we already do!)
- ✅ Use checkpoints for position tracking only

---

## Summary & Recommendations

### ✅ DO Use GlueSync Checkpoints

**Purpose**: Position tracking and health monitoring

**Implementation**:
1. Read `.cp` files (JSON format)
2. Compare with AS400 journal state
3. Calculate replication lag
4. Validate our SQLite cache

**Effort**: Low (1-2 hours to implement)  
**Benefit**: High (better monitoring, lag detection)

---

### ❌ DON'T Read GlueSync Database Cache

**Purpose**: Actual journal entry data

**Why not**:
- Binary Chronicle Queue format
- No Python reader available
- High development cost
- Minimal benefit vs AS400 queries

**Alternative**:
- Keep using qadmcli → AS400
- Cache in SQLite (already implemented!)
- Use checkpoints for validation

---

### 🎯 Recommended Implementation Priority

1. **Phase 1** (High Priority): Checkpoint Reader
   - Read GlueSync checkpoints
   - Calculate lag
   - Add to monitoring output
   - **Effort**: 1-2 hours

2. **Phase 2** (Medium Priority): Smart Cache
   - Check GlueSync before AS400 query
   - Use SQLite cache if GlueSync is current
   - Fallback to AS400 if behind
   - **Effort**: 2-3 hours

3. **Phase 3** (Low Priority): Lag Alerting
   - Alert if GlueSync stops processing
   - Track lag trends
   - Historical lag reporting
   - **Effort**: 3-4 hours

---

## Conclusion

**GlueSync checkpoints** are a valuable source of information for:
- ✅ Replication lag monitoring
- ✅ Health checking
- ✅ Cache validation
- ✅ Sequence gap detection

**GlueSync database cache** should NOT be used because:
- ❌ Binary format (Chronicle Queue)
- ❌ No Python reader
- ❌ High development cost
- ❌ Better alternatives exist (AS400 + SQLite)

**Best approach**: Hybrid strategy using checkpoints for metadata + AS400 for actual data.

---

**Status**: Analysis Complete  
**Recommendation**: Implement Phase 1 (Checkpoint Reader)  
**Estimated Effort**: 1-2 hours for Phase 1
