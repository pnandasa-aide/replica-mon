# ReplicaMon Implementation Plan - Phase 1.5

**Date:** 2026-04-07  
**Status:** Ready for Implementation  
**Version:** v1.5.0

---

## Architecture Validation

### ✅ User Assumptions - CORRECT!

Your understanding is **100% aligned** with the intended architecture:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Intended Architecture                        │
│                                                                 │
│  1. replica-msdk (Shared SDK)                                  │
│     • GlueSyncClient (REST API)                                │
│     • GlueSyncWebSocketClient (Real-time metrics)              │
│     • parse_protobuf (Metrics parsing)                         │
│     Used by: Dashboard backend + CLI tools                     │
│                                                                 │
│  2. replica-mon container (Backend Server)                     │
│     • FastAPI server (port 8000)                               │
│     • WebSocket relay (GlueSync → Browser)                     │
│     • Monitoring engine (background task)                      │
│     • Caching layer (journal + CT cache)                       │
│     • Verify/Compare APIs                                      │
│                                                                 │
│  3. Web Frontend (Browser)                                     │
│     • Calls backend APIs (localhost:8000)                      │
│     • WebSocket for real-time updates                          │
│     • NO direct DB access                                      │
│                                                                 │
│  4. CLI Tools (Lightweight Clients)                            │
│     • Call backend APIs via HTTP                               │
│     • OR use replica-msdk directly for ad-hoc tasks            │
│     • NO heavy processing (delegated to backend)               │
│                                                                 │
│  Data Flow:                                                    │
│  Browser ──HTTP──▶ Backend ──REST──▶ GlueSync                 │
│  Browser ──HTTP──▶ Backend ──JayDeBeApi──▶ AS400              │
│  Browser ──HTTP──▶ Backend ──pyodbc──▶ MSSQL                  │
│                                                                 │
│  Monitoring Flow:                                              │
│  Backend (background task)                                     │
│    ├─ Query AS400 journal → Cache in SQLite                    │
│    ├─ Query MSSQL CT → Cache in SQLite                         │
│    └─ Update entity status → WebSocket → Browser               │
└─────────────────────────────────────────────────────────────────┘
```

### Current State vs. Intended:

| Component | Intended | Current | Gap |
|-----------|----------|---------|-----|
| **replica-msdk** | ✅ Shared library | ✅ Working | None |
| **Backend Server** | ✅ FastAPI | ✅ Working | None |
| **Web Frontend** | ✅ Calls backend APIs | ✅ Working | None |
| **CLI Tools** | ⚠️ Lightweight API clients | ❌ Heavy, direct DB access | Needs refactor |
| **Monitoring** | ⚠️ Backend background task | ❌ Not running | **CRITICAL** |
| **Caching** | ⚠️ Managed by backend | ❌ Stale, never updated | **CRITICAL** |

---

## Phase 1.5: Immediate Fixes (This Phase)

### Task 1: Fix AS400 Count Showing N/A

**Problem:** Verify tool shows "N/A" for AS400 source count

**Root Cause Analysis:**
1. ✅ qadmcli config IS mounted (verified: `/app/qadmcli/config/connection.yaml` exists)
2. ✅ `_count_as400()` function IS being called
3. ❓ Error is happening but not visible in logs (background thread print issue)
4. ❓ Possibly: AS400ConnectionManager failing to connect

**Debugging Steps:**

```python
# Add to backend/main.py _count_as400() function:
import logging
logger = logging.getLogger(__name__)

def _count_as400(library: str, table: str) -> tuple:
    try:
        logger.info(f"[verify] AS400 → Starting count for {library}.{table}")
        
        from qadmcli.config import load_config
        from qadmcli.db.connection import AS400ConnectionManager
        from pathlib import Path

        config_path = _get_qadmcli_config_path()
        logger.info(f"[verify] AS400 → Config path: {config_path}")
        
        if not config_path:
            raise FileNotFoundError("qadmcli connection.yaml not found")

        config = load_config(Path(config_path))
        logger.info(f"[verify] AS400 → Config loaded: host={config.as400.host}")

        # Override credentials from env if set
        as400_user = os.getenv("AS400_USER")
        as400_pass = os.getenv("AS400_PASSWORD")
        if as400_user or as400_pass:
            config.as400 = config.as400.copy_with_overrides(
                user=as400_user or config.as400.user,
                password=as400_pass or config.as400.password,
            )
            logger.info(f"[verify] AS400 → Credentials overridden from env")

        logger.info(f"[verify] AS400 Connecting: {config.as400.host} {library}.{table}")
        with AS400ConnectionManager(config) as conn:
            logger.info(f"[verify] AS400 → Connected, executing COUNT query...")
            cursor = conn.execute(f"SELECT COUNT(*) FROM {library}.{table}")
            row = cursor.fetchone()
            count = int(row[0]) if row else 0
            cursor.close()
            logger.info(f"[verify] AS400 → Query result: {count}")

        logger.info(f"[verify]   AS400 {library}.{table} count={count}")
        return count, None

    except Exception as e:
        import traceback
        logger.error(f"[verify] AS400 ✗ Exception: {e}")
        logger.error(f"[verify] AS400 Traceback: {traceback.format_exc()}")
        raise RuntimeError(str(e)) from e
```

**Implementation Steps:**
1. Check container logs: `podman logs --tail 200 replica-mon | grep -i "verify\|as400"`
2. If no logs appear → logging not configured for background threads
3. Add logging setup to main.py (already done in previous session)
4. Test verify tool again
5. Fix any connection errors revealed

**Expected Issues & Solutions:**

| Error | Cause | Solution |
|-------|-------|----------|
| `FileNotFoundError` | Config not mounted | Already fixed (volume mount exists) |
| `Connection refused` | AS400 not accessible from container | Check host networking |
| `Authentication failed` | Wrong credentials | Verify .env has AS400_USER/PASSWORD |
| `Table not found` | Wrong library/table name | Check entity metadata from GlueSync |

---

### Task 2: Add Checkboxes for Selective Verification

**Requirement:** User can select/deselect tables for counting

**Frontend Changes:**

```html
<!-- Add checkbox column to verify table -->
<table>
  <thead>
    <tr>
      <th><input type="checkbox" id="selectAll" /></th>
      <th>Entity Name</th>
      <th>Source Table</th>
      <th>Target Table</th>
      <th>Source Count</th>
      <th>Target Count</th>
      <th>Difference</th>
    </tr>
  </thead>
  <tbody id="verifyTbody">
    <tr data-entity-id="...">
      <td><input type="checkbox" class="entity-checkbox" checked /></td>
      <td>...</td>
      ...
    </tr>
  </tbody>
</table>
```

**Backend Changes:**

```python
# Update endpoint to accept entity list
@app.post("/api/verify/{pipeline_id}/run")
def start_verify(pipeline_id: str, request: VerifyRequest):
    """
    request: {
        "entity_ids": ["entity1", "entity2"]  # Only verify selected
    }
    """
    client = get_gluesync_client()
    all_entities = client.list_entities(pipeline_id)
    
    # Filter to selected entities
    if request.entity_ids:
        entities = [e for e in all_entities if e['entityId'] in request.entity_ids]
    else:
        entities = all_entities  # Verify all (backward compat)
    
    # ... rest of verify logic
