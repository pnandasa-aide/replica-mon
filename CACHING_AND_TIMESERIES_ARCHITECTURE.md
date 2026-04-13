# Replica-Mon Caching & Time-Series Architecture

## 1. Current Caching Status & Issues

### ✅ What's Implemented:
- **Journal Summary Cache**: `.cache/GSLIBTST_CUSTOMERS.json` (summary counts only)
- **Journal Full Cache**: Can store all entries (for investigation)
- **CT Summary Cache**: `.cache/CT_DBO_CUSTOMERS.json`
- **Tiered Cache Levels**: "summary" (default) vs "full" (on discrepancy)
- **Auto-Flagging**: Tables with mismatches marked `requires_attention=True`

### ❌ Current Issues:

#### Issue 1: Summary Cache Not Working
**Problem**: Cache is being saved but not properly reused by monitor.py

**Location**: `monitor.py` → `get_entity_comparison()` calls `AS400JournalReader.get_summary()`

**Expected Flow**:
```
1. Check cache for existing entries
2. If cache exists, only fetch new entries since last_timestamp
3. Merge new entries with cache
4. Return summary from merged data
```

**Actual Flow**: 
- Cache is saved but monitor.py doesn't check it before querying
- Every monitor cycle re-fetches all journal entries

#### Issue 2: CT Cache Not Verified
**Problem**: No code to verify if CT cache is being used

**CT Cache Location**: `.cache/CT_DBO_*.json`

**Check Command**:
```bash
ls -la .cache/CT_*.json
cat .cache/CT_DBO_CUSTOMERS.json
```

#### Issue 3: No Time-Windowed Comparison
**Problem**: Monitor compares ALL historical data, not current interval

**Current Behavior**:
```
Cycle 1 (10:00): Count all entries from beginning → Journal: 34559, CT: 1
Cycle 2 (10:05): Count all entries from beginning → Journal: 34600, CT: 50
```

**Expected Behavior**:
```
Cycle 1 (10:00-10:05): Count entries in this 5-min window → Journal: 41, CT: 49
Cycle 2 (10:05-10:10): Count entries in this 5-min window → Journal: 38, CT: 40
```

---

## 2. Recommended Fix: Time-Windowed Monitoring

### Approach A: Interval-Based Counting (Recommended)

**Concept**: Count changes ONLY within each monitoring interval

```python
def monitor_entity_with_window(source_table, target_table, interval_start, interval_end):
    """
    Monitor changes within a specific time window.
    
    This detects replication lag and throughput per interval.
    """
    
    # AS400: Count journal entries in this window
    journal_summary = get_journal_summary(
        table=source_table,
        from_time=interval_start,
        to_time=interval_end
    )
    
    # MSSQL: Count CT changes in this window
    ct_summary = get_ct_summary(
        table=target_table,
        from_time=interval_start,  # Adjusted for timezone
        to_time=interval_end
    )
    
    # Compare interval counts (not cumulative)
    return {
        'interval_start': interval_start,
        'interval_end': interval_end,
        'journal_count': journal_summary['total'],
        'ct_count': ct_summary['total'],
        'lag': journal_summary['total'] - ct_summary['total']
    }
```

**Benefits**:
- ✅ Detects real-time replication lag
- ✅ Shows throughput per interval
- ✅ Pattern matching works (both should spike/drop together)
- ✅ No cumulative count issues

### Approach B: Cumulative with Delta Calculation

**Concept**: Store cumulative counts, calculate delta between intervals

```python
def monitor_with_deltas():
    # Current cumulative counts
    current_journal = get_total_journal_count()  # e.g., 34600
    current_ct = get_total_ct_count()            # e.g., 50
    
    # Previous cumulative counts (from cache)
    prev_journal = cache.get('journal_total')    # e.g., 34559
    prev_ct = cache.get('ct_total')              # e.g., 1
    
    # Calculate delta for this interval
    delta_journal = current_journal - prev_journal  # 41 new entries
    delta_ct = current_ct - prev_ct                 # 49 new changes
    
    # Compare deltas
    return {
        'interval_changes': {
            'journal': delta_journal,
            'ct': delta_ct,
            'lag': delta_journal - delta_ct
        }
    }
```

