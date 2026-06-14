# ReplicaMon Comprehensive Architecture Document

**Version:** v0.5.1  
**Date:** 2026-04-07  
**Status:** Authoritative Reference

---

## 1. System Overview

ReplicaMon is a **dual-mode** replication monitoring and reconciliation tool for GlueSync deployments. It provides:

1. **Real-time Web Dashboard** - Live monitoring via WebSocket metrics
2. **CLI Tools** - Deep record-level comparison and batch monitoring

### 1.1 Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          REPLICA-MON ECOSYSTEM                          │
│                                                                         │
│  ┌──────────────────────────────┐    ┌──────────────────────────────┐  │
│  │   Mode 1: Web Dashboard      │    │   Mode 2: CLI Tools          │  │
│  │   (Always Running)           │    │   (Ad-hoc Execution)         │  │
│  │                              │    │                              │  │
│  │  FastAPI Backend :8000       │    │  ./replica-mon.sh            │  │
│  │  WebSocket Server            │    │  compare.py (FIX NEEDED)     │  │
│  │  REST API                    │    │  monitor.py (FIX NEEDED)     │  │
│  │  Verify Tool                 │    │                              │  │
│  └────────┬─────────────────────┘    └────────┬─────────────────────┘  │
│           │                                    │                        │
│           │ podman-compose up -d               │ podman run --rm        │
│           ▼                                    ▼                        │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │              replica-mon Container (Podman)                     │   │
│  │                                                                 │   │
│  │  Components:                                                    │   │
│  │  ┌─────────────────┐  ┌──────────────────┐  ┌───────────────┐ │   │
│  │  │  Backend Server │  │  Monitoring      │  │  Shared       │ │   │
│  │  │  (main.py)      │  │  Engine          │  │  Libraries    │ │   │
│  │  │                 │  │  (monitor.py)    │  │  (lib/)       │ │   │
│  │  │  • REST API     │  │  • Entity disc.  │  │  • AS400      │ │   │
│  │  │  • WebSocket    │  │  • Scheduled     │  │    journal    │ │   │
│  │  │  • Verify       │  │  • Cache update  │  │  • MSSQL CT   │ │   │
│  │  │  • GlueSync SDK │  │  • Prereq check  │  │  • Comparator │ │   │
│  │  └────────┬────────┘  └────────┬─────────┘  └───────┬───────┘ │   │
│  │           │                    │                     │         │   │
│  │           └────────────────────┴─────────────────────┘         │   │
│  │                                                                 │   │
│  │  Volumes:                                                       │   │
│  │  • ./cache:/app/replica-mon/cache:Z (journal/CT cache)         │   │
│  │  • ./metrics:/app/replica-mon/metrics:Z (WebSocket metrics)    │   │
│  │  • ../qadmcli/config:/app/qadmcli/config:Z (AS400 config)      │   │
│  │                                                                 │   │
│  │  Network: host (accesses localhost:1717, localhost:9090)       │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│           │                              │                              │
│           │ GlueSync API                 │ Direct DB                    │
│           │ (WebSocket + REST)           │ (JayDeBeApi + pyodbc)        │
│           ▼                              ▼                              │
│  ┌────────────────────┐      ┌────────────────────┐                    │
│  │  GlueSync Core Hub │      │  AS400 (Source)    │                    │
│  │  :1717             │      │  161.82.146.249    │                    │
│  │                    │      │                    │                    │
│  │  • Entity metadata │      │  • Journal entries │                    │
│  │  • Real-time ops   │      │  • Table data      │                    │
│  │  • Pipeline config │      │  • PK metadata     │                    │
│  └────────────────────┘      └────────────────────┘                    │
│                                │                                        │
│                                │ Replication                            │
│                                ▼                                        │
│                       ┌────────────────────┐                           │
│                       │  MSSQL (Target)    │                           │
│                       │  192.168.13.62     │                           │
│                       │                    │                           │
│                       │  • Change Tracking │                           │
│                       │  • Target tables   │                           │
│                       └────────────────────┘                           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Breakdown

### 2.1 Dashboard Backend (`backend/main.py`)

**Role:** Web server and API gateway  
**Technology:** FastAPI + Uvicorn  
**Port:** 8000  
**Network:** Host mode (accesses GlueSync at localhost:1717)

#### Sub-Components:

##### 2.1.1 REST API Endpoints

| Endpoint | Method | Purpose | Status |
|----------|--------|---------|--------|
| `/api/pipelines` | GET | List all GlueSync pipelines | ✅ Working |
| `/api/pipelines/{id}/entities` | GET | List entities in pipeline | ✅ Working |
| `/api/verify/{id}/run` | POST | Start verification job | ✅ Working |
| `/api/verify/{id}/results` | GET | Get verification results | ✅ Working |
| `/api/pipelines/{id}/entities/{eid}/start` | POST | Start entity | ✅ Working |
| `/api/pipelines/{id}/entities/{eid}/stop` | POST | Stop entity | ✅ Working |

**Missing Endpoints (Planned):**
- `/api/verify/{id}/entity/{eid}/primary-keys` - Get PK metadata from GlueSync
- `/api/verify/{id}/entity/{eid}/compare-records` - Record-level comparison
- `/api/verify/{id}/entity/{eid}/sample-records` - Get sample records for UI

##### 2.1.2 WebSocket Server

**Endpoint:** `/ws/metrics?pipeline_id={id}&entities={list}`

**Function:**
- Connects to GlueSync WebSocket (`wss://localhost:1717/ws/metrics`)
- Receives real-time replication metrics (INSERT/UPDATE/DELETE counts)
- Stores metrics in SQLite time-series database (`metrics/ws_metrics.db`)
- Broadcasts metrics to connected browser clients
- Supports multiple concurrent WebSocket connections

**Metrics Stored:**
```sql
CREATE TABLE ws_metrics (
    timestamp TEXT,
    entity_name TEXT,
    operation TEXT,      -- 'insert', 'update', 'delete'
    count INTEGER,
    pipeline_id TEXT
);
```

##### 2.1.3 Verify Tool (Current Implementation)

**Location:** Lines 166-340 in `backend/main.py`

**Current Capabilities:**
```python
def _count_as400(library, table):
    """Count total rows in AS400 source table"""
    # Uses: qadmcli AS400ConnectionManager (JayDeBeApi)
    # Query: SELECT COUNT(*) FROM {library}.{table}
    # Returns: (count, None)

def _count_mssql(schema, table):
    """Count total rows in MSSQL target table"""
    # Uses: pyodbc
    # Query: SELECT COUNT(*) FROM {schema}.{table}
    # Returns: (count, last_timestamp)
```

**Execution Flow:**
1. User clicks "Run Verification" in UI
2. Frontend calls `POST /api/verify/{pipeline_id}/run`
3. Backend spawns `_verify_worker()` background thread
4. For each entity:
   - Call `_count_as400()` → gets source row count
   - Call `_count_mssql()` → gets target row count
   - Calculate difference
5. Results stored in `_verify_jobs` dict (in-memory)
6. Frontend polls `GET /api/verify/{pipeline_id}/results` for progress

**Limitations:**
- ❌ Only counts total rows (no record-level comparison)
- ❌ Cannot identify missing records by primary key
- ❌ Cannot compare column values
- ❌ In-memory job storage (lost on restart)

##### 2.1.4 GlueSync Client Integration

**Location:** `replica_msdk/client.py`

**Used APIs:**
```python
client = GlueSyncClient(
    base_url=os.getenv("GLUESYNC_HOST", "https://localhost:1717"),
    username=os.getenv("GLUESYNC_ADMIN_USERNAME"),
    password=os.getenv("GLUESYNC_ADMIN_PASSWORD")
)

# Used by dashboard:
client.list_pipelines()           # GET /pipelines
client.list_entities(pipeline_id) # GET /pipelines/{id}/entities
client.start_entity(...)          # POST /pipelines/{id}/commands/sync/start
client.stop_entity(...)           # POST /pipelines/{id}/commands/sync/stop
```