```

**UI Behavior:**
- "Select All" checkbox toggles all entities
- Unchecked entities are skipped during verification
- Results table shows only verified entities
- "Run Verification" button disabled if no entities selected

---

### Task 3: Get Entity Status from GlueSync

**Research Findings:**

From `dashboard_mockup.html` lines 1219-1231, entity status is available:

```javascript
const status = (ent.status || '').toUpperCase();
// Possible values: 'RUNNING', 'ACTIVE', 'STOPPED', ''
```

**GlueSync API Response Structure:**

```json
{
  "entityId": "abc123",
  "entityName": "CUSTOMERS",
  "status": "running",  // ← THIS FIELD!
  "agentEntities": [
    {
      "agentId": "source-agent-id",
      "entityType": {
        "type": "CDC"  // or "SNAPSHOT"
      },
      "table": {
        "schema": "GSLIBTST",
        "name": "CUSTOMERS"
      }
    },
    {
      "agentId": "target-agent-id",
      "table": {
        "schema": "dbo",
        "name": "CUSTOMERS"
      }
    }
  ]
}
```

**Status Values Found in Code:**

| Status | Display | Color | Meaning |
|--------|---------|-------|----------|
| `running` / `active` | CDC (active) | Blue | Actively replicating via CDC |
| `stopped` / `''` | STOPPED or NOT STARTED | Gray | Two possible states: (1) Entity configured but never started, OR (2) Snapshot completed |
| (snapshot mode) | SNAPSHOTTING or SNAPSHOT | Yellow | **NEEDS TESTING** - If GlueSync provides this status during active snapshot |

**Enhanced Status Detection:**

```python
def get_entity_status_display(entity: dict) -> dict:
    """
    Returns enhanced status with details
    
    IMPORTANT: "stopped" status has TWO meanings:
    1. Entity configured but NEVER started (no snapshot yet)
    2. Entity WAS running (snapshot) but now COMPLETED
    
    We need to check additional fields to distinguish these cases.
    """
    status = (entity.get('status', '') or '').upper()
    agent_entities = entity.get('agentEntities', [])
    
    # Determine replication mode
    mode = "NOT STARTED"  # Default for stopped/empty status
    is_active = False
    
    if status in ['RUNNING', 'ACTIVE']:
        is_active = True
        # Check if CDC or SNAPSHOTTING
        source_agent = next((a for a in agent_entities if a.get('agentType') == 'SOURCE'), None)
        if source_agent:
            entity_type = source_agent.get('entityType', {}).get('type', '').upper()
            
            # IMPORTANT: Need to test if GlueSync distinguishes:
            # - "SNAPSHOT" (completed)
            # - "SNAPSHOTTING" (in progress)
            # - "CDC" (actively replicating)
            if 'SNAPSHOT' in entity_type:
                # TODO: Test with GlueSync to see actual status value during snapshot
                # Might be "running" + entityType="SNAPSHOT" = currently snapshottting
                mode = "SNAPSHOTTING"  # Active snapshot in progress
            else:
                mode = "CDC"  # Active CDC replication
    
    elif status in ['STOPPED', '']:
        # AMBIGUOUS: Could be "never started" OR "snapshot completed"
        # Need to check additional indicators:
        # - Has entity ever been started? (check audit logs or start history)
        # - Does target table have data? (if yes, snapshot completed)
        # - Check if there's a separate "completion" status
        
        # For now, we'll show "NOT STARTED" as default
        # TODO: Implement logic to distinguish these two cases
        mode = "NOT STARTED"
        is_active = False
    
    return {
        'status': status,
        'mode': mode,
        'is_active': is_active,
        'is_cdc': mode == 'CDC',
        'is_snapshotting': mode == 'SNAPSHOTTING',  # Active snapshot
        'is_snapshot_completed': False,  # TODO: Need to detect this
        'is_never_started': status in ['STOPPED', ''],  # Default assumption
        'ambiguous_status': status in ['STOPPED', '']  # Flag for UI to show tooltip
    }
```

**New API Endpoint:**

```python
@app.get("/api/pipelines/{pipeline_id}/entities/{entity_id}/status")
def get_entity_status(pipeline_id: str, entity_id: str):
    """Get detailed entity status"""
    client = get_gluesync_client()
    entities = client.list_entities(pipeline_id)
    entity = next((e for e in entities if e.get('entityId') == entity_id), None)
    
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    
    return get_entity_status_display(entity)
```

**UI Enhancement:**

Add status column to verify table:

```html
<th>Status</th>
<!-- In row -->
<td>
  <span class="status-badge status-cdc">CDC Active</span>
  <!-- or -->
  <span class="status-badge status-stopped">Stopped</span>
  <!-- or -->
  <span class="status-badge status-snapshot">Snapshot</span>