**Benefits**:
- ✅ Works with existing qadmcli commands
- ✅ Handles replication lag gracefully
- ✅ Easier to implement

---

## 3. Time-Series Data Storage for Future Dashboard

### Recommended Architecture: **TimescaleDB + PostgreSQL**

#### Why TimescaleDB?
- ✅ Built on PostgreSQL (familiar, reliable)
- ✅ Optimized for time-series data
- ✅ Automatic partitioning by time
- ✅ Efficient aggregation queries
- ✅ Native support for Grafana, ELK-style dashboards
- ✅ Scales to billions of rows

#### Alternative Options:

| Database | Pros | Cons | Best For |
|----------|------|------|----------|
| **TimescaleDB** | SQL, aggregations, Grafana | Requires PostgreSQL | **Recommended** |
| **InfluxDB** | Purpose-built for metrics | Limited SQL, custom query language | Pure metrics |
| **Prometheus** | Great for alerting | Short retention, not for history | Real-time alerting |
| **Elasticsearch** | Full-text search, Kibana | Heavy, complex | Log analysis |
| **SQLite** | Simple, embedded | No time-series features | Local dev only |

### Schema Design for TimescaleDB

```sql
-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Main metrics table
CREATE TABLE replication_metrics (
    time TIMESTAMPTZ NOT NULL,
    source_table TEXT NOT NULL,
    target_table TEXT NOT NULL,
    pipeline_id TEXT,
    
    -- Journal counts (AS400)
    journal_inserts INTEGER DEFAULT 0,
    journal_updates INTEGER DEFAULT 0,
    journal_deletes INTEGER DEFAULT 0,
    journal_total INTEGER DEFAULT 0,
    
    -- CT counts (MSSQL)
    ct_inserts INTEGER DEFAULT 0,
    ct_updates INTEGER DEFAULT 0,
    ct_deletes INTEGER DEFAULT 0,
    ct_total INTEGER DEFAULT 0,
    
    -- Derived metrics
    replication_lag INTEGER,  -- journal_total - ct_total
    status TEXT,              -- 'OK', 'MISMATCH', 'PREREQ_FAILED'
    
    -- Metadata
    monitoring_interval INTEGER,  -- seconds (e.g., 300)
    cache_status TEXT             -- 'hit', 'miss', 'partial'
);

-- Convert to hypertable (TimescaleDB magic)
SELECT create_hypertable('replication_metrics', 'time');

-- Add indexes for common queries
CREATE INDEX idx_source_table ON replication_metrics(source_table, time DESC);
CREATE INDEX idx_status ON replication_metrics(status, time DESC);

-- Create continuous aggregates (auto-materialized views)
CREATE MATERIALIZED VIEW metrics_1min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    source_table,
    target_table,
    SUM(journal_inserts) AS journal_inserts,
    SUM(journal_updates) AS journal_updates,
    SUM(journal_deletes) AS journal_deletes,
    SUM(journal_total) AS journal_total,
    SUM(ct_inserts) AS ct_inserts,
    SUM(ct_total) AS ct_total,
    AVG(replication_lag) AS avg_lag
FROM replication_metrics
GROUP BY bucket, source_table, target_table;

-- Similar for 5min, 1hr, 1day aggregates
```

### Data Ingestion Pattern