**Available but NOT Used:**
```python
client.get_discovery_columns(pipeline_id, agent_id, schema, table)
# Returns: {"columns": [...], "keys": [...]}  ← PRIMARY KEYS!
```

### 2.2 Monitoring Engine (`monitor.py`)

**Role:** Automated entity monitoring with caching  
**Current Status:** ❌ **NOT ACCESSIBLE** from container (needs fix)

#### Original Design:

**Execution Modes:**
```bash
# Single check
./replica-mon.sh python3 monitor.py

# Continuous monitoring
./replica-mon.sh python3 monitor.py --continuous --interval 300
```

**Workflow:**
1. **Auto-discovery**: Query GlueSync for active entities via `replica-cli`
2. **For each entity:**
   - Query AS400 journal (with caching)
   - Query MSSQL Change Tracking
   - Compare operation counts
   - Update SQLite cache
3. **Display results**: Table format or JSON

**Caching Mechanism:**
```python
# lib/journal_cache.py
class JournalCache:
    """SQLite-based journal entry cache"""
    
    def get_cached_entries(self, table, since_sequence):
        # Returns cached journal entries from SQLite
        
    def cache_entries(self, table, entries):
        # Stores journal entries in SQLite
        
    def get_summary(self, table, since):
        # Returns aggregated counts (INSERT/UPDATE/DELETE)
```

**Cache Update Strategy:**
- **First run**: Query AS400 (slow, ~60 sec), store in cache
- **Subsequent runs**: Use cache + incremental updates (fast, ~1 sec)
- **Discrepancy detected**: Flag table for manual review
- **Manual reset**: Clear cache and re-query

#### Current Problem:

**Monitor.py is NOT running in the dashboard container!**

- Container runs only `uvicorn main:app` (FastAPI backend)
- `monitor.py` is in the container but never executed
- **No background process updates the journal cache**
- WebSocket metrics are separate from journal cache

**Question:** Who updates the journal cache now?

**Answer:** **NOBODY** - The cache is stale unless manually run via CLI (which is broken).

**Solution Needed:**
- Option A: Run `monitor.py` as background process in dashboard container
- Option B: Integrate monitoring logic into FastAPI backend (background task)
- Option C: Separate monitoring container that shares volumes with dashboard

### 2.3 Shared Libraries (`lib/`)

**Location:** `/app/replica-mon/lib/`

#### Key Modules:

| Module | Purpose | Used By | Status |
|--------|---------|---------|--------|
| `as400_journal.py` | Query AS400 DISPLAY_JOURNAL | compare.py, monitor.py | ✅ In container |
| `mssql_ct.py` | Query MSSQL CHANGETABLE() | compare.py, monitor.py | ✅ In container |
| `comparator.py` | Compare journal vs CT | compare.py, monitor.py | ✅ In container |
| `journal_cache.py` | SQLite caching | compare.py, monitor.py | ✅ In container |
| `timezone.py` | Timezone normalization | compare.py | ✅ In container |
| `connection.py` | AS400ConnectionManager | backend/main.py | ✅ In container |

#### AS400ConnectionManager (Used by Dashboard):

```python
# From qadmcli.db.connection
class AS400ConnectionManager:
    """JayDeBeApi connection to AS400 via JT400"""
    
    def __init__(self, config):
        # Loads connection.yaml
        # Initializes JT400 JDBC connection
        
    def execute(self, sql):
        # Returns cursor with results
        
    def __enter__(self):
        # Context manager
        
    def __exit__(self, ...):
        # Close connection
```

**Used by:** `backend/main.py` → `_count_as400()` function

**NOT Used by:** `monitor.py`, `compare.py` (they use `AS400JournalReader` instead)

### 2.4 CLI Tools (`compare.py`, `monitor.py`)

**Current Status:** ❌ **BROKEN** - Cannot execute via `replica-mon.sh`

#### Why Broken:

**Old Containerfile (v0.4):**
```dockerfile
ENTRYPOINT ["python3"]  # Allows: podman run image compare.py
```

**New Containerfile (v0.5):**
```dockerfile
ENTRYPOINT ["uvicorn", "main:app", "--app-dir", "/app/replica-mon/backend", ...]
# Hardcoded to FastAPI!
```

**replica-mon.sh tries to override:**
```bash
podman run --entrypoint python3 ... "$IMAGE_NAME" "$@"
# But Containerfile doesn't expect this!
```

**Result:**
```bash
./replica-mon.sh python3 compare.py ...
# Becomes: uvicorn main:app compare.py ...  ← WRONG!
```

#### Fix Required:

**Option 1: Update Containerfile** (Recommended)
```dockerfile
# Remove ENTRYPOINT, use CMD instead
CMD ["uvicorn", "main:app", "--app-dir", "/app/replica-mon/backend", "--host", "0.0.0.0", "--port", "8000"]
```

Then `replica-mon.sh` can override with `--entrypoint python3`.

**Option 2: Update replica-mon.sh**
```bash
# Don't use podman run, use podman exec on running container
podman exec replica-mon python3 /app/replica-mon/compare.py "$@"
```

**Option 3: Separate CLI container**
```yaml
# podman-compose.yaml
services:
  dashboard:
    ...
  cli:
    image: localhost/replica-mon:latest
    entrypoint: python3
    profiles: ["cli"]  # Only run when explicitly requested
```

### 2.5 GlueSync Integration Layer

**Components:**
1. **REST API Client** (`replica_msdk/client.py`)
2. **WebSocket Client** (`replica_msdk/websocket.py`)
3. **Protobuf Parser** (`replica_msdk/protobuf_parser.py`)

#### REST API Client:

**Authentication:**
```python
# Login and get token
resp = session.post(f"{base_url}/authentication/login",
                    json={"username": username, "password": password})
token = resp.json()["apiToken"]

# Use token in all requests
headers = {"Authorization": f"Bearer {token}"}
```

**Key Endpoints Used:**

| Endpoint | Method | Purpose | Response |
|----------|--------|---------|----------|
| `/pipelines` | GET | List all pipelines | `[{id, name, agents, ...}]` |
| `/pipelines/{id}` | GET | Get pipeline details | `{id, name, agents, ...}` |
| `/pipelines/{id}/entities` | GET | List entities | `[{entity: {...}}, ...]` |
| `/pipelines/{id}/agents` | GET | List agents | `[{agentId, agentType, ...}]` |
| `/pipelines/{id}/agents/{aid}/discovery/columns` | GET | **Get columns + PKs** | `{columns: [...], keys: [...]}` |
| `/pipelines/{id}/commands/sync/start` | POST | Start entity | `202 Accepted` |
| `/pipelines/{id}/commands/sync/stop` | POST | Stop entity | `202 Accepted` |

#### WebSocket Client:

**Connection:**
```python
ws_url = f"wss://localhost:1717/ws/metrics"
ws = websocket.WebSocketApp(ws_url, ...)
ws.run_forever()
```

**Metrics Received:**
```protobuf
message EntityMetrics {
    string entity_name = 1;
    string operation = 2;    // "insert", "update", "delete"
    int64 count = 3;
    int64 timestamp = 4;
}
```

**Processing:**
1. Receive metrics from GlueSync
2. Parse protobuf to Python dict
3. Store in SQLite (`metrics/ws_metrics.db`)
4. Broadcast to browser WebSocket clients

---

## 3. Data Flow Diagrams

### 3.1 Dashboard Real-Time Metrics Flow