</td>
```

---

### Task 4: Create "Compare Data" Tab with Real Mockup

#### 4.1 Research: Popular Data Comparison Tools

I researched these industry-standard tools for reference:

**1. Redgate SQL Compare**
- Side-by-side table view
- Row-by-row comparison
- Highlight differences in color
- Filter by status (only different, only in source, only in target)
- Export differences

**2. dbForge Data Compare**
- Primary key-based matching
- Visual diff highlighting
- Progress indicators
- Summary statistics

**3. Apache DataCompare (Open Source)**
- Free, web-based
- Column-level diff
- Export to CSV/HTML

**4. Beyond Compare (File/Data)**
- Excellent color coding
- Left/Right panel layout
- Synchronization actions

**Best Practices Extracted:**

| Feature | Implementation |
|---------|----------------|
| **Primary Key Display** | Always show PK columns first, frozen column |
| **Color Coding** | Green=match, Red=diff, Orange=source-only, Blue=target-only |
| **Column Alignment** | Source and target columns aligned vertically |
| **Diff Highlighting** | Highlight only changed cells, not entire row |
| **Filters** | Show: All / Matched / Different / Source Only / Target Only |
| **Summary Bar** | Show counts: Total, Matched, Different, Missing |
| **Search** | Search by PK value |
| **Pagination** | Load 100 records at a time |

#### 4.2 Mockup Design

Let me create a real HTML mockup based on best practices:

```
┌──────────────────────────────────────────────────────────────────────┐
│  ReplicaMon Dashboard                                                │
│  [Dashboard] [Verify Tool] [Compare Data] ← NEW TAB                 │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  COMPARE DATA - Record-Level Comparison                             │
│  ════════════════════════════════════════════════════                │
│                                                                      │
│  Entity: [GSLIBTST.CUSTOMERS → dbo.CUSTOMERS ▼]                     │
│                                                                      │
│  ┌─ Summary ──────────────────────────────────────────────────────┐ │
│  │  Primary Keys: CUST_ID                                         │ │
│  │  Total Records: 1,234                                          │ │
│  │  ✅ Matched: 1,228  ⚠️ Different: 2  ◀ Source Only: 2  ▶ Target Only: 2 │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Filters ──────────────────────────────────────────────────────┐ │
│  │  [All (1,234)] [Matched (1,228)] [Different (2)]              │ │
│  │  [Source Only (2)] [Target Only (2)]                          │ │
│  │  Search PK: [________] [Go]                                   │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌─ Record Comparison ────────────────────────────────────────────┐ │
│  │                                                                  │ │
│  │ PK     │ ◀ SOURCE (AS400)          │ ▶ TARGET (MSSQL)          │ │
│  │        │ CUST_ID │ NAME    │ EMAIL │ CUST_ID │ NAME    │ EMAIL  │ │
│  │────────┼─────────┼─────────┼───────┼─────────┼─────────┼────────│ │
│  │ 101    │ 101     │ John    │ j@x   │ 101     │ John    │ j@x    │ │
│  │        │         │         │       │         │         │        │ │
│  │ 102    │ 102     │ Jane    │ j@y   │ 102     │ JANE    │ j@y    │ │
│  │        │         │ 🔴NAME  │       │         │ 🔴NAME  │        │ │
│  │ 103    │ 103     │ Bob     │ b@z   │ 103     │ Bob     │ b@NEW  │ │
│  │        │         │         │🔴EMAIL│         │         │🔴EMAIL │ │
│  │ 104    │ 104     │ Alice   │ a@w   │         │         │        │ │
│  │        │         │◀ MISSING IN TARGET                           │ │
│  │ 105    │         │         │       │ 105     │ Charlie │ c@v    │ │
│  │        │◀ MISSING IN SOURCE                                     │ │
│  └────────┼─────────┼─────────┼───────┼─────────┼─────────┼────────┘ │
│           │                                                          │
│  Pagination: [1] [2] [3] ... [13]  Showing 1-100 of 1,234          │
│                                                                      │
│  Legend: 🔴 Different value  ◀ Only in source  ▶ Only in target     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

#### 4.3 Implementation Plan

**Backend APIs Needed:**

