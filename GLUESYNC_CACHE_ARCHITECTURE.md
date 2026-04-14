# GlueSync Agent Cache Architecture Analysis

## Overview

GlueSync uses a **hybrid caching strategy** for agents:
- **Journal checkpoints**: JSON files for tracking position
- **Database cache**: Binary Chronicle Queue files for storing actual data

---

## Directory Structure

```
agents/
├── {agent_id}/                    # e.g., dfb34af1 (SOURCE agent)
│   ├── bootstrap.json             # Agent identification
│   ├── journal-checkpoints/       # Position tracking (JSON)
│   │   └── {library}/
│   │       ├── {journal1}.cp      # Checkpoint file
│   │       └── {journal2}.cp
│   └── database-cache/            # Actual data storage (Binary)
│       └── {hash_id}/
│           └── {numeric_id}/
│               ├── metadata.cq4t  # Chronicle Queue metadata
│               ├── 20260413-04F.cq4  # Daily binary data file (80MB each)
│               ├── 20260413-05F.cq4
│               └── ...
└── {agent_id}/                    # e.g., 063c551e (TARGET agent)
    └── bootstrap.json             # Only config, no cache
```

---

## 1. Journal Checkpoints (`.cp` files)

### Purpose
Track the **current position** in the AS400 journal so GlueSync can resume from where it left off.

### Format: JSON
```json
{
  "journalLibrary": "GSLIBTST",
  "journalName": "CUSTJRN",
  "receiverLibrary": "GSLIBTST",
  "receiverName": "CUSTRC0001",
  "sequenceNumber": "00000000000000035037",
  "timestamp": 1776136218858
}
```

### Key Fields
- **sequenceNumber**: The journal entry number (35037 in this case)
- **timestamp**: Unix timestamp in milliseconds (1776136218858 = Apr 14, 2026)
- **receiverName**: Current journal receiver being read

### Why JSON?
✅ **Human-readable** - Easy to debug and inspect  
✅ **Simple structure** - Only metadata, no binary data  
✅ **Fast parsing** - Small file size (< 200 bytes)  
✅ **Safe to edit** - Can manually adjust position if needed  

---

## 2. Database Cache (`.cq4` files)

### Purpose
Store the **actual journal entry data** (including binary raw_data fields) in a high-performance queue.

### Format: Chronicle Queue (Binary)
- **File extension**: `.cq4` (Chronicle Queue v4)
- **File size**: 80MB per day (fixed allocation)
- **Naming**: `YYYYMMDD-{sequence}F.cq4`
- **Type**: Binary data file

### What is Chronicle Queue?
Chronicle Queue is a **Java library** for high-performance, low-latency message queues:
- **Binary format** - Not human-readable
- **Memory-mapped** - Fast I/O via mmap
- **Append-only** - Optimized for sequential writes
- **Zero GC** - No garbage collection overhead
- **Persistence** - Survives restarts

### Why Binary (Chronicle Queue)?
✅ **Handles binary data** - Can store raw `\u0000` bytes, images, etc.  
✅ **No JSON parsing issues** - Binary data doesn't need escaping  
✅ **High performance** - Memory-mapped files, zero-copy  
✅ **Streaming** - Can read entries sequentially without loading all  
✅ **Compact** - More efficient than JSON for large data  
✅ **Type-safe** - Preserves data types exactly  

### Directory Structure Explained
```
database-cache/
└── 2f7b032d/                    # Hash (likely entity/table ID)
    └── -7840468056302474202/    # Numeric ID (likely queue partition)
        ├── metadata.cq4t        # Queue metadata
        ├── 20260412-16F.cq4     # April 12, 2026 data
        ├── 20260413-04F.cq4     # April 13, 2026 data
        └── 20260414-01F.cq4     # April 14, 2026 data
```

Each `.cq4` file represents **one day of data** for a specific entity/table.

---

## 3. Bootstrap Files

### Purpose
Agent identification and configuration.

### Format: JSON
```json
{
  "agentId": "dfb34af1",
  "agentUserTag": "ship-at-scale-ibm-iseries"
}
```

### Why JSON?
✅ Configuration data (not binary)  
✅ Readable for debugging  
✅ Small size  

---

## Comparison: JSON vs Binary for Different Use Cases

| Use Case | Format | Why |
|----------|--------|-----|
| **Checkpoints/Metadata** | JSON (.cp) | ✅ Simple, readable, small |
| **Journal entry data** | Binary (.cq4) | ✅ Handles raw bytes, fast, compact |
| **Agent config** | JSON (.json) | ✅ Human-readable, editable |
| **Large binary fields** | Binary (.cq4) | ✅ No escaping needed, efficient |

---

## Lessons for replica-mon Caching

### Current Approach: JSON Cache
```python
# replica-mon currently stores journal entries as JSON
cache_file = "cache/journal_GSLIBTST_CUSTOMERS.json"
{
  "entries": [
    {
      "entry_number": 35037,
      "after_image": {
        "raw_data": "\u0000\u0000\u0004:..."  # ← PROBLEM!
      }
    }
  ]
}
```

### Problems with JSON for Binary Data
❌ **Parse errors**: `\u0000` and special chars break JSON parsing  
❌ **Large size**: Escaping binary data inflates size  
❌ **Slow**: Parsing large JSON files is CPU-intensive  
❌ **Fragile**: One bad character breaks the entire file  