```python
# monitor.py - After each monitoring cycle
def save_metrics_to_timeseries(results):
    """Save monitoring results to TimescaleDB."""
    
    conn = psycopg2.connect("dbname=replica_mon")
    cursor = conn.cursor()
    
    for result in results:
        cursor.execute("""
            INSERT INTO replication_metrics (
                time, source_table, target_table,
                journal_inserts, journal_updates, journal_deletes, journal_total,
                ct_inserts, ct_updates, ct_deletes, ct_total,
                replication_lag, status, monitoring_interval
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            datetime.now(),
            result['source_table'],
            result['target_table'],
            result.get('journal_inserts', 0),
            result.get('journal_updates', 0),
            result.get('journal_deletes', 0),
            result.get('journal_total', 0),
            result.get('ct_inserts', 0),
            result.get('ct_updates', 0),
            result.get('ct_deletes', 0),
            result.get('ct_total', 0),
            result.get('replication_lag', 0),
            result.get('status', 'UNKNOWN'),
            result.get('interval', 300)
        ))
    
    conn.commit()
```

### Dashboard Queries (Grafana/SQL)

#### 1. Time-Series Graph: Operations by Type
```sql
-- Show inserts/updates/deletes over time (last 24 hours)
SELECT
    time_bucket('5 minutes', time) AS period,
    SUM(journal_inserts) AS inserts,
    SUM(journal_updates) AS updates,
    SUM(journal_deletes) AS deletes
FROM replication_metrics
WHERE source_table = 'GSLIBTST.CUSTOMERS'
  AND time > NOW() - INTERVAL '24 hours'
GROUP BY period
ORDER BY period;
```

#### 2. Replication Lag Over Time
```sql
SELECT
    time_bucket('1 minute', time) AS period,
    AVG(replication_lag) AS avg_lag,
    MAX(replication_lag) AS max_lag
FROM replication_metrics
WHERE source_table = 'GSLIBTST.CUSTOMERS'
  AND time > NOW() - INTERVAL '1 hour'
GROUP BY period
ORDER BY period;
```

#### 3. Compare Source vs Target Patterns
```sql
SELECT
    time_bucket('10 minutes', time) AS period,
    SUM(journal_total) AS source_changes,
    SUM(ct_total) AS target_changes
FROM replication_metrics
WHERE source_table = 'GSLIBTST.CUSTOMERS'
  AND time > NOW() - INTERVAL '6 hours'
GROUP BY period
ORDER BY period;
```

#### 4. Table Comparison Dashboard
```sql
-- Show all tables with their latest status
SELECT DISTINCT ON (source_table)
    source_table,
    target_table,
    status,
    journal_total,
    ct_total,
    replication_lag,
    time AS last_check
FROM replication_metrics
WHERE time > NOW() - INTERVAL '1 hour'
ORDER BY source_table, time DESC;
```

---

## 4. Lightweight Alternative: SQLite + CSV

If TimescaleDB is overkill for now, start simple:

### Option A: SQLite with Time-Series Table

```python
import sqlite3
from datetime import datetime

class MetricsDB:
    def __init__(self, db_path="metrics.db"):
        self.conn = sqlite3.connect(db_path)
        self.create_tables()
    
    def create_tables(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS metrics (
                time TEXT NOT NULL,
                source_table TEXT NOT NULL,
                target_table TEXT NOT NULL,
                journal_total INTEGER,
                ct_total INTEGER,
                replication_lag INTEGER,
                status TEXT
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_metrics_time 
            ON metrics(time)
        """)
        self.conn.commit()
    
    def save_metrics(self, results):
        for result in results:
            self.conn.execute("""
                INSERT INTO metrics (
                    time, source_table, target_table,
                    journal_total, ct_total, replication_lag, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                result['source_table'],
                result['target_table'],
                result.get('journal_total', 0),
                result.get('ct_total', 0),
                result.get('journal_total', 0) - result.get('ct_total', 0),
                result.get('status', 'UNKNOWN')
            ))
        self.conn.commit()
    
    def get_time_series(self, table, hours=24):
        """Get time-series data for a table."""
        cursor = self.conn.execute("""
            SELECT time, journal_total, ct_total, replication_lag, status
            FROM metrics
            WHERE source_table = ?
              AND time > datetime('now', '-{} hours')
            ORDER BY time
        """.format(hours), (table,))
        return cursor.fetchall()
```

### Option B: CSV Files (Simplest)

```python
import csv
from pathlib import Path
from datetime import datetime

class MetricsCSV:
    def __init__(self, metrics_dir="metrics"):
        self.metrics_dir = Path(metrics_dir)
        self.metrics_dir.mkdir(exist_ok=True)
    
    def save_metrics(self, results, timestamp=None):
        timestamp = timestamp or datetime.now()
        filename = f"metrics_{timestamp.strftime('%Y-%m-%d')}.csv"
        filepath = self.metrics_dir / filename
        
        file_exists = filepath.exists()
        
        with open(filepath, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'timestamp', 'source_table', 'target_table',
                'journal_inserts', 'journal_updates', 'journal_deletes', 'journal_total',
                'ct_inserts', 'ct_updates', 'ct_deletes', 'ct_total',
                'replication_lag', 'status'
            ])
            
            if not file_exists:
                writer.writeheader()
            
            for result in results:
                writer.writerow({
                    'timestamp': timestamp.isoformat(),
                    'source_table': result['source_table'],
                    'target_table': result['target_table'],
                    'journal_inserts': result.get('journal_inserts', 0),
                    'journal_updates': result.get('journal_updates', 0),
                    'journal_deletes': result.get('journal_deletes', 0),
                    'journal_total': result.get('journal_total', 0),
                    'ct_inserts': result.get('ct_inserts', 0),
                    'ct_updates': result.get('ct_updates', 0),
                    'ct_deletes': result.get('ct_deletes', 0),
                    'ct_total': result.get('ct_total', 0),
                    'replication_lag': result.get('journal_total', 0) - result.get('ct_total', 0),
                    'status': result.get('status', 'UNKNOWN')
                })
```

---

## 5. Recommended Implementation Phases

### Phase 1: Fix Current Caching (1-2 days)
- [ ] Fix summary cache reuse in monitor.py
- [ ] Add time-windowed queries (from_time, to_time)
- [ ] Verify CT cache is working
- [ ] Add delta calculation between intervals

### Phase 2: SQLite Metrics Storage (1 day)
- [ ] Add MetricsDB class
- [ ] Save results after each monitor cycle
- [ ] Add basic time-series queries
- [ ] Export to CSV for analysis

### Phase 3: Grafana Dashboard (2-3 days)
- [ ] Migrate to TimescaleDB (or keep SQLite)
- [ ] Install Grafana
- [ ] Create dashboards:
  - Real-time replication lag
  - Operations by type (insert/update/delete)
  - Table comparison view
  - Alerting on high lag

### Phase 4: Advanced Features (Future)
- [ ] Auto-scaling monitoring intervals based on activity
- [ ] Predictive lag detection
- [ ] Anomaly detection (sudden spikes/drops)
- [ ] Automated root cause analysis

---

## 6. Immediate Next Steps

### To Fix Caching NOW:

1. **Check if cache files exist**:
```bash
ls -la .cache/
cat .cache/GSLIBTST_CUSTOMERS.meta.json
cat .cache/CT_DBO_CUSTOMERS.meta.json
```

2. **Verify monitor.py uses cache**:
```bash
grep -n "use_cache" monitor.py
grep -n "load_cache" monitor.py
```

3. **Add time-windowed parameters to qadmcli calls**:
```python
# Instead of:
journal_reader.get_summary(source_table)

# Use:
journal_reader.get_summary(
    source_table,
    since=interval_start,  # e.g., "2026-04-13 22:00:00"
    until=interval_end     # e.g., "2026-04-13 22:05:00"
)
```

### Recommended Storage Choice:

**Start with**: SQLite (simple, embedded, SQL queries)
**Migrate to**: TimescaleDB when you need:
- Multi-user dashboard access
- Long-term retention (>6 months)
- Grafana integration
- High query performance on large datasets

Would you like me to implement any of these fixes or the metrics storage system?