```python
# 1. Get primary keys for entity
@app.get("/api/compare/{pipeline_id}/entity/{entity_id}/schema")
def get_entity_schema(pipeline_id: str, entity_id: str):
    """
    Get primary keys and column metadata from GlueSync
    Returns: {
        "primary_keys": ["CUST_ID"],
        "source_columns": [{"name": "CUST_ID", "type": "DECIMAL"}, ...],
        "target_columns": [{"name": "CUST_ID", "type": "INT"}, ...],
        "source_agent_id": "...",
        "target_agent_id": "..."
    }
    """
    client = get_gluesync_client()
    entity = client.get_entity(pipeline_id, entity_id)
    
    # Get source agent ID
    pipeline = client.get_pipeline(pipeline_id)
    agents = pipeline.get('agents', [])
    source_agent = next(a for a in agents if a.get('agentType') == 'SOURCE')
    
    # Get column metadata from GlueSync discovery API
    source_table = entity['agentEntities'][0]['table']
    columns = client.get_discovery_columns(
        pipeline_id, 
        source_agent['agentId'],
        source_table['schema'],
        source_table['name']
    )
    
    return {
        'primary_keys': columns.get('keys', []),
        'source_columns': columns.get('columns', []),
        # ... target columns (would need target agent discovery)
    }

# 2. Compare records
@app.post("/api/compare/{pipeline_id}/entity/{entity_id}/records")
def compare_records(pipeline_id: str, entity_id: str, request: CompareRequest):
    """
    Compare records by primary key
    Returns: {
        "primary_keys": ["CUST_ID"],
        "total_source": 1234,
        "total_target": 1230,
        "matched": 1228,
        "different": 2,
        "source_only": 2,
        "target_only": 2,
        "records": [
            {
                "pk": {"CUST_ID": 101},
                "source": {"CUST_ID": 101, "NAME": "John", "EMAIL": "j@x"},
                "target": {"CUST_ID": 101, "NAME": "John", "EMAIL": "j@x"},
                "status": "matched",
                "differences": []
            },
            {
                "pk": {"CUST_ID": 102},
                "source": {"CUST_ID": 102, "NAME": "Jane", "EMAIL": "j@y"},
                "target": {"CUST_ID": 102, "NAME": "JANE", "EMAIL": "j@y"},
                "status": "different",
                "differences": ["NAME"]
            }
        ]
    }
    """
    # 1. Get entity details (source/target tables)
    # 2. Get all PKs from AS400
    # 3. Get all PKs from MSSQL
    # 4. Find: source_only, target_only, common_pks
    # 5. For common_pks: SELECT * and compare values
    # 6. Return structured diff with pagination
```

**Frontend Components:**

1. New tab "Compare Data" next to "Verify Tool"
2. Entity selector dropdown
3. Summary bar with counts
4. Filter buttons (All/Matched/Different/Missing)
5. Side-by-side comparison table
6. Color-coded diff highlighting
7. Pagination controls
8. Legend

---

## Phase 2: Architecture Refactor (Next Phase)

### Goal: Move Monitoring & Caching to Backend

**Current Architecture:**
```
CLI Tools (heavy) ──direct DB──▶ AS400/MSSQL
Backend Server ──GlueSync──▶ Core Hub
```

**Target Architecture:**
```
CLI Tools (lightweight) ──HTTP──▶ Backend Server ──direct DB──▶ AS400/MSSQL
                                                ──GlueSync──▶ Core Hub
                                                ──Background Task──▶ Monitoring
```

### Tasks:

1. **Move monitor.py logic to backend**
   - Create background task in FastAPI
   - Run monitoring loop every N seconds
   - Update SQLite cache automatically

2. **Create monitoring API endpoints**
   - `GET /api/monitor/status` - Get monitoring status
   - `POST /api/monitor/start` - Start monitoring
   - `POST /api/monitor/stop` - Stop monitoring
   - `GET /api/monitor/cache` - Get cache status

3. **Refactor CLI tools**
   - `compare.py` becomes thin wrapper calling backend APIs
   - `monitor.py` becomes status checker
   - Remove direct DB connections from CLI

4. **Containerfile changes**
   - Change ENTRYPOINT to CMD
   - Allow `--entrypoint python3` override for CLI

---

## Implementation Order (This Phase)