### Recommended: Hybrid Approach (like GlueSync)

#### Option 1: Binary + JSON Metadata
```python
# Metadata (JSON) - position tracking only
cache/metadata_GSLIBTST_CUSTOMERS.json
{
  "last_sequence": 35037,
  "last_timestamp": "2026-04-14 01:34:20",
  "entry_count": 100,
  "data_file": "cache/data_GSLIBTST_CUSTOMERS.bin"
}

# Data (Binary) - actual entries
cache/data_GSLIBTST_CUSTOMERS.bin
[Entry 1][Entry 2][Entry 3]...  # Binary serialized
```

#### Option 2: SQLite Database
```python
# Single file database
cache/replica_mon.db

Tables:
- journal_entries (id, sequence, timestamp, table, raw_data BLOB)
- ct_changes (version, timestamp, table, operation, raw_data BLOB)
- cache_metadata (table, last_sequence, last_timestamp, entry_count)
```

**Benefits**:
✅ **BLOB support** - Store binary data natively  
✅ **Fast queries** - Indexed lookups  
✅ **No parse errors** - Binary stored as-is  
✅ **Incremental** - Add entries without rewriting file  
✅ **Compact** - Better compression than JSON  

#### Option 3: Pickle (Python-specific)
```python
import pickle

# Serialize Python objects directly
with open('cache/journal_GSLIBTST_CUSTOMERS.pkl', 'wb') as f:
    pickle.dump(entries, f)
```

**Benefits**:
✅ **Handles any Python object** - Including bytes  
✅ **Fast** - No JSON encoding/decoding  
✅ **Preserves types** - datetime, bytes, etc.  

**Drawbacks**:
❌ **Python-only** - Can't read from other languages  
❌ **Security risk** - Unpickling untrusted data is dangerous  

---

## Recommendation for replica-mon

### Best Solution: **SQLite with BLOB columns**

```python
import sqlite3
from datetime import datetime

class JournalCache:
    def __init__(self, cache_dir: str):
        self.db_path = f"{cache_dir}/journal_cache.db"
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS journal_entries (
                    id INTEGER PRIMARY KEY,
                    table_name TEXT NOT NULL,
                    entry_number INTEGER NOT NULL,
                    entry_timestamp TEXT NOT NULL,
                    entry_type TEXT,
                    raw_data BLOB,  # ← Binary data stored directly!
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(table_name, entry_number)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache_metadata (
                    table_name TEXT PRIMARY KEY,
                    last_sequence INTEGER,
                    last_timestamp TEXT,
                    entry_count INTEGER,
                    updated_at TEXT
                )
            """)
    
    def store_entries(self, table: str, entries: list):
        with sqlite3.connect(self.db_path) as conn:
            for entry in entries:
                # Convert raw_data to bytes if it's a string
                raw_data = entry.get('after_image', {}).get('raw_data')
                if isinstance(raw_data, str):
                    raw_data = raw_data.encode('utf-8', errors='ignore')
                
                conn.execute("""
                    INSERT OR REPLACE INTO journal_entries 
                    (table_name, entry_number, entry_timestamp, entry_type, raw_data)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    table,
                    entry['entry_number'],
                    entry['entry_timestamp'],
                    entry['entry_type'],
                    raw_data
                ))
            
            # Update metadata
            conn.execute("""
                INSERT OR REPLACE INTO cache_metadata 
                (table_name, last_sequence, last_timestamp, entry_count, updated_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                table,
                entries[-1]['entry_number'],
                entries[-1]['entry_timestamp'],
                len(entries),
                datetime.now().isoformat()
            ))
```

### Benefits for replica-mon
✅ **No more JSON parse errors** - Binary stored as BLOB  
✅ **Faster** - SQLite queries vs JSON parsing  
✅ **Incremental** - Add entries without rewriting entire file  
✅ **Compact** - Better storage efficiency  
✅ **Queryable** - Can filter by timestamp, sequence, etc.  
✅ **Safe** - One bad entry doesn't corrupt entire cache  

---

## Summary

### How GlueSync Does It:
1. **Metadata/Checkpoints**: JSON files (small, readable)
2. **Actual Data**: Binary Chronicle Queue (fast, handles binary)
3. **Separation of concerns**: Different formats for different purposes

### What replica-mon Should Do:
1. **Stop storing binary data in JSON** - Use SQLite or binary format
2. **Keep metadata in JSON or SQLite** - Position tracking, counts, etc.
3. **Store raw_data as BLOB** - No escaping, no parse errors
4. **Incremental updates** - Don't rewrite entire cache file

### Priority:
🔴 **HIGH**: Fix JSON parse errors by using BLOB storage  
🟡 **MEDIUM**: Improve cache performance with SQLite  
🟢 **LOW**: Add compression for large binary fields  

---

## Next Steps

1. **Short-term**: Add try-catch around JSON parser to handle binary data better
2. **Medium-term**: Migrate to SQLite with BLOB columns
3. **Long-term**: Consider Chronicle Queue (if Java interop needed) or keep SQLite

Would you like me to implement the SQLite-based cache for replica-mon?
