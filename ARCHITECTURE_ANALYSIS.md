# ReplicaMon Architecture Analysis & Enhancement Plan

**Date:** 2026-04-07  
**Status:** Draft for Review  
**Version:** v0.5.1

---

## Part A: Compare.py Architecture Impact Analysis

### A.1 Original Compare Workflow (Pre-Dashboard)

The original `compare.py` was designed as a **standalone CLI tool** that performed deep, record-level comparison:

```
┌──────────────────────────────────────────────────────────────┐
│                    compare.py (Original)                     │
│                                                              │
│  1. Load qadmcli config (connection.yaml)                   │
│  2. Connect to AS400 via JayDeBeApi/JT400                   │
│  3. Query QSYS2.DISPLAY_JOURNAL for entries                 │
│  4. Connect to MSSQL via pyodbc                             │
│  5. Query CHANGETABLE() for CT changes                      │
│  6. Compare operation counts (INSERT/UPDATE/DELETE)         │
│  7. Optional: Compare individual records by PK              │
│  8. Generate text/JSON report                               │
└──────────────────────────────────────────────────────────────┘
```

**Key Characteristics:**
- ✅ **Ad-hoc execution**: Run on-demand, then exits
- ✅ **Deep comparison**: Could compare individual record values
- ✅ **Timezone-aware**: Handled AS400 (UTC+0) vs MSSQL (UTC+7)
- ✅ **Intelligent caching**: SQLite cache for journal entries
- ✅ **Direct DB access**: Connected to both AS400 and MSSQL directly
- ❌ **No real-time monitoring**: Only snapshot comparison
- ❌ **No web UI**: CLI-only output

### A.2 Architecture Changes & Impact

#### What Changed:

| Aspect | Original (v0.4) | Current (v0.5) |
|--------|----------------|----------------|
| **Primary Interface** | CLI (`./replica-mon.sh`) | Web Dashboard (`podman-compose up -d`) |
| **Container Mode** | Ephemeral (`podman run --rm`) | Persistent (always-running FastAPI) |
| **Entry Point** | `python3 compare.py` | `uvicorn backend/main:app` |
| **Data Source** | Direct AS400 + MSSQL queries | GlueSync WebSocket + direct queries (verify) |
| **Cache Update** | On each CLI run | ❓ **UNCLEAR - needs clarification** |
| **Monitoring** | `monitor.py` (ad-hoc) | WebSocket real-time stream |

#### Current Status of compare.py:

**❌ BROKEN** - The command fails because:

```bash
./replica-mon.sh python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS
# Error: python3: can't open file '/app/compare.py': [Errno 2] No such file or directory
```

**Root Cause:**
- Containerfile ENTRYPOINT is now: `uvicorn main:app --app-dir /app/replica-mon/backend`
- The `replica-mon.sh` script passes `compare.py` as an argument to `uvicorn`, not `python3`
- Even if it reached python3, the working directory is `/app`, not `/app/replica-mon`
- The `--entrypoint python3` override in the old script doesn't work with the new Containerfile

#### Impact Assessment:

| Feature | Status | Impact |
|---------|--------|--------|
| **compare.py CLI** | ❌ Broken | Cannot run deep record comparison |
| **monitor.py CLI** | ❌ Broken | Cannot run automated monitoring |
| **Dashboard - Entity Metrics** | ✅ Working | Real-time INSERT/UPDATE/DELETE counts via WebSocket |
| **Dashboard - Verify Tool** | ✅ Partially Working | Row counts only (no record-level comparison) |
| **Journal Caching** | ⚠️ Unclear | Cache exists but update mechanism unclear |
| **Timezone Handling** | ⚠️ Unused | compare.py had it, dashboard doesn't use it |

### A.3 How Current Verify Tool Works

The current verify tool (`backend/main.py` lines 222-340):