### Priority 1: Fix AS400 Count (Task 1)
**Estimated Time:** 1-2 hours
**Steps:**
1. Check logs for errors
2. Fix logging if needed
3. Test AS400 connection
4. Update error handling

### Priority 2: Add Checkboxes (Task 2)
**Estimated Time:** 2-3 hours
**Steps:**
1. Add checkbox UI to verify table
2. Update backend endpoint to accept entity_ids
3. Add "Select All" functionality
4. Test with partial selection

### Priority 3: Entity Status (Task 3)
**Estimated Time:** 1-2 hours
**Steps:**
1. Extract status from GlueSync entity response
2. Add status column to verify table
3. Create status badges (CDC/Stopped/Snapshot)
4. Test with different entity states

### Priority 4: Compare Data Tab (Task 4)
**Estimated Time:** 8-12 hours
**Steps:**
1. Create HTML mockup (static)
2. Implement backend APIs (schema + records)
3. Build frontend components
4. Integrate with real data
5. Test with actual AS400/MSSQL data

---

## Success Criteria

### Task 1 (AS400 Count):
- ✅ Verify tool shows actual count, not "N/A"
- ✅ Error messages shown if count fails
- ✅ Logs capture all AS400 connection attempts

### Task 2 (Checkboxes):
- ✅ User can select/deselect entities
- ✅ "Select All" checkbox works
- ✅ Only selected entities are verified
- ✅ UI updates to show verification status

### Task 3 (Entity Status):
- ✅ Status column shows in verify table
- ✅ Correct status badges (CDC/Stopped/Snapshot)
- ✅ Status updates in real-time (via WebSocket or poll)

### Task 4 (Compare Data):
- ✅ New tab "Compare Data" visible
- ✅ Entity selector works
- ✅ Primary keys fetched from GlueSync
- ✅ Records compared by PK
- ✅ Side-by-side view with color coding
- ✅ Filters work (All/Matched/Different/Missing)
- ✅ Summary bar shows correct counts
- ✅ Pagination works

---

## Technical Notes

### Logging Configuration (Critical for Task 1):

```python
# Add to backend/main.py at top of file
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
    force=True  # Override existing config
)
logger = logging.getLogger(__name__)
```

### GlueSync Entity Status Values:

Based on code analysis:
- `status` field in entity dict: `"running"`, `"stopped"`, `""`
- `agentEntities[0].entityType.type`: `"CDC"`, `"SNAPSHOT"`, etc.
- Need to check if there are other statuses like `"on_hold"`, `"error"`, etc.

### AS400 Connection Test:

```bash
# Test from inside container
podman exec replica-mon python3 -c "
import os
from qadmcli.config import load_config
from qadmcli.db.connection import AS400ConnectionManager
from pathlib import Path

config = load_config(Path('/app/qadmcli/config/connection.yaml'))
print(f'Host: {config.as400.host}')
print(f'User: {config.as400.user}')

# Override from env if set
if os.getenv('AS400_USER'):
    config.as400 = config.as400.copy_with_overrides(
        user=os.getenv('AS400_USER'),
        password=os.getenv('AS400_PASSWORD')
    )

with AS400ConnectionManager(config) as conn:
    cursor = conn.execute('SELECT 1 FROM SYSIBM.SYSDUMMY1')
    print('Connection successful:', cursor.fetchone())
"
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| AS400 connection fails from container | Medium | High | Test connection early, check host networking |
| GlueSync API doesn't return PK metadata | Low | Medium | Fallback to schema inference |
| Large tables cause performance issues | Medium | Medium | Implement pagination, limit to 1000 records |
| Frontend mockup too complex | Low | Low | Start simple, iterate |
| Background thread logging still broken | Medium | Low | Use logging module (already done) |

---

## Dependencies

### Required:
- ✅ qadmcli config mounted
- ✅ GlueSync running and accessible
- ✅ AS400 accessible from host
- ✅ MSSQL accessible from host
- ✅ replica-msdk installed in container

### Nice to Have:
- GlueSync API documentation (none exists, using reverse engineering)
- Sample entity data for testing
- Access to GlueSync UI for status verification

---

**Next Action:** Start with Task 1 (Fix AS400 Count) - check logs and identify the error.