```
┌──────────────┐     WebSocket      ┌────────────────────┐
│  GlueSync    │ ──metrics────────▶ │  Dashboard Backend │
│  Core Hub    │                    │  (main.py)         │
│  :1717       │                    │                    │
└──────────────┘                    │  1. Receive metrics│
                                    │  2. Parse protobuf │
                                    │  3. Store in SQLite│
                                    │  4. Broadcast to UI│
                                    └────────┬───────────┘
                                             │ WebSocket
                                             ▼
                                    ┌────────────────────┐
                                    │  Browser UI        │
                                    │  localhost:8000    │
                                    │                    │
                                    │  • Live charts     │
                                    │  • Entity status   │
                                    │  • Operation counts│
                                    └────────────────────┘
```

### 3.2 Verify Tool Flow (Current)

```
User clicks "Run Verification"
         │
         ▼
Frontend: POST /api/verify/{pipeline_id}/run
         │
         ▼
Backend: start_verify() endpoint
         │
         ├─ Get entities from GlueSync
         │
         └─ Spawn _verify_worker() thread
              │
              ├─ For each entity:
              │   │
              │   ├─ _count_as400(library, table)
              │   │   └─ SELECT COUNT(*) FROM library.table
              │   │
              │   ├─ _count_mssql(schema, table)
              │   │   └─ SELECT COUNT(*) FROM schema.table
              │   │
              │   └─ Calculate diff = source - target
              │
              └─ Store results in _verify_jobs dict
                   │
                   ▼
Frontend: GET /api/verify/{pipeline_id}/results (poll every 2s)
                   │
                   ▼
              Display in UI:
              ┌──────────────┬────────┬────────┬──────┐
              │ Entity       │ Source │ Target │ Diff │
              ├──────────────┼────────┼────────┼──────┤
              │ CUSTOMERS    │ 1,234  │ 1,230  │ +4   │
              │ ORDERS       │ 5,678  │ 5,678  │ 0    │
              └──────────────┴────────┴────────┴──────┘
```

### 3.3 Journal Cache Flow (BROKEN - Needs Fix)

```
┌─────────────────────────────────────────────────────────┐
│  ORIGINAL DESIGN (v0.4)                                 │
│                                                         │
│  monitor.py runs (via CLI or cron)                     │
│       │                                                 │
│       ├─ Auto-discover entities from GlueSync           │
│       │                                                 │
│       └─ For each entity:                               │
│            │                                            │
│            ├─ Check SQLite cache                        │
│            │   ├─ Cache hit → use cached entries        │
│            │   └─ Cache miss → query AS400              │
│            │                                            │
│            ├─ Query AS400 DISPLAY_JOURNAL               │
│            │   └─ Store in SQLite cache                 │
│            │                                            │
│            ├─ Query MSSQL CHANGETABLE()                 │
│            │                                            │
│            └─ Compare and report                        │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  CURRENT STATE (v0.5)                                   │
│                                                         │
│  Dashboard container runs (FastAPI only)               │
│       │                                                 │
│       ├─ WebSocket metrics → SQLite (ws_metrics.db)    │
│       │   └─ This is SEPARATE from journal cache!      │
│       │                                                 │
│       └─ Journal cache (cache/journal.db)               │
│           └─ NEVER UPDATED (monitor.py not running)    │
│           └─ STALE DATA from last CLI run              │
└─────────────────────────────────────────────────────────┘
```

---

## 4. Volume Mounts & Data Persistence

### 4.1 Current Volume Configuration

```yaml
# podman-compose.yaml
volumes:
  # SQLite journal/CT cache (legacy monitor.py)
  - ./cache:/app/replica-mon/cache:Z
  
  # SQLite WebSocket metrics time-series store (new)
  - ./metrics:/app/replica-mon/metrics:Z
  
  # qadmcli config for AS400 connection (needed for verify tool)
  - ../qadmcli/config:/app/qadmcli/config:Z
```

### 4.2 What's Stored Where