```python
def _count_as400(library, table):
    # 1. Load qadmcli config
    # 2. Connect to AS400 via AS400ConnectionManager
    # 3. Execute: SELECT COUNT(*) FROM {library}.{table}
    # 4. Return: (count, None)
    
def _count_mssql(schema, table):
    # 1. Load config from env or config.json
    # 2. Connect via pyodbc
    # 3. Execute: SELECT COUNT(*) FROM {schema}.{table}
    # 4. Return: (count, last_timestamp)
```

**Current Limitations:**
- ❌ Only compares **total row counts**
- ❌ Cannot identify **which specific records** are missing
- ❌ Cannot compare **column values** for matching records
- ❌ No awareness of **primary keys**
- ❌ Cannot detect **data corruption** (same count, different values)

### A.4 What's Missing for Full Parity

To restore original compare.py functionality + add new features:

1. **Record-level comparison by PK** (new requirement)
2. **Column value diff** (new requirement)  
3. **Visual side-by-side display** (new requirement)
4. **PK discovery from GlueSync metadata** (new requirement)
5. **CLI access to compare.py** (broken, needs fix)
6. **Cache update mechanism clarification** (unclear)

---

## Part B: Record-Level Comparison Feature Design

### B.1 Requirements Analysis

**User Requirements:**
1. Compare source and target records on **matching primary keys**
2. Get primary keys from **GlueSync metadata**
3. If PKs match, check if **non-key column values** also match
4. Display **side-by-side view** (source left, target right)
5. Show **missing records** (empty on one side)
6. **Highlight differences** in non-key fields

### B.2 GlueSync API Capability Assessment

#### Available APIs (from replica_msdk/client.py):

| API | Endpoint | Returns | PK Info? |
|-----|----------|---------|----------|
| `list_entities()` | `GET /pipelines/{id}/entities` | Entity list with source/target tables | ❌ No PK details |
| `get_entity()` | Via list_entities filter | Single entity details | ❌ No PK details |
| `get_discovery_columns()` | `GET /pipelines/{id}/agents/{agent_id}/discovery/columns` | Column metadata | ✅ **YES - has keys** |
| `get_discovery_tables()` | `GET /pipelines/{id}/agents/{agent_id}/discovery/tables` | Table list | ❌ No PK details |

**Key Finding:** 
```python
def get_discovery_columns(self, pipeline_id, agent_id, schema, table):
    resp = self.request("GET", 
        f"/pipelines/{pipeline_id}/agents/{agent_id}/discovery/columns",
        params={"tableschema": schema, "tablename": table})
    return resp.json()  # Returns: {"columns": [...], "keys": [...]}
```

**✅ GlueSync API DOES support primary key discovery!**

#### Missing Information:

To use `get_discovery_columns()`, we need:
- `pipeline_id` ✅ (from `list_pipelines()`)
- `agent_id` ⚠️ (need to get from pipeline config)
- `schema` ✅ (from entity: source table library)
- `table` ✅ (from entity: source table name)

**How to get agent_id:**
```python
# Option 1: From pipeline object
pipeline = client.get_pipeline(pipeline_id)
agents = pipeline.get('agents', [])
source_agent = next(a for a in agents if a.get('agentType') == 'SOURCE')
source_agent_id = source_agent['agentId']

# Option 2: List agents
agents = client.list_agents(pipeline_id)
source_agent = next(a for a in agents if a.get('type') == 'SOURCE')
```

### B.3 API Design for Record Comparison

#### New Backend Endpoints Needed:

```python
# 1. Get primary keys for an entity
@app.get("/api/verify/{pipeline_id}/entity/{entity_id}/primary-keys")
def get_entity_primary_keys(pipeline_id: str, entity_id: str):
    """
    Get primary key columns from GlueSync metadata
    Returns: {"keys": ["CUST_ID", "ORDER_NO"], "source_agent_id": "..."}
    """
    # 1. Get entity details (source table, target table)
    # 2. Get source agent ID from pipeline
    # 3. Call GlueSync: get_discovery_columns(pipeline_id, source_agent_id, schema, table)
    # 4. Extract keys from response
    # 5. Return keys

# 2. Compare records by primary key
@app.post("/api/verify/{pipeline_id}/entity/{entity_id}/compare-records")
def compare_records(pipeline_id: str, entity_id: str, request: CompareRequest):
    """
    Compare records between AS400 and MSSQL by primary key
    Returns: {
        "source_only": [...],      # Records only in AS400
        "target_only": [...],      # Records only in MSSQL
        "matched": [...],          # Records with same PK, all values match
        "mismatched": [...]        # Records with same PK, different values
    }
    """
    # 1. Get PK columns (from above API or request)
    # 2. Get all PKs from AS400: SELECT {pk_cols} FROM {library}.{table}
    # 3. Get all PKs from MSSQL: SELECT {pk_cols} FROM {schema}.{table}
    # 4. Find: source_only, target_only, common_pks
    # 5. For common_pks: SELECT * FROM both, compare values
    # 6. Return structured diff

# 3. Get sample records (for UI display)
@app.get("/api/verify/{pipeline_id}/entity/{entity_id}/sample-records")
def get_sample_records(pipeline_id: str, entity_id: str, limit: int = 100):
    """
    Get sample records from both source and target
    Returns: {
        "source_columns": [...],
        "target_columns": [...],
        "records": [
            {
                "pk": {"CUST_ID": 123},
                "source": {"CUST_ID": 123, "NAME": "John", ...},
                "target": {"CUST_ID": 123, "NAME": "John", ...},
                "status": "matched|mismatched|source_only|target_only",
                "differences": ["NAME"]  # Column names that differ
            }
        ]
    }
    """
```

### B.4 Frontend Mockup Design

I'll create a comprehensive HTML mockup. Let me design it:

#### Layout Structure:

```
┌──────────────────────────────────────────────────────────────────┐
│  Entity Comparison: GSLIBTST.CUSTOMERS → dbo.CUSTOMERS          │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─ Summary ───────────────────────────────────────────────────┐│
│  │  Primary Keys: CUST_ID                                      ││
│  │  Source Records: 1,234    Target Records: 1,230             ││
│  │  Matched: 1,228    Mismatched: 2    Missing: 6              ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  ┌─ Filters ───────────────────────────────────────────────────┐│
│  │  [All ▼] [Matched ✓] [Mismatched ⚠] [Source Only ◀]       ││
│  │  [Target Only ▶]                                           ││
│  │  Search PK: [________]  [Apply]                            ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  ┌─ Record Comparison Table ───────────────────────────────────┐│
│  │                                                              ││
│  │  PK     │ SOURCE (AS400)        │ TARGET (MSSQL)           ││
│  │         │ CUST_ID│NAME │EMAIL   │ CUST_ID│NAME │EMAIL      ││
│  │─────────┼────────┼─────┼────────┼────────┼─────┼───────────││
│  │ 123     │ 123    │John │j@x.com │ 123    │John │j@x.com   ││
│  │         │        │     │        │        │     │           ││
│  │ 124     │ 124    │Jane │j@y.com │ 124    │JANE │j@y.com   ││
│  │         │        │🔴   │        │        │🔴   │           ││
│  │ 125     │ 125    │Bob  │b@z.com │        │     │           ││
│  │         │        │     │        │        │◀MISSING         ││
│  │ 126     │        │     │        │ 126    │Alice│a@w.com   ││
│  │         │        │MISSING▶│     │        │     │           ││
│  └─────────┼────────┼─────┼────────┼────────┼─────┼───────────┘│
│            │                                                    │
│  Pagination: [1] [2] [3] ... [13]  Showing 1-100 of 1,234     │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

#### Color Coding:
- ✅ **Green background**: Matched records (all values same)
- ⚠️ **Yellow background**: Mismatched records (PK same, values differ)
- 🔴 **Red highlight**: Specific columns that differ
- ◀ **Orange background (right side empty)**: Source only (missing in target)
- ▶ **Blue background (left side empty)**: Target only (missing in source)

Let me create the actual mockup HTML file:
