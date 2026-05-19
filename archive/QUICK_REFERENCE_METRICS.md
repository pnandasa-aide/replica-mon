# Replica-Mon Quick Reference: Caching & Metrics

## ✅ What's Implemented

### 1. Journal Summary Cache (Fixed!)
**Location**: `.cache/GSLIBTST_CUSTOMERS.meta.json`

**How it works**:
```
Cycle 1 (10:00): Fetch from AS400 → Cache summary counts
Cycle 2 (10:05): Use cache (if < 1 hour old) ✅ FAST!
Cycle 3 (11:05): Cache expired → Re-fetch from AS400
```

**What's cached**:
```json
{
  "summary_inserts": 25433,
  "summary_updates": 6826,
  "summary_deletes": 1175,
  "summary_total": 34559,
  "summary_cached_at": "2026-04-13 22:30:00",
  "cache_level": "summary"
}
```

### 2. File-Based Metrics Storage (New!)
**Location**: `metrics/metrics_YYYY-MM-DD.csv`

**Example CSV**:
```csv
timestamp,source_table,target_table,status,journal_total,ct_total,replication_lag
2026-04-13T22:30:00,GSLIBTST.CUSTOMERS,dbo.CUSTOMERS,✅ OK,34559,34559,0
2026-04-13T22:35:00,GSLIBTST.CUSTOMERS,dbo.CUSTOMERS,❌ MISMATCH,34600,34580,20
```

### 3. Full Journal Storage (For Investigation)
**Location**: `metrics/journals/GSLIBTST_CUSTOMERS_2026-04-13.json`

**When used**: Only when table is flagged with `requires_attention=True`

**What's stored**: Complete journal entry details for root cause analysis

---

## 📊 How to View Metrics

### Option 1: Command Line
```bash
cd /home/ubuntu/_qoder/replica-mon

# View today's metrics
cat metrics/metrics_$(date +%Y-%m-%d).csv

# View summary stats
python3 -c "
from lib.metrics_storage import get_table_summary
print(get_table_summary('GSLIBTST.CUSTOMERS', hours=24))
"

# Export all metrics to JSON
python3 -c "
from lib.metrics_storage import MetricsFileStorage
storage = MetricsFileStorage()
storage.export_to_json()
"
```

### Option 2: Excel/Google Sheets
```bash
# Open CSV in Excel
libreoffice --calc metrics/metrics_2026-04-13.csv

# Or upload to Google Sheets
```

### Option 3: Python Analysis
```python
from lib.metrics_storage import MetricsFileStorage

storage = MetricsFileStorage()

# Get time-series for a table
data = storage.get_time_series('GSLIBTST.CUSTOMERS', hours=24)

# Plot with matplotlib
import matplotlib.pyplot as plt
import pandas as pd

df = pd.DataFrame(data)
df['timestamp'] = pd.to_datetime(df['timestamp'])
df.set_index('timestamp', inplace=True)

df[['journal_total', 'ct_total']].plot()
plt.title('Replication Metrics')
plt.ylabel('Change Count')
plt.show()
```

---

## 🔧 Cache Management

### Check Cache Status
```bash
# View journal cache
cat .cache/GSLIBTST_CUSTOMERS.meta.json

# View CT cache
cat .cache/CT_DBO_CUSTOMERS.meta.json

# List all caches
ls -la .cache/
```

### Clear Cache
```bash
# Clear all caches
rm -rf .cache/*.json

# Clear specific table cache
rm .cache/GSLIBTST_CUSTOMERS.json
rm .cache/GSLIBTST_CUSTOMERS.meta.json
```

### Force Cache Refresh
```bash
# Run monitor with --no-cache flag
python3 monitor.py --no-cache
```

---

## 📈 Understanding the Data

### Time-Series Pattern (What You'll See)

With replication lag, the graph looks like this:

```
Count
  ↑
  │    ╱────── Source (Journal)
  │   ╱    ╱
  │  ╱    ╱    ╱────── Target (CT, lagging behind)
  │ ╱    ╱    ╱
  │╱    ╱    ╱
  └──────────────────→ Time
   10:00 10:05 10:10
```

**Key insight**: Even though absolute numbers differ, the **pattern** (slope, spikes) should match!

### Replication Lag Detection

```csv
timestamp,journal_total,ct_total,replication_lag
10:00:00,34559,34559,0          ← No lag
10:05:00,34600,34580,20         ← 20 changes pending
10:10:00,34650,34645,5          ← Catching up
10:15:00,34700,34700,0          ← Caught up!
```

---

## 🎯 Next Steps (Time-Windowed Monitoring)

**Current behavior**: Cumulative counts (all-time totals)

**Future improvement**: Count ONLY changes in current monitoring interval

### Example of Time-Windowed:
```
Monitor interval: 5 minutes (10:00 - 10:05)

Journal: 41 new changes (not 34,600 total)
CT: 39 new changes (not 34,580 total)
Lag: 2 changes behind in THIS interval
```

**Benefits**:
- ✅ Detects real-time replication lag
- ✅ Shows throughput per interval
- ✅ Pattern matching works perfectly

**To implement**: Need to pass `--from-time` and `--to-time` to qadmcli commands

---

## 🗂️ File Structure

```
replica-mon/
├── .cache/                          # Journal & CT summaries
│   ├── GSLIBTST_CUSTOMERS.json
│   ├── GSLIBTST_CUSTOMERS.meta.json
│   ├── CT_DBO_CUSTOMERS.json
│   └── CT_DBO_CUSTOMERS.meta.json
│
├── metrics/                         # Time-series metrics (NEW!)
│   ├── metrics_2026-04-13.csv
│   ├── metrics_2026-04-14.csv
│   └── journals/                    # Full journal entries (when flagged)
│       ├── GSLIBTST_CUSTOMERS_2026-04-13.json
│       └── GSLIBTST_ORDERS_2026-04-13.json
│
├── lib/
│   ├── journal_cache.py             # Cache management
│   ├── as400_journal.py             # Journal reader (with caching)
│   ├── mssql_ct.py                  # CT reader (with caching)
│   └── metrics_storage.py           # File-based metrics (NEW!)
│
└── monitor.py                       # Main monitoring script
```

---

## 💡 Pro Tips

1. **Monitor grows slowly**: ~1KB per monitoring cycle per table
2. **CSV is universal**: Import to Excel, Google Sheets, Grafana, Python, R
3. **Daily files**: Easy to archive/delete old data
4. **Full journals are optional**: Only saved when problems detected
5. **Cache is fast**: 1-hour validity means minimal AS400 queries

---

## 🚀 Quick Commands

```bash
# Run monitoring (auto-saves metrics)
python3 monitor.py

# Continuous monitoring
python3 monitor.py --continuous --interval 300

# View today's metrics
tail -f metrics/metrics_$(date +%Y-%m-%d).csv

# Get summary for last 24 hours
python3 -c "from lib.metrics_storage import get_table_summary; print(get_table_summary('GSLIBTST.CUSTOMERS'))"

# Export to JSON for analysis
python3 -c "from lib.metrics_storage import MetricsFileStorage; MetricsFileStorage().export_to_json()"

# Check cache status
cat .cache/GSLIBTST_CUSTOMERS.meta.json | python3 -m json.tool
```
