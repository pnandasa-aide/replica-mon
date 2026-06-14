# Implementation Summary & Next Steps

**Date:** 2026-04-07  
**Phase:** 1.5 (Immediate Fixes)

---

## ✅ Architecture Validation

Your assumptions are **100% CORRECT**! The intended architecture is:

```
replica-msdk (Shared SDK)
    ↓
replica-mon container (Backend Server - FastAPI)
    ↓
├── Web Frontend (Browser) ← HTTP/WebSocket → Backend
└── CLI Tools (Lightweight) ← HTTP → Backend
```

**Current Gaps:**
- ❌ CLI tools still do direct DB access (should call backend APIs)
- ❌ Monitoring engine not running in backend
- ❌ Cache never updated
- ❌ compare.py broken

**Full Plan:** See [IMPLEMENTATION_PLAN_PHASE_1.5.md](file:///home/ubuntu/_qoder/replica-mon/IMPLEMENTATION_PLAN_PHASE_1.5.md)

---

## 📋 This Phase Tasks (4 Tasks)

### Task 1: Fix AS400 Count Showing N/A

**Status:** Ready to implement  
**Estimated Time:** 1-2 hours  
**File:** `backend/main.py`

**Steps:**
1. Check container logs: `podman logs --tail 200 replica-mon | grep -i "verify\|as400"`
2. If no logs → logging not configured (should already be fixed from previous session)
3. Test AS400 connection manually:
   ```bash
   podman exec replica-mon python3 -c "
   from qadmcli.config import load_config
   from qadmcli.db.connection import AS400ConnectionManager
   from pathlib import Path
   
   config = load_config(Path('/app/qadmcli/config/connection.yaml'))
   with AS400ConnectionManager(config) as conn:
       cursor = conn.execute('SELECT 1 FROM SYSIBM.SYSDUMMY1')
       print('Connection OK:', cursor.fetchone())
   "
   ```
4. Fix any connection errors
5. Verify count displays in UI

**Expected Issues:**
- Credentials not in .env → Add AS400_USER and AS400_PASSWORD
- Host networking issue → Verify container can reach AS400
- Table not found → Check entity metadata from GlueSync

---

### Task 2: Add Checkboxes for Selective Verification

**Status:** Ready to implement  
**Estimated Time:** 2-3 hours  
**Files:** `backend/main.py`, dashboard HTML/JS

**Backend Changes:**

```python
# Add Pydantic model
class VerifyRequest(BaseModel):
    entity_ids: Optional[List[str]] = None  # None = verify all

# Update endpoint
@app.post("/api/verify/{pipeline_id}/run")
def start_verify(pipeline_id: str, request: VerifyRequest = None):
    client = get_gluesync_client()
    all_entities = client.list_entities(pipeline_id)
    
    # Filter to selected entities
    if request and request.entity_ids:
        entities = [e for e in all_entities if e['entityId'] in request.entity_ids]
    else:
        entities = all_entities
    
    # ... rest of logic
```

**Frontend Changes:**
- Add checkbox column to verify table
- Add "Select All" checkbox in header
- Pass selected entity_ids to API call
- Disable "Run Verification" if none selected

---

### Task 3: Get Entity Status from GlueSync

**Status:** Ready to implement  
**Estimated Time:** 1-2 hours  
**File:** `backend/main.py`, dashboard HTML/JS

**Status Values Found:**
- `status` field: `"running"`, `"stopped"`, `""`
- Mode detection from `agentEntities[0].entityType.type`: `"CDC"`, `"SNAPSHOT"`

**Display Badges:**
- 🟦 **CDC** (blue) - Active replication
- 🟨 **SNAPSHOT** (yellow) - One-time snapshot
- ⬜ **STOPPED** (gray) - Not running

**Implementation:**

```python
# Add to entity result
def get_entity_status_display(entity: dict) -> dict:
    status = (entity.get('status', '') or '').upper()
    agent_entities = entity.get('agentEntities', [])
    
    mode = "NOT STARTED"
    if status in ['RUNNING', 'ACTIVE']:
        source_agent = next((a for a in agent_entities if a.get('agentType') == 'SOURCE'), None)
        if source_agent:
            entity_type = source_agent.get('entityType', {}).get('type', '').upper()
            mode = "SNAPSHOT" if 'SNAPSHOT' in entity_type else "CDC"
    
    return {
        'status': status,
        'mode': mode,
        'is_active': status in ['RUNNING', 'ACTIVE']
    }
```

**UI:** Add status column to verify table with colored badges

---

### Task 4: Create "Compare Data" Tab

**Status:** Mockup created, ready to implement  
**Estimated Time:** 8-12 hours  
**Files:** New backend endpoints, new frontend tab

**Mockup:** See [COMPARE_DATA_MOCKUP.html](file:///home/ubuntu/_qoder/replica-mon/COMPARE_DATA_MOCKUP.html)

**Open mockup in browser to review design:**
```bash
firefox /home/ubuntu/_qoder/replica-mon/COMPARE_DATA_MOCKUP.html
# or
google-chrome /home/ubuntu/_qoder/replica-mon/COMPARE_DATA_MOCKUP.html
```

**Features Designed:**
- ✅ Side-by-side comparison (Source left, Target right)
- ✅ Primary key-based matching
- ✅ Color-coded differences (🔴 red highlights)
- ✅ Missing record indicators (◀ Source only, ▶ Target only)
- ✅ Summary bar with counts
- ✅ Filter buttons (All/Matched/Different/Missing)
- ✅ Search by PK value
- ✅ Pagination (100 records per page)
- ✅ Legend explaining color coding

**Research References:**

| Tool | Key Features Borrowed |
|------|----------------------|
| **Redgate SQL Compare** | Side-by-side view, row-by-row comparison |
| **dbForge Data Compare** | PK-based matching, visual diff highlighting |
| **Beyond Compare** | Excellent color coding, left/right panel layout |
| **Apache DataCompare** | Free, web-based, column-level diff |

**Backend APIs Needed:**

```python
# 1. Get schema (primary keys + columns)
GET /api/compare/{pipeline_id}/entity/{entity_id}/schema

# 2. Compare records
POST /api/compare/{pipeline_id}/entity/{entity_id}/records
Body: {
    "page": 1,
    "page_size": 100,
    "filter": "all|matched|different|source_only|target_only",
    "search_pk": "123"  # Optional
}

# 3. Get comparison summary
GET /api/compare/{pipeline_id}/entity/{entity_id}/summary
```

---

## 📚 Documents Created

1. **[ARCHITECTURE_ANALYSIS.md](file:///home/ubuntu/_qoder/replica-mon/ARCHITECTURE_ANALYSIS.md)**
   - Part A: compare.py impact analysis
   - Part B: Record-level comparison design
   - GlueSync API capability assessment

2. **[COMPREHENSIVE_ARCHITECTURE.md](file:///home/ubuntu/_qoder/replica-mon/COMPREHENSIVE_ARCHITECTURE.md)**
   - Complete system architecture
   - Component breakdown
   - Data flow diagrams
   - Known issues & technical debt
   - Troubleshooting guide

3. **[IMPLEMENTATION_PLAN_PHASE_1.5.md](file:///home/ubuntu/_qoder/replica-mon/IMPLEMENTATION_PLAN_PHASE_1.5.md)**
   - Detailed implementation steps for all 4 tasks
   - Code snippets
   - Risk assessment
   - Dependencies

4. **[COMPARE_DATA_MOCKUP.html](file:///home/ubuntu/_qoder/replica-mon/COMPARE_DATA_MOCKUP.html)**
   - Fully functional HTML/CSS mockup
   - Interactive filter buttons
   - Realistic sample data
   - Color-coded diff highlighting

---

## 🚀 Recommended Implementation Order

### Priority 1: Task 1 (Fix AS400 Count) - TODAY
**Why:** Blocking issue, affects existing functionality  
**Time:** 1-2 hours  
**Action:** Check logs, test connection, fix errors

### Priority 2: Task 3 (Entity Status) - TODAY
**Why:** Quick win, enhances existing verify tool  
**Time:** 1-2 hours  
**Action:** Extract status from GlueSync response, add badges

### Priority 3: Task 2 (Checkboxes) - TOMORROW
**Why:** Improves user experience  
**Time:** 2-3 hours  
**Action:** Add UI checkboxes, update backend endpoint

### Priority 4: Task 4 (Compare Data Tab) - THIS WEEK
**Why:** Major new feature, requires careful implementation  
**Time:** 8-12 hours  
**Action:** Review mockup, implement backend APIs, build frontend

---

## ⚠️ Critical Findings

1. **Journal Cache is NEVER UPDATED**
   - monitor.py not running in dashboard container
   - Cache is stale from last CLI run
   - **Fix in Phase 2:** Move monitoring to backend background task

2. **CLI Tools are BROKEN**
   - compare.py and monitor.py cannot run via replica-mon.sh
   - Containerfile ENTRYPOINT hardcoded to uvicorn
   - **Fix in Phase 2:** Change to CMD, allow --entrypoint override

3. **replica-msdk is Working**
   - GlueSyncClient ✅
   - GlueSyncWebSocketClient ✅
   - Used by backend successfully ✅

4. **GlueSync API Supports PK Discovery**
   - `GET /pipelines/{id}/agents/{agent_id}/discovery/columns`
   - Returns: `{"columns": [...], "keys": [...]}`
   - **Can use for Compare Data feature!** ✅

---

## 🎯 Next Action

**Start with Task 1 RIGHT NOW:**

```bash
# Step 1: Check logs for AS400 errors
podman logs --tail 200 replica-mon 2>&1 | grep -i "verify\|as400\|error"

# Step 2: Test AS400 connection
podman exec replica-mon python3 -c "
import os
from qadmcli.config import load_config
from qadmcli.db.connection import AS400ConnectionManager
from pathlib import Path

config_path = Path('/app/qadmcli/config/connection.yaml')
print(f'Config exists: {config_path.exists()}')

if config_path.exists():
    config = load_config(config_path)
    print(f'AS400 Host: {config.as400.host}')
    print(f'AS400 User: {config.as400.user}')
    
    # Test connection
    try:
        with AS400ConnectionManager(config) as conn:
            cursor = conn.execute('SELECT 1 FROM SYSIBM.SYSDUMMY1')
            result = cursor.fetchone()
            print(f'✅ Connection successful: {result}')
    except Exception as e:
        print(f'❌ Connection failed: {e}')
        import traceback
        traceback.print_exc()
"

# Step 3: Run verify and watch logs
podman logs -f replica-mon 2>&1 | grep -i "verify"
# (In another terminal, click "Run Verification" in browser)
```

**Share the output with me and I'll help fix any errors!** 🔧

---

## 📊 Progress Tracking

| Task | Status | Time Est. | Actual Time | Notes |
|------|--------|-----------|-------------|-------|
| Task 1: Fix AS400 Count | 🔴 TODO | 1-2h | - | Blocking issue |
| Task 2: Checkboxes | ⚪ TODO | 2-3h | - | Depends on Task 1 |
| Task 3: Entity Status | ⚪ TODO | 1-2h | - | Quick win |
| Task 4: Compare Data | ⚪ TODO | 8-12h | - | Major feature |

**Total Estimated Time:** 12-19 hours

---

**Ready to start implementing? Let me know which task to begin with!** 🚀