| Path | Container Path | Contents | Updated By | Used By |
|------|----------------|----------|------------|---------|
| `./cache/` | `/app/replica-mon/cache/` | `journal.db` - Journal entry cache | ❌ monitor.py (broken) | compare.py, monitor.py |
| `./metrics/` | `/app/replica-mon/metrics/` | `ws_metrics.db` - WebSocket time-series | ✅ WebSocket handler | Dashboard UI |
| `../qadmcli/config/` | `/app/qadmcli/config/` | `connection.yaml` - AS400 credentials | Manual (user) | _count_as400() |

### 4.3 Cache Files Structure

```
cache/
└── journal.db
    ├── journal_entries     # Cached AS400 journal entries
    │   ├── table_name TEXT
    │   ├── sequence_number INTEGER
    │   ├── operation TEXT
    │   ├── timestamp TEXT
    │   └── raw_data TEXT
    │
    ├── cache_metadata      # Cache state tracking
    │   ├── table_name TEXT
    │   ├── last_sequence INTEGER
    │   ├── last_update TEXT
    │   └── attention_flag BOOLEAN
    │
    └── attention_flags     # Tables with discrepancies
        ├── table_name TEXT
        ├── first_detected TEXT
        └── status TEXT

metrics/
└── ws_metrics.db
    ├── ws_metrics          # Real-time operation counts
    │   ├── timestamp TEXT
    │   ├── entity_name TEXT
    │   ├── operation TEXT
    │   └── count INTEGER
    │
    └── entity_status       # Current entity state
        ├── entity_name TEXT
        ├── total_inserts INTEGER
        ├── total_updates INTEGER
        └── total_deletes INTEGER
```

---

## 5. Known Issues & Technical Debt

### 5.1 Critical Issues

| Issue | Impact | Priority | Fix |
|-------|--------|----------|-----|
| **compare.py broken** | Cannot run deep comparison | 🔴 P0 | Fix Containerfile or replica-mon.sh |
| **monitor.py broken** | Journal cache never updated | 🔴 P0 | Run as background task or fix CLI |
| **Journal cache stale** | Monitoring shows old data | 🔴 P0 | Integrate with dashboard or run cron |
| **No record-level compare** | Can't identify missing rows | 🟡 P1 | Implement Part B design |
| **In-memory verify jobs** | Lost on restart | 🟡 P1 | Store in SQLite |

### 5.2 Architecture Decisions Needed

1. **Should monitor.py run in dashboard container?**
   - Option A: Background thread in FastAPI
   - Option B: Separate container with shared volumes
   - Option C: Cron job on host

2. **Should we keep dual entry points (uvicorn + python3)?**
   - Current: ENTRYPOINT hardcoded to uvicorn
   - Alternative: Use CMD, allow --entrypoint override

3. **Should journal cache and WebSocket metrics be unified?**
   - Current: Two separate SQLite databases
   - Alternative: Single metrics database with different tables

---

## 6. Enhancement Roadmap

### Phase 1: Fix Broken CLI (P0)
- [ ] Update Containerfile to use CMD instead of ENTRYPOINT
- [ ] Test `./replica-mon.sh python3 compare.py` works
- [ ] Test `./replica-mon.sh python3 monitor.py` works

### Phase 2: Record-Level Comparison (P1)
- [ ] Implement `GET /api/verify/{id}/entity/{eid}/primary-keys`
- [ ] Implement `POST /api/verify/{id}/entity/{eid}/compare-records`
- [ ] Create frontend mockup (HTML/CSS)
- [ ] Integrate into dashboard UI

### Phase 3: Cache Management (P1)
- [ ] Run monitor.py as background task in dashboard
- [ ] Add cache status endpoint
- [ ] Add cache refresh button to UI

### Phase 4: Advanced Features (P2)
- [ ] Export comparison results to CSV
- [ ] Email alerts on discrepancies
- [ ] Historical trend analysis

---

## 7. Environment Variables

### Required Variables

| Variable | Purpose | Example | Used By |
|----------|---------|---------|---------|
| `GLUESYNC_HOST` | GlueSync Core Hub URL | `https://localhost:1717` | Backend |
| `GLUESYNC_ADMIN_USERNAME` | Admin username | `admin` | Backend |
| `GLUESYNC_ADMIN_PASSWORD` | Admin password | `P@ssw0rd` | Backend |
| `AS400_USER` | AS400 connection user | `GLUESYNC01` | _count_as400() |
| `AS400_PASSWORD` | AS400 connection password | `...` | _count_as400() |
| `MSSQL_USER` | MSSQL connection user | `gluesync_user` | _count_mssql() |
| `MSSQL_PASSWORD` | MSSQL connection password | `...` | _count_mssql() |

### Optional Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `METRICS_DB_PATH` | WebSocket metrics DB path | `./metrics/ws_metrics.db` |
| `QADMCLI_PATH` | qadmcli installation path | `qadmcli` |
| `PROMETHEUS_URL` | Prometheus URL | `http://localhost:9090` |

---

## 8. Deployment Commands

### Dashboard (Production)

```bash
cd /home/ubuntu/_qoder/replica-mon

# Start
podman-compose up -d

# View logs
podman logs -f replica-mon

# Check status
podman ps | grep replica-mon

# Restart
podman-compose restart

# Stop
podman-compose down
```

### CLI Tools (Ad-hoc)

```bash
cd /home/ubuntu/_qoder/replica-mon

# Run compare.py
./replica-mon.sh python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS

# Run monitor.py (single check)
./replica-mon.sh python3 monitor.py

# Run monitor.py (continuous)
./replica-mon.sh python3 monitor.py --continuous --interval 300

# JSON output
./replica-mon.sh python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS --format json
```

---

## 9. Troubleshooting

### Dashboard Not Accessible

```bash
# Check container is running
podman ps | grep replica-mon

# Check logs
podman logs replica-mon

# Restart
podman-compose down && podman-compose up -d
```

### Verify Tool Shows No AS400 Count

```bash
# Check qadmcli config is mounted
podman exec replica-mon ls -la /app/qadmcli/config/

# Check logs for errors
podman logs --tail 100 replica-mon | grep -i "verify\|as400"

# Test AS400 connection
podman exec replica-mon python3 -c "
from qadmcli.config import load_config
from pathlib import Path
config = load_config(Path('/app/qadmcli/config/connection.yaml'))
print(f'AS400 Host: {config.as400.host}')
"
```

### WebSocket Not Connecting

```bash
# Check GlueSync is running
curl -k https://localhost:1717

# Test from inside container
podman exec replica-mon curl -k https://localhost:1717

# Check WebSocket endpoint
podman logs replica-mon | grep -i "websocket"
```

### Cache Not Updating

```bash
# Check cache directory
ls -la ./cache/

# Check cache database
sqlite3 ./cache/journal.db "SELECT * FROM cache_metadata;"

# Manual cache refresh (when CLI is fixed)
./replica-mon.sh python3 monitor.py --no-cache
```

---

## 10. References

- [ARCHITECTURE_ANALYSIS.md](./ARCHITECTURE_ANALYSIS.md) - Detailed analysis of compare.py impact and Part B design
- [METRICS_ARCHITECTURE.md](./METRICS_ARCHITECTURE.md) - WebSocket metrics design
- [REPLICA_MON_ARCHITECTURE.md](./REPLICA_MON_ARCHITECTURE.md) - Original architecture (outdated)
- [WEBSOCKET_ARCHITECTURE.md](./WEBSOCKET_ARCHITECTURE.md) - WebSocket implementation details
- [README.md](./README.md) - User documentation

---

**Document History:**

| Version | Date | Changes |
|---------|------|---------|
| v0.5.1 | 2026-04-07 | Comprehensive architecture document |
| v0.5.0 | 2026-04-05 | Initial dashboard architecture |
| v0.4.0 | 2026-03-28 | CLI-only architecture |
