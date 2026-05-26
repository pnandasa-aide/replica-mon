import os
import sys
import asyncio
import json
import sqlite3
import threading
import logging
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import urllib.request
import urllib.parse

# Configure logging for background threads
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
    force=True
)
logger = logging.getLogger(__name__)

# Ensure replica-msdk can be imported
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from replica_msdk import GlueSyncClient, GlueSyncWebSocketClient, parse_protobuf

app = FastAPI(title="Replica-Mon Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
GLUESYNC_URL = os.getenv("GLUESYNC_HOST", "https://localhost:1717")
ADMIN_PASS = os.getenv("GLUESYNC_ADMIN_PASSWORD") or os.getenv("ADMIN_PASS")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

# SQLite time-series store — use absolute path to avoid --app-dir __file__ resolution issues
METRICS_DB_PATH = os.getenv("METRICS_DB_PATH", "/app/replica-mon/metrics/ws_metrics.db")
REPORTS_DIR = os.getenv("REPORTS_DIR", "/app/replica-mon/compare/reports")

# Mount static reports directory
os.makedirs(REPORTS_DIR, exist_ok=True)
app.mount("/reports", StaticFiles(directory=REPORTS_DIR), name="reports")

# ─────────────────────────────────────────────
# SQLite Time-Series Store
# ─────────────────────────────────────────────

def init_metrics_db():
    """Initialize the SQLite time-series database for I/U/D breakdown storage."""
    os.makedirs(os.path.dirname(METRICS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(METRICS_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ws_metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            pipeline_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            inserts     INTEGER DEFAULT 0,
            updates     INTEGER DEFAULT 0,
            deletes     INTEGER DEFAULT 0,
            total_ops   INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entity_status_cache (
            pipeline_id TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            status      TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (pipeline_id, entity_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_health_cache (
            pipeline_id       TEXT NOT NULL,
            agent_id          TEXT NOT NULL,
            agent_type        TEXT,
            connection_status TEXT NOT NULL,
            health_status     TEXT NOT NULL,
            connected_db_host TEXT,
            connected_db_name TEXT,
            connection_error  TEXT,
            updated_at        TEXT NOT NULL,
            PRIMARY KEY (pipeline_id, agent_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_entity_time
        ON ws_metrics(entity_name, captured_at)
    """)
    
    # Scheduler & Profiling System Tables
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS report_profiles (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT NOT NULL,
            pipeline_id       TEXT NOT NULL,
            entities          TEXT NOT NULL, -- JSON array of entity names
            skip_if_all_pass  INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mailer_profiles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            emails        TEXT NOT NULL, -- Comma-separated
            subject       TEXT NOT NULL,
            body_header   TEXT,
            body_ending   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_jobs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            name              TEXT NOT NULL,
            pipeline_id       TEXT NOT NULL,
            cron_expression   TEXT NOT NULL,
            report_profile_id INTEGER,
            mailer_profile_id INTEGER,
            enabled           INTEGER DEFAULT 1,
            FOREIGN KEY(report_profile_id) REFERENCES report_profiles(id),
            FOREIGN KEY(mailer_profile_id) REFERENCES mailer_profiles(id)
        )
    """)
    
    # Set default global log path
    conn.execute("""
        INSERT OR IGNORE INTO scheduler_settings (key, value)
        VALUES ('log_path', '/app/replica-mon/logs/scheduler.log')
    """)

    # Auto-cleanup: keep last 30 days
    conn.execute("""
        DELETE FROM ws_metrics
        WHERE captured_at < datetime('now', '-30 days')
    """)
    conn.commit()
    conn.close()

# Thread-local SQLite connections (SQLite is not thread-safe with shared connections)
_db_local = threading.local()

def get_db():
    if not hasattr(_db_local, 'conn') or _db_local.conn is None:
        _db_local.conn = sqlite3.connect(METRICS_DB_PATH, check_same_thread=False)
        _db_local.conn.row_factory = sqlite3.Row
    return _db_local.conn

def store_metrics(pipeline_id: str, entity_name: str, inserts: int, updates: int, deletes: int, total_ops: int):
    """Store a metrics snapshot to SQLite. Called from the WebSocket message handler."""
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO ws_metrics (captured_at, pipeline_id, entity_name, inserts, updates, deletes, total_ops)
               VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?)""",
            (pipeline_id, entity_name, inserts, updates, deletes, total_ops)
        )
        conn.commit()
    except Exception as e:
        print(f"[metrics-db] Error storing metrics: {e}")

def store_entity_status(pipeline_id: str, entity_name: str, status: str):
    """Store/update entity status in SQLite cache."""
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO entity_status_cache (pipeline_id, entity_name, status, updated_at)
               VALUES (?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(pipeline_id, entity_name) DO UPDATE SET
               status=excluded.status, updated_at=excluded.updated_at""",
            (pipeline_id, entity_name, status)
        )
        conn.commit()
    except Exception as e:
        print(f"[metrics-db] Error caching entity status: {e}")

def get_all_cached_statuses(pipeline_id: str) -> dict:
    """Get all cached statuses for a pipeline."""
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT entity_name, status FROM entity_status_cache WHERE pipeline_id = ?",
            (pipeline_id,)
        ).fetchall()
        return {r["entity_name"]: r["status"] for r in rows}
    except Exception as e:
        print(f"[metrics-db] Error reading cached entity statuses: {e}")
        return {}

def store_agent_health(pipeline_id: str, agent_id: str, agent_type: str,
                       connection_status: str, health_status: str,
                       connected_db_host: str = None, connected_db_name: str = None,
                       connection_error: str = None):
    """Store/update agent health info in SQLite cache."""
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO agent_health_cache
               (pipeline_id, agent_id, agent_type, connection_status, health_status,
                connected_db_host, connected_db_name, connection_error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(pipeline_id, agent_id) DO UPDATE SET
               agent_type=excluded.agent_type,
               connection_status=excluded.connection_status,
               health_status=excluded.health_status,
               connected_db_host=excluded.connected_db_host,
               connected_db_name=excluded.connected_db_name,
               connection_error=excluded.connection_error,
               updated_at=excluded.updated_at""",
            (pipeline_id, agent_id, agent_type, connection_status, health_status,
             connected_db_host, connected_db_name, connection_error)
        )
        conn.commit()
    except Exception as e:
        print(f"[metrics-db] Error caching agent health: {e}")

def get_all_agent_health(pipeline_id: str) -> list:
    """Get all cached agent health records for a pipeline."""
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT agent_id, agent_type, connection_status, health_status,
                      connected_db_host, connected_db_name, connection_error, updated_at
               FROM agent_health_cache WHERE pipeline_id = ?""",
            (pipeline_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[metrics-db] Error reading agent health: {e}")
        return []

# Initialize DB on startup
init_metrics_db()

# ─────────────────────────────────────────────
# GlueSync Client Helper
# ─────────────────────────────────────────────

# Global token cache for proxy endpoint
_gluesync_token = None
_gluesync_token_expiry = 0

def get_gluesync_token() -> str:
    """Get or refresh GlueSync Bearer token."""
    global _gluesync_token, _gluesync_token_expiry
    
    import time
    import httpx
    
    # Return cached token if still valid (5 min expiry)
    if _gluesync_token and time.time() < _gluesync_token_expiry:
        return _gluesync_token
    
    # Login to get new token
    try:
        with httpx.Client(verify=False, timeout=10.0) as client:
            resp = client.post(
                f"{GLUESYNC_URL}/authentication/login",
                json={"username": "admin", "password": ADMIN_PASS}
            )
            resp.raise_for_status()
            data = resp.json()
            _gluesync_token = data.get("apiToken")
            _gluesync_token_expiry = time.time() + 300  # 5 minutes
            print(f"[auth] Got new GlueSync token", flush=True)
            return _gluesync_token
    except Exception as e:
        print(f"[auth] Failed to get GlueSync token: {e}", flush=True)
        raise

def get_gluesync_client() -> GlueSyncClient:
    if not ADMIN_PASS:
        raise Exception("GLUESYNC_ADMIN_PASSWORD environment variable is required")
    return GlueSyncClient(GLUESYNC_URL, "admin", ADMIN_PASS, verify_ssl=False)

# ─────────────────────────────────────────────
# Static File Serving (Dashboard HTML)
# ─────────────────────────────────────────────

_dashboard_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

@app.get("/", include_in_schema=False)
def serve_dashboard():
    html_path = os.path.join(_dashboard_dir, "dashboard_mockup.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Dashboard HTML not found")

@app.get("/wss", include_in_schema=False)
def serve_ws_viewer():
    html_path = os.path.join(_dashboard_dir, "ws_viewer.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="WS Viewer HTML not found")

# ─────────────────────────────────────────────
# Pipeline & Entity REST Endpoints
# ─────────────────────────────────────────────

@app.get("/api/pipelines")
def get_pipelines():
    try:
        client = get_gluesync_client()
        pipelines = client.list_pipelines()
        # Normalize: GlueSync returns 'id', dashboard expects 'pipelineId'
        normalized = []
        for p in (pipelines if isinstance(pipelines, list) else []):
            if isinstance(p, dict):
                p.setdefault('pipelineId', p.get('id', ''))
                p.setdefault('name', p.get('pipelineName', p.get('id', '')))
            normalized.append(p)
        return normalized
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/pipelines/{pipeline_id}/entities")
def get_entities(pipeline_id: str):
    try:
        client = get_gluesync_client()
        entities = client.list_entities(pipeline_id)
        
        # Get cached statuses from WebSocket
        cached_statuses = get_all_cached_statuses(pipeline_id)
        
        # Get agent health info to annotate entities
        agent_health_list = get_all_agent_health(pipeline_id)
        # Build agentId -> health map
        agent_health_map = {ah['agent_id']: ah for ah in agent_health_list}
        
        # Get agents config to know which agent is SOURCE vs TARGET
        try:
            agents_config = client.list_agents(pipeline_id)
        except:
            agents_config = []
        src_agent_id = next((a['agentId'] for a in agents_config if a.get('agentType') == 'SOURCE'), None)
        tgt_agent_id = next((a['agentId'] for a in agents_config if a.get('agentType') == 'TARGET'), None)
        
        # Build target agent health for annotation
        tgt_health = agent_health_map.get(tgt_agent_id, {}) if tgt_agent_id else {}
        src_health = agent_health_map.get(src_agent_id, {}) if src_agent_id else {}
        
        # Get recent metrics activity to infer status
        try:
            conn = get_db()
            # Check if entity has metrics in last 60 seconds
            recent_entities = conn.execute(
                """SELECT DISTINCT entity_name FROM ws_metrics 
                   WHERE pipeline_id = ? 
                   AND captured_at >= datetime('now', 'localtime', '-60 seconds')""",
                (pipeline_id,)
            ).fetchall()
            active_from_metrics = {r["entity_name"] for r in recent_entities}
        except Exception as e:
            print(f"[entities] Error checking metrics activity: {e}")
            active_from_metrics = set()
        
        for ent in (entities or []):
            if isinstance(ent, dict):
                name = ent.get('entityName')
                
                # Priority 1: Use explicit status from WebSocket cache
                if name in cached_statuses:
                    ent['status'] = cached_statuses[name]
                # Priority 2: Infer from recent metrics activity
                elif name in active_from_metrics:
                    ent['status'] = 'RUNNING'
                # Priority 3: Default to STOPPED
                else:
                    ent['status'] = 'STOPPED'

                # Inject agent health
                ent['targetAgentHealth'] = {
                    'connectionStatus': tgt_health.get('connection_status', 'UNKNOWN'),
                    'healthStatus': tgt_health.get('health_status', 'UNKNOWN'),
                    'connectedDbHost': tgt_health.get('connected_db_host'),
                    'connectedDbName': tgt_health.get('connected_db_name'),
                    'connectionError': tgt_health.get('connection_error'),
                    'updatedAt': tgt_health.get('updated_at'),
                } if tgt_health else None
                ent['sourceAgentHealth'] = {
                    'connectionStatus': src_health.get('connection_status', 'UNKNOWN'),
                    'healthStatus': src_health.get('health_status', 'UNKNOWN'),
                    'connectedDbHost': src_health.get('connected_db_host'),
                    'connectedDbName': src_health.get('connected_db_name'),
                    'connectionError': src_health.get('connection_error'),
                    'updatedAt': src_health.get('updated_at'),
                } if src_health else None
        
        return entities
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Verify Tool — Source ↔ Target Reconciliation
# ─────────────────────────────────────────────

def _load_replica_cli_config() -> dict:
    """Load replica-cli/config.json if present — used as fallback for connection params."""
    candidates = [
        os.path.join(os.path.dirname(__file__), "../../../replica-cli/config.json"),
        "/app/replica-cli/config.json",
    ]
    for path in candidates:
        try:
            with open(os.path.normpath(path)) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _count_mssql(schema: str, table: str):
    """Return (count, last_timestamp_str) from MSSQL target table."""
    import pyodbc

    # Load config.json as fallback for host/database
    cfg = _load_replica_cli_config()
    tgt_conn = cfg.get("target_agent", {}).get("connection", {})

    server   = os.getenv("MSSQL_HOST")   or tgt_conn.get("host", "")
    database = os.getenv("MSSQL_DATABASE") or tgt_conn.get("database_name", "")
    user     = os.getenv("MSSQL_USER", "")
    password = os.getenv("MSSQL_PASSWORD", "")
    driver   = os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")

    if not server or not database or not user:
        raise ValueError(
            f"Missing MSSQL config: HOST='{server}' DB='{database}' USER='{user}'"
        )

    conn_str = (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
                f"UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=yes;")
    print(f"[verify] Connecting: SERVER={server} DB={database}", flush=True)
    conn = pyodbc.connect(conn_str, timeout=5)
    cursor = conn.cursor()
    full_table = f"[{schema}].[{table}]"
    cursor.execute(f"SELECT COUNT(*) FROM {full_table}")
    count = cursor.fetchone()[0]
    print(f"[verify]   {full_table} count={count}", flush=True)

    last_ts = None
    for ts_col in ["LastUpdate", "last_update", "UPDATED_AT", "UpdatedAt", "CREATED_AT"]:
        try:
            cursor.execute(f"SELECT MAX([{ts_col}]) FROM {full_table}")
            row = cursor.fetchone()
            if row and row[0]:
                last_ts = str(row[0])
                break
        except Exception:
            continue
    conn.close()
    return count, last_ts


def _get_qadmcli_config_path() -> str:
    """Find qadmcli connection.yaml."""
    candidates = [
        "/app/qadmcli/config/connection.yaml",
        "/opt/qadmcli/config/connection.yaml",
        os.path.normpath(os.path.join(os.path.dirname(__file__), "../../../qadmcli/config/connection.yaml")),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""


def _count_as400(library: str, table: str) -> tuple:
    """Return (count, None) from AS400 source using qadmcli jaydebeapi connection."""
    import sys
    sys.stderr.write(f"[verify] AS400 → Starting count for {library}.{table}\n")
    sys.stderr.flush()
    logger.info(f"[verify] AS400 → Starting count for {library}.{table}")
    
    try:
        from qadmcli.config import load_config
        from qadmcli.db.connection import AS400ConnectionManager
        from pathlib import Path

        config_path = _get_qadmcli_config_path()
        sys.stderr.write(f"[verify] AS400 → Config path: {config_path}\n")
        sys.stderr.flush()
        logger.info(f"[verify] AS400 → Config path: {config_path}")
        
        if not config_path:
            raise FileNotFoundError("qadmcli connection.yaml not found")

        config = load_config(Path(config_path))
        sys.stderr.write(f"[verify] AS400 → Config loaded: host={config.as400.host}\n")
        sys.stderr.flush()
        logger.info(f"[verify] AS400 → Config loaded: host={config.as400.host}")

        # Override credentials from env if set
        as400_user = os.getenv("AS400_USER")
        as400_pass = os.getenv("AS400_PASSWORD")
        if as400_user or as400_pass:
            config.as400 = config.as400.copy_with_overrides(
                user=as400_user or config.as400.user,
                password=as400_pass or config.as400.password,
            )
            sys.stderr.write(f"[verify] AS400 → Credentials overridden from env\n")
            sys.stderr.flush()
            logger.info(f"[verify] AS400 → Credentials overridden from env")

        sys.stderr.write(f"[verify] AS400 Connecting: {config.as400.host} {library}.{table}\n")
        sys.stderr.flush()
        logger.info(f"[verify] AS400 Connecting: {config.as400.host} {library}.{table}")
        with AS400ConnectionManager(config) as conn:
            sys.stderr.write(f"[verify] AS400 → Connected, executing COUNT query...\n")
            sys.stderr.flush()
            logger.info(f"[verify] AS400 → Connected, executing COUNT query...")
            cursor = conn.execute(f"SELECT COUNT(*) FROM {library}.{table}")
            row = cursor.fetchone()
            count = int(row[0]) if row else 0
            cursor.close()
            sys.stderr.write(f"[verify] AS400 → Query result: {count}\n")
            sys.stderr.flush()
            logger.info(f"[verify] AS400 → Query result: {count}")

        sys.stderr.write(f"[verify]   AS400 {library}.{table} count={count}\n")
        sys.stderr.flush()
        logger.info(f"[verify]   AS400 {library}.{table} count={count}")
        return count, None

    except Exception as e:
        import traceback
        sys.stderr.write(f"[verify] AS400 ✗ Exception: {e}\n")
        sys.stderr.write(f"[verify] AS400 Traceback: {traceback.format_exc()}\n")
        sys.stderr.flush()
        logger.error(f"[verify] AS400 ✗ Exception: {e}")
        logger.error(f"[verify] AS400 Traceback: {traceback.format_exc()}")
        raise RuntimeError(str(e)) from e


# ── Async Job Store (in-memory) ──────────────────────────────────────────────
# Stores verify job results keyed by pipeline_id
_verify_jobs: dict = {}   # pipeline_id → { "status": "running"|"done", "results": [...], "started_at": ... }
_verify_lock = threading.Lock()



def _verify_worker(pipeline_id: str, entities: list):
    """Background thread: counts each entity one by one, updates job state incrementally."""
    import sys
    sys.stderr.write(f"[verify] [{pipeline_id}] ===== Worker STARTED, {len(entities)} entities =====\n")
    sys.stderr.flush()
    print(f"[verify] [{pipeline_id}] ===== Worker STARTED, {len(entities)} entities =====", flush=True)
    logger.info(f"[verify] [{pipeline_id}] Worker STARTED, {len(entities)} entities")
    
    with _verify_lock:
        _verify_jobs[pipeline_id] = {
            "status": "running",
            "results": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "total": len(entities),
            "done_count": 0,
        }

    for ent in entities:
        agents    = ent.get("agentEntities", [])
        src_agent = agents[0] if len(agents) > 0 else {}
        tgt_agent = agents[1] if len(agents) > 1 else {}

        src_info   = src_agent.get("table", {})
        src_library = src_info.get("schema", "")
        src_table   = src_info.get("name", "")

        tgt_info   = tgt_agent.get("table", {})
        tgt_schema = tgt_info.get("schema", "")
        tgt_table  = tgt_info.get("name", "")

        result = {
            "entity_name":    ent.get("entityName", ""),
            "src_library":    src_library,
            "src_table":      src_table,
            "tgt_schema":     tgt_schema,
            "tgt_table":      tgt_table,
            "source_count":   None,
            "source_last_ts": None,
            "target_count":   None,
            "target_last_ts": None,
            "diff":           None,
            "error":          None,
        }

        logger.info(f"[verify] [{pipeline_id}] {result['entity_name']}  src={src_library}.{src_table}  tgt={tgt_schema}.{tgt_table}")

        # ── Source COUNT (AS400) ──
        if src_library and src_table:
            try:
                logger.info(f"[verify]   AS400 → Counting {src_library}.{src_table}...")
                src_cnt, src_ts = _count_as400(src_library, src_table)
                result["source_count"]   = src_cnt
                result["source_last_ts"] = src_ts or "—"
                logger.info(f"[verify]   AS400 ✓ Count={src_cnt}")
            except Exception as e:
                import traceback
                error_msg = str(e)
                error_detail = f"Error: {error_msg[:200]}"
                result["source_last_ts"] = error_detail
                result["error"] = error_detail
                logger.error(f"[verify]   AS400 ✗ {error_msg}")
                logger.error(f"[verify]   AS400 Traceback: {traceback.format_exc()}")
        else:
            result["source_last_ts"] = "No source table info"
            logger.warning(f"[verify]   AS400 ⚠ No source table info (library={src_library}, table={src_table})")

        # ── Target COUNT (MSSQL) ──
        if tgt_schema and tgt_table:
            try:
                cnt, ts = _count_mssql(tgt_schema, tgt_table)
                result["target_count"]   = cnt
                result["target_last_ts"] = ts
            except Exception as e:
                err = str(e)
                result["error"] = err[:200]
                print(f"[verify]   MSSQL ✗ {err}", flush=True)
        else:
            result["error"] = "No target table info"

        # ── Difference ──
        if result["source_count"] is not None and result["target_count"] is not None:
            result["diff"] = result["source_count"] - result["target_count"]

        # Append result and increment counter atomically
        with _verify_lock:
            _verify_jobs[pipeline_id]["results"].append(result)
            _verify_jobs[pipeline_id]["done_count"] += 1

    with _verify_lock:
        _verify_jobs[pipeline_id]["status"] = "done"
        _verify_jobs[pipeline_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
    print(f"[verify] [{pipeline_id}] All done ({len(entities)} entities)", flush=True)



class VerifyRunRequest(BaseModel):
    entities: Optional[List[str]] = None


class ApiProxyRequest(BaseModel):
    target_url: str
    method: str = "GET"
    headers: Optional[Dict[str, str]] = None
    body: Optional[Any] = None



@app.post("/api/verify/{pipeline_id}/run")
def start_verify(pipeline_id: str, req: Optional[VerifyRunRequest] = None):
    """
    Start async verification job. Returns immediately with job metadata.
    Poll GET /api/verify/{pipeline_id}/results for incremental results.
    """
    import sys
    sys.stderr.write(f"[verify] ===== start_verify CALLED pipeline={pipeline_id} =====\n")
    sys.stderr.flush()
    print(f"[verify] ===== start_verify CALLED pipeline={pipeline_id} =====", flush=True)
    logger.info(f"[verify] start_verify pipeline={pipeline_id}")
    
    try:
        client   = get_gluesync_client()
        entities = client.list_entities(pipeline_id)
        
        # Filter entities if a specific list was provided in the request
        if req and req.entities is not None:
            sys.stderr.write(f"[verify] Filtering entities list from {len(entities)} to requested ones: {req.entities}\n")
            sys.stderr.flush()
            entities = [e for e in entities if e.get('entityName') in req.entities]
            
        sys.stderr.write(f"[verify] Found {len(entities)} entities to count\n")
        sys.stderr.flush()
        print(f"[verify] Found {len(entities)} entities to count", flush=True)
    except Exception as e:
        sys.stderr.write(f"[verify] ERROR: {e}\n")
        sys.stderr.flush()
        print(f"[verify] ERROR: {e}", flush=True)
        raise HTTPException(status_code=500, detail=f"Failed to list entities: {e}")

    if not entities:
        raise HTTPException(status_code=404, detail="No entities found (or selected) for verification in this pipeline")

    # Kick off background worker
    sys.stderr.write(f"[verify] Starting background worker thread...\n")
    sys.stderr.flush()
    print(f"[verify] Starting background worker thread...", flush=True)
    t = threading.Thread(target=_verify_worker, args=(pipeline_id, entities), daemon=True)
    t.start()

    return {
        "status": "started",
        "pipeline_id": pipeline_id,
        "total": len(entities),
        "poll_url": f"/api/verify/{pipeline_id}/results",
    }


@app.get("/api/verify/debug")
def verify_debug():
    """Debug endpoint to test if code changes are loaded"""
    import sys
    sys.stderr.write("[DEBUG] Debug endpoint called!\n")
    sys.stderr.flush()
    return {
        "message": "Debug endpoint working",
        "python_version": sys.version,
        "stderr_writable": sys.stderr.writable(),
        "stdout_writable": sys.stdout.writable(),
    }


@app.get("/api/verify/{pipeline_id}/results")
def get_verify_results(pipeline_id: str):
    """
    Returns current (possibly partial) verification results.
    UI should poll this every 2s until status == 'done'.
    """
    with _verify_lock:
        job = _verify_jobs.get(pipeline_id)

    if not job:
        return {"status": "not_started", "results": [], "total": 0, "done_count": 0}

    return {
        "status":     job["status"],
        "total":      job["total"],
        "done_count": job["done_count"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "results":    list(job["results"]),
    }

# ─────────────────────────────────────────────
# Reports Generation, Saving, Listing, & Cleanup
# ─────────────────────────────────────────────

def cleanup_old_reports(retention_days: int = 30):
    """Delete report files older than X days from disk."""
    try:
        import time
        now = time.time()
        cutoff = now - (retention_days * 86400)
        count = 0
        if os.path.exists(REPORTS_DIR):
            for root, dirs, files in os.walk(REPORTS_DIR):
                for f in files:
                    if f.endswith('.html') and f.startswith('report_'):
                        file_path = os.path.join(root, f)
                        if os.path.getmtime(file_path) < cutoff:
                            os.remove(file_path)
                            count += 1
            if count > 0:
                print(f"[cleanup] Cleaned up {count} reports older than {retention_days} days.")
    except Exception as e:
        print(f"[cleanup] Error cleaning old reports: {e}")

def save_verification_report(pipeline_id: str, job: dict) -> str:
    """Generate and save a premium, self-contained HTML verification report."""
    # Use local timezone for display (matching user's host machine)
    from zoneinfo import ZoneInfo
    try:
        local_tz = ZoneInfo("Asia/Bangkok")  # Thailand timezone
    except:
        local_tz = None
    
    now_local = datetime.now(local_tz) if local_tz else datetime.now()
    timestamp = now_local.strftime("%Y%m%d_%H%M%S")
    display_time = now_local.strftime("%Y-%m-%d %H:%M:%S") + (f" {now_local.tzname()}" if now_local.tzname() else "")
    
    # Fetch pipeline name from GlueSync
    try:
        client = get_gluesync_client()
        pipeline = client.get_pipeline(pipeline_id)
        pipeline_name = pipeline.get('name', pipeline_id) if pipeline else pipeline_id
    except Exception as e:
        print(f"[report] Warning: Could not fetch pipeline name: {e}")
        pipeline_name = pipeline_id
    
    pipeline_dir = os.path.join(REPORTS_DIR, pipeline_id)
    os.makedirs(pipeline_dir, exist_ok=True)

    # Compute summary status
    results = job.get("results", [])
    total_entities = len(results)
    pass_count = sum(1 for r in results if r.get('diff') == 0)
    fail_count = sum(1 for r in results if r.get('diff') is not None and r.get('diff') != 0)
    error_count = sum(1 for r in results if r.get('error') and r.get('target_count') is None)
    summary_status = "pass" if (total_entities > 0 and fail_count == 0 and error_count == 0) else "fail"

    filename = f"report_{pipeline_id}_{timestamp}_{summary_status}.html"
    filepath = os.path.join(pipeline_dir, filename)
    
    # Generate rows
    rows_html = ""
    for r in results:
        src_cnt = f"{r.get('source_count'):,}" if r.get('source_count') is not None else "N/A"
        src_ts = r.get('source_last_ts', '—')
        tgt_cnt = f"{r.get('target_count'):,}" if r.get('target_count') is not None else "N/A"
        tgt_ts = r.get('target_last_ts', '—')
        
        diff = r.get('diff')
        if r.get('error') and r.get('target_count') is None:
            diff_html = '<span class="val-err">—</span>'
            status_html = f'<span class="badge status-error">⚠ ERROR</span>'
        elif diff is None:
            diff_html = '<span class="val-loading">N/A</span>'
            status_html = '<span class="val-loading">—</span>'
        else:
            diff_class = "diff-ok" if diff == 0 else ("diff-warn" if abs(diff) < 100 else "diff-err")
            diff_sign = "+" if diff > 0 else ""
            diff_html = f'<span class="{diff_class}">{diff_sign}{diff:,}</span>'
            
            status_class = "val-ok" if diff == 0 else ("val-warn" if abs(diff) < 100 else "val-err")
            status_text = "✓ IN SYNC" if diff == 0 else ("⚠ MINOR GAP" if abs(diff) < 100 else "✗ OUT OF SYNC")
            status_html = f'<span class="badge {status_class}">{status_text}</span>'
            
        rows_html += f"""
        <tr data-entity-name="{r.get('entity_name') or ''}" data-src-library="{r.get('src_library') or ''}" data-src-table="{r.get('src_table') or ''}" data-tgt-schema="{r.get('tgt_schema') or ''}" data-tgt-table="{r.get('tgt_table') or ''}">
            <td><strong>{r.get('entity_name')}</strong></td>
            <td><span class="sub-text">{r.get('src_library') or ''}.{r.get('src_table') or ''}</span></td>
            <td><span class="sub-text">{r.get('tgt_schema') or ''}.{r.get('tgt_table') or ''}</span></td>
            <td><strong>{src_cnt}</strong><br><span class="sub-text">{src_ts}</span></td>
            <td><strong>{tgt_cnt}</strong><br><span class="sub-text">{tgt_ts}</span></td>
            <td>{diff_html}</td>
            <td>{status_html}</td>
        </tr>
        """

    summary_badge_color = "#10b981" if summary_status == "pass" else "#ef4444"
    summary_badge_text = f"✓ ALL IN SYNC ({pass_count}/{total_entities})" if summary_status == "pass" \
        else f"✗ {fail_count + error_count} DISCREPANCY (pass: {pass_count}/{total_entities})"
        
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Replication Verification Report - {pipeline_name}</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #f8fafc;
            --bg-secondary: #f1f5f9;
            --bg-card: #ffffff;
            --border-color: #e2e8f0;
            --text-primary: #0f172a;
            --text-secondary: #64748b;
            --accent-primary: #3b82f6;
            --status-ok: #10b981;
            --status-warn: #f59e0b;
            --status-err: #ef4444;
        }}
        body {{
            background-color: var(--bg-primary);
            color: var(--text-primary);
            font-family: 'Inter', sans-serif;
            margin: 0;
            padding: 2rem;
            display: flex;
            justify-content: center;
        }}
        .container {{
            width: 100%;
            max-width: 1100px;
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 2rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }}
        h1 {{
            margin-top: 0;
            font-size: 1.8rem;
            font-weight: 700;
            letter-spacing: -0.025em;
            color: var(--accent-primary);
        }}
        .meta-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
            padding-bottom: 1.5rem;
            border-bottom: 1px solid var(--border-color);
        }}
        .meta-item .label {{
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
            margin-bottom: 0.25rem;
        }}
        .meta-item .value {{
            font-size: 1rem;
            font-weight: 600;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }}
        th {{
            text-align: left;
            padding: 1rem;
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-secondary);
            border-bottom: 2px solid var(--border-color);
        }}
        td {{
            padding: 1rem;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.9rem;
            vertical-align: middle;
        }}
        .sub-text {{
            font-size: 0.75rem;
            color: var(--text-secondary);
            font-family: 'Fira Code', monospace;
        }}
        .badge {{
            display: inline-block;
            padding: 0.25rem 0.5rem;
            border-radius: 6px;
            font-size: 0.75rem;
            font-weight: 600;
            text-align: center;
        }}
        .val-ok {{ background: rgba(16, 185, 129, 0.15); color: var(--status-ok); }}
        .val-warn {{ background: rgba(245, 158, 11, 0.15); color: var(--status-warn); }}
        .val-err {{ background: rgba(239, 68, 68, 0.15); color: var(--status-err); }}
        .diff-ok {{ color: var(--status-ok); font-family: 'Fira Code', monospace; font-weight: 600; }}
        .diff-warn {{ color: var(--status-warn); font-family: 'Fira Code', monospace; font-weight: 600; }}
        .diff-err {{ color: var(--status-err); font-family: 'Fira Code', monospace; font-weight: 600; }}
        .status-error {{ background: rgba(239, 68, 68, 0.15); color: var(--status-err); }}
        .summary-banner {{
            display: flex; align-items: center; gap: 0.75rem;
            padding: 0.75rem 1rem; border-radius: 8px; margin-bottom: 1.5rem;
            background: var(--bg-secondary); border-left: 4px solid {summary_badge_color};
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Replication Verification Report</h1>
        <div style="margin-bottom:0.5rem; color:var(--text-secondary); font-size:0.9rem;">
            Pipeline: <strong style="color:var(--accent-primary)">{pipeline_name}</strong>
            <span style="margin:0 0.5rem; color:var(--text-secondary)">|</span>
            <span style="font-family:monospace; font-size:0.8rem;">{pipeline_id}</span>
        </div>
        <div class="summary-banner">
            <span style="font-size:1.1rem; font-weight:700; color:{summary_badge_color}">{summary_badge_text}</span>
        </div>
        <div class="meta-grid">
            <div class="meta-item">
                <div class="label">Pipeline</div>
                <div class="value">{pipeline_name}</div>
            </div>
            <div class="meta-item">
                <div class="label">Generated At</div>
                <div class="value">{display_time}</div>
            </div>
            <div class="meta-item">
                <div class="label">Entities Checked</div>
                <div class="value">{total_entities}</div>
            </div>
            <div class="meta-item">
                <div class="label">In Sync / Discrepancy</div>
                <div class="value" style="color:{summary_badge_color}">{pass_count} / {fail_count + error_count}</div>
            </div>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Entity Name</th>
                    <th>Source Table</th>
                    <th>Target Table</th>
                    <th>Source (AS400) Count</th>
                    <th>Target (MSSQL) Count</th>
                    <th>Difference</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>
    </div>
</body>
</html>
"""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_content)
        
    cleanup_old_reports()
    return filename

@app.post("/api/verify/{pipeline_id}/save")
def save_verify_job_report(pipeline_id: str):
    """Save the completed verification job results as a static HTML report."""
    with _verify_lock:
        job = _verify_jobs.get(pipeline_id)
        
    if not job:
        raise HTTPException(status_code=400, detail="No verification job has been run for this pipeline.")
        
    if job.get("status") != "done":
        raise HTTPException(status_code=400, detail=f"The verification job is still in status: {job.get('status')}. Wait for it to finish.")
        
    try:
        filename = save_verification_report(pipeline_id, job)
        report_url = f"/reports/{pipeline_id}/{filename}"
        return {
            "status": "success",
            "message": "Report saved successfully",
            "filename": filename,
            "url": report_url
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save report: {str(e)}")

@app.get("/api/reports")
def list_reports(
    pipeline_id: str = None,
    search: str = None,
    status: str = None,
    from_date: str = None,
    to_date: str = None
):
    """List all saved verification reports with advanced backend filtering."""
    reports = []
    
    # Build pipeline name cache
    pipeline_names = {}
    try:
        client = get_gluesync_client()
        pipelines = client.list_pipelines()
        for p in pipelines:
            pipeline_names[p['id']] = p.get('name', p['id'])
    except Exception as e:
        print(f"[reports] Warning: Could not fetch pipeline names: {e}")
    
    try:
        if not os.path.exists(REPORTS_DIR):
            return []
            
        subdirs = [pipeline_id] if pipeline_id else os.listdir(REPORTS_DIR)
        
        for subdir in subdirs:
            subdir_path = os.path.join(REPORTS_DIR, subdir)
            if not os.path.isdir(subdir_path):
                continue
            
            # Get pipeline name from cache
            pipeline_name = pipeline_names.get(subdir, subdir)
                
            for f in os.listdir(subdir_path):
                if f.endswith('.html') and f.startswith('report_'):
                    filepath = os.path.join(subdir_path, f)
                    stat = os.stat(filepath)
                    # Filename format: report_{pipeline_id}_{YYYYMMDD}_{HHMMSS}_{status}.html
                    base = f.replace('.html', '')
                    parts = base.split('_')
                    timestamp_str = "Unknown"
                    summary_status = None
                    entity_count = 0
                    
                    # Last part may be 'pass' or 'fail'
                    if parts[-1] in ('pass', 'fail'):
                        summary_status = parts[-1]
                        date_part = parts[-3] if len(parts) >= 3 else ''
                        time_part = parts[-2] if len(parts) >= 2 else ''
                    else:
                        date_part = parts[-2] if len(parts) >= 2 else ''
                        time_part = parts[-1]
                    if len(date_part) == 8 and len(time_part) == 6:
                        timestamp_str = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}"
                    
                    # Apply status filter (if provided)
                    if status and summary_status != status:
                        continue
                        
                    # Apply date filtering (if provided)
                    match_date_range = True
                    if timestamp_str != "Unknown":
                        try:
                            report_dt_str = timestamp_str.replace(' ', 'T')
                            if from_date:
                                from_val = from_date
                                if 'T' not in from_val:
                                    from_val += 'T00:00:00'
                                elif len(from_val) == 16:
                                    from_val += ':00'
                                if report_dt_str < from_val:
                                    match_date_range = False
                            if to_date:
                                to_val = to_date
                                if 'T' not in to_val:
                                    to_val += 'T23:59:59'
                                elif len(to_val) == 16:
                                    to_val += ':59'
                                if report_dt_str > to_val:
                                    match_date_range = False
                        except Exception as e:
                            print(f"[reports] Date comparison error: {e}")
                    
                    if (from_date or to_date) and not match_date_range:
                        continue
                    
                    # Extract entity count and entity names from HTML content
                    entities = []
                    entities_data = []
                    try:
                        with open(filepath, 'r', encoding='utf-8') as rf:
                            content = rf.read()
                            import re
                            # Look for "Entities Checked" value
                            match = re.search(r'<div class="label">Entities Checked</div>\s*<div class="value">(\d+)</div>', content)
                            if match:
                                entity_count = int(match.group(1))
                            
                            # Parse TR blocks for rich metadata attributes
                            tr_blocks = re.findall(r'<tr\s+([^>]+)>', content)
                            for tr_attr in tr_blocks:
                                if 'data-entity-name' in tr_attr:
                                    ent_name = (re.search(r'data-entity-name="([^"]*)"', tr_attr) or [None, ""])[1]
                                    src_lib = (re.search(r'data-src-library="([^"]*)"', tr_attr) or [None, ""])[1]
                                    src_tbl = (re.search(r'data-src-table="([^"]*)"', tr_attr) or [None, ""])[1]
                                    tgt_sch = (re.search(r'data-tgt-schema="([^"]*)"', tr_attr) or [None, ""])[1]
                                    tgt_tbl = (re.search(r'data-tgt-table="([^"]*)"', tr_attr) or [None, ""])[1]
                                    entities_data.append((ent_name, src_lib, src_tbl, tgt_sch, tgt_tbl))
                                    
                            if not entities_data:
                                old_matches = re.findall(r'<tr>\s*<td><strong>([^<]+)</strong></td>', content)
                                if old_matches:
                                    entities = old_matches
                                    entities_data = [(m, "", "", "", "") for m in old_matches]
                            else:
                                entities = [m[0] for m in entities_data]
                    except Exception as e:
                        print(f"[reports] Error parsing HTML file: {e}")
                        
                    # Apply search filters (if provided)
                    if search:
                        terms = search.split()
                        match_search = True
                        for term in terms:
                            exclude = False
                            if term.startswith('-'):
                                exclude = True
                                term_val = term[1:]
                            else:
                                term_val = term
                            
                            if not term_val:
                                continue
                                
                            try:
                                if '*' in term_val or '?' in term_val:
                                    regex_pattern = '.*'.join(re.escape(p) for p in term_val.split('*'))
                                    regex_pattern = regex_pattern.replace('\\?', '.')
                                    rx = re.compile(regex_pattern, re.IGNORECASE)
                                else:
                                    rx = re.compile(re.escape(term_val), re.IGNORECASE)
                            except Exception:
                                rx = re.compile(re.escape(term_val), re.IGNORECASE)
                                
                            term_matched = False
                            for ent_name, src_lib, src_tbl, tgt_sch, tgt_tbl in entities_data:
                                if (rx.search(ent_name) or
                                    (src_lib and rx.search(src_lib)) or
                                    (src_tbl and rx.search(src_tbl)) or
                                    (tgt_sch and rx.search(tgt_sch)) or
                                    (tgt_tbl and rx.search(tgt_tbl))):
                                    term_matched = True
                                    break
                                    
                            if exclude:
                                if term_matched:
                                    match_search = False
                                    break
                            else:
                                if not term_matched:
                                    match_search = False
                                    break
                                    
                        if not match_search:
                            continue
                            
                    reports.append({
                        "pipeline_id": subdir,
                        "pipeline_name": pipeline_name,
                        "filename": f,
                        "url": f"/reports/{subdir}/{f}",
                        "created_at": timestamp_str,
                        "summary_status": summary_status,
                        "entity_count": entity_count,
                        "entities": entities,
                        "size_bytes": stat.st_size
                    })
        reports.sort(key=lambda x: x["created_at"], reverse=True)
    except Exception as e:
        print(f"[reports] Error listing reports: {e}")
        
    return reports


@app.post("/api/proxy")
async def proxy_api_request(req: ApiProxyRequest):
    """
    Proxy API requests to GlueSync Core Hub.
    Allows browser-based API testing without CORS/SSL issues.
    """
    import httpx
    
    try:
        # Get Bearer token
        token = get_gluesync_token()
        
        # If target_url is just a path, prepend GlueSync base URL
        target_url = req.target_url
        if not target_url.startswith('http'):
            target_url = f"{GLUESYNC_URL}{target_url if target_url.startswith('/') else '/' + target_url}"
        
        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            # Prepare request with Bearer auth
            headers = req.headers or {}
            headers['Authorization'] = f'Bearer {token}'
            
            kwargs = {
                'method': req.method.upper(),
                'url': target_url,
                'headers': headers,
            }
            
            if req.body and req.method.upper() in ['POST', 'PUT', 'PATCH']:
                kwargs['json'] = req.body if isinstance(req.body, dict) else json.loads(req.body) if isinstance(req.body, str) else req.body
            
            # Make request
            response = await client.request(**kwargs)
            
            # Return response — return JSON body with status code, filtering out problematic headers
            content_type = response.headers.get('content-type', '')
            if 'application/json' in content_type:
                body = response.json()
            else:
                body = response.text
            
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=content_type or 'application/json'
            )
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Request to GlueSync timed out")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Proxy error: {str(e)}")


@app.post("/api/pipelines/{pipeline_id}/entities/{entity_id}/start")
def start_entity(pipeline_id: str, entity_id: str, with_snapshot: bool = False):
    try:
        client = get_gluesync_client()
        success = client.start_entity(pipeline_id, entity_id, with_snapshot=with_snapshot)
        if success:
            return {"status": "success", "message": f"Entity {entity_id} started"}
        raise HTTPException(status_code=500, detail="Failed to start entity")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/pipelines/{pipeline_id}/entities/{entity_id}/stop")
def stop_entity(pipeline_id: str, entity_id: str):
    try:
        client = get_gluesync_client()
        success = client.stop_entity(pipeline_id, entity_id)
        if success:
            return {"status": "success", "message": f"Entity {entity_id} stopped"}
        raise HTTPException(status_code=500, detail="Failed to stop entity")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────
# Metrics REST Endpoints (SQLite I/U/D store)
# ─────────────────────────────────────────────

@app.get("/api/metrics/{pipeline_id}/{entity_name}")
def get_entity_metrics(
    pipeline_id: str,
    entity_name: str,
    window_hours: int = Query(default=8, description="Time window in hours (e.g. 8 = since ~midnight)")
):
    """
    Returns I/U/D breakdown for a single entity over a time window.
    Uses MAX-MIN delta pattern on cumulative counters.
    """
    try:
        conn = get_db()
        row = conn.execute("""
            SELECT
                entity_name,
                COUNT(*) as samples,
                MIN(captured_at) as window_start,
                MAX(captured_at) as window_end,
                MAX(inserts) - MIN(inserts)   AS new_inserts,
                MAX(updates) - MIN(updates)   AS new_updates,
                MAX(deletes) - MIN(deletes)   AS new_deletes,
                MAX(total_ops) - MIN(total_ops) AS new_total,
                MAX(total_ops) as cumulative_total
            FROM ws_metrics
            WHERE entity_name = ?
              AND pipeline_id = ?
              AND captured_at >= datetime('now', 'localtime', ? )
        """, (entity_name, pipeline_id, f'-{window_hours} hours')).fetchone()

        if not row or row['samples'] == 0:
            return {
                "entity_name": entity_name,
                "pipeline_id": pipeline_id,
                "window_hours": window_hours,
                "message": "No data collected yet. Ensure the WebSocket stream is connected.",
                "inserts": 0, "updates": 0, "deletes": 0, "total": 0
            }

        return {
            "entity_name": row["entity_name"],
            "pipeline_id": pipeline_id,
            "window_hours": window_hours,
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "samples_collected": row["samples"],
            "inserts": max(0, row["new_inserts"] or 0),
            "updates": max(0, row["new_updates"] or 0),
            "deletes": max(0, row["new_deletes"] or 0),
            "total": max(0, row["new_total"] or 0),
            "cumulative_total": row["cumulative_total"] or 0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/metrics/{pipeline_id}")
def get_pipeline_metrics(
    pipeline_id: str,
    window_hours: int = Query(default=8, description="Time window in hours")
):
    """Returns I/U/D breakdown for all entities in a pipeline."""
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT
                entity_name,
                COUNT(*) as samples,
                MIN(captured_at) as window_start,
                MAX(captured_at) as window_end,
                MAX(inserts) - MIN(inserts)     AS new_inserts,
                MAX(updates) - MIN(updates)     AS new_updates,
                MAX(deletes) - MIN(deletes)     AS new_deletes,
                MAX(total_ops) - MIN(total_ops) AS new_total,
                MAX(total_ops)                  AS cumulative_total
            FROM ws_metrics
            WHERE pipeline_id = ?
              AND captured_at >= datetime('now', 'localtime', ?)
            GROUP BY entity_name
            ORDER BY new_total DESC
        """, (pipeline_id, f'-{window_hours} hours')).fetchall()

        return {
            "pipeline_id": pipeline_id,
            "window_hours": window_hours,
            "entities": [
                {
                    "entity_name": r["entity_name"],
                    "window_start": r["window_start"],
                    "window_end": r["window_end"],
                    "samples_collected": r["samples"],
                    "inserts": max(0, r["new_inserts"] or 0),
                    "updates": max(0, r["new_updates"] or 0),
                    "deletes": max(0, r["new_deletes"] or 0),
                    "total": max(0, r["new_total"] or 0),
                    "cumulative_total": r["cumulative_total"] or 0
                }
                for r in rows
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/metrics/{pipeline_id}/{entity_name}/hourly")
def get_entity_hourly(
    pipeline_id: str,
    entity_name: str,
    hours: int = Query(default=24, description="Number of past hours to return")
):
    """Returns hourly I/U/D breakdown for charting."""
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT
                strftime('%Y-%m-%d %H:00', captured_at) AS hour,
                MAX(inserts) - MIN(inserts)     AS new_inserts,
                MAX(updates) - MIN(updates)     AS new_updates,
                MAX(deletes) - MIN(deletes)     AS new_deletes,
                MAX(total_ops) - MIN(total_ops) AS new_total,
                COUNT(*)                        AS samples
            FROM ws_metrics
            WHERE entity_name = ?
              AND pipeline_id = ?
              AND captured_at >= datetime('now', 'localtime', ?)
            GROUP BY hour
            ORDER BY hour ASC
        """, (entity_name, pipeline_id, f'-{hours} hours')).fetchall()

        return {
            "entity_name": entity_name,
            "pipeline_id": pipeline_id,
            "hours_requested": hours,
            "data": [
                {
                    "hour": r["hour"],
                    "inserts": max(0, r["new_inserts"] or 0),
                    "updates": max(0, r["new_updates"] or 0),
                    "deletes": max(0, r["new_deletes"] or 0),
                    "total": max(0, r["new_total"] or 0),
                    "samples": r["samples"]
                }
                for r in rows
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Prometheus Proxy Endpoint
# ─────────────────────────────────────────────

@app.get("/api/prometheus/query")
def prometheus_query(
    query: str = Query(..., description="PromQL query string"),
    window: str = Query(default="8h", description="Time window e.g. 8h, 1h, 24h")
):
    """
    Proxy PromQL instant query with increase() applied over the window.
    Returns per-entity total row counts from Prometheus.
    """
    try:
        full_query = f"increase({query}[{window}])"
        encoded = urllib.parse.urlencode({"query": full_query})
        url = f"{PROMETHEUS_URL}/prometheus/api/v1/query?{encoded}"

        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())

        if data.get("status") != "success":
            raise HTTPException(status_code=502, detail="Prometheus query failed")

        results = []
        for item in data["data"]["result"]:
            metric = item["metric"]
            results.append({
                "entity_name": metric.get("entityName", "unknown"),
                "entity_id": metric.get("entityId", ""),
                "pipeline_id": metric.get("pipelineId", ""),
                "pipeline_name": metric.get("pipelineName", ""),
                "value": float(item["value"][1]),
                "timestamp": item["value"][0]
            })

        return {
            "query": full_query,
            "window": window,
            "source": "prometheus",
            "results": sorted(results, key=lambda x: -x["value"])
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Prometheus unavailable: {e}")


@app.get("/api/prometheus/throughput")
def prometheus_throughput(
    rate_window: str = Query(default="5m", description="Rate window e.g. 5m, 1m, 15m")
):
    """Returns current rows/second throughput per entity from Prometheus."""
    try:
        query = f"rate(gluesync_total_count[{rate_window}])"
        encoded = urllib.parse.urlencode({"query": query})
        url = f"{PROMETHEUS_URL}/prometheus/api/v1/query?{encoded}"

        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())

        results = []
        for item in data["data"]["result"]:
            metric = item["metric"]
            results.append({
                "entity_name": metric.get("entityName", "unknown"),
                "pipeline_name": metric.get("pipelineName", ""),
                "rows_per_second": round(float(item["value"][1]), 4),
            })

        return {
            "rate_window": rate_window,
            "source": "prometheus",
            "results": sorted(results, key=lambda x: -x["rows_per_second"])
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Prometheus unavailable: {e}")


# ─────────────────────────────────────────────
# WebSocket — Live Metrics Stream
# ─────────────────────────────────────────────

# Track which pipeline each entity belongs to (set during WS subscribe)
_entity_pipeline_map: dict = {}

@app.websocket("/ws/metrics")
async def websocket_endpoint(websocket: WebSocket, pipeline_id: str, entities: str):
    """
    WebSocket endpoint: connects to GlueSync, subscribes, decodes Protobuf,
    stores I/U/D data to SQLite, and forwards clean JSON to the browser client.
    """
    await websocket.accept()

    if not ADMIN_PASS:
        await websocket.close(code=1008, reason="No GlueSync credentials configured")
        return

    entity_list = [e.strip() for e in entities.split(",") if e.strip()]
    if not entity_list or not pipeline_id:
        await websocket.close(code=1008, reason="Missing pipeline_id or entities")
        return

    # Map entity names to pipeline for DB storage
    for name in entity_list:
        _entity_pipeline_map[name] = pipeline_id

    # Load metadata for enrichment
    metadata_cache = {}
    try:
        rest_client = get_gluesync_client()
        pipelines = rest_client.list_pipelines()
        entities_meta = rest_client.list_entities(pipeline_id)
        
        # Build metadata cache
        for p in pipelines:
            metadata_cache[p['id']] = {
                'type': 'pipeline',
                'name': p.get('name', p['id']),
                'id': p['id']
            }
        
        for ent in entities_meta:
            entity_name = ent.get('entityName', '')
            metadata_cache[entity_name] = {
                'type': 'entity',
                'name': entity_name,
                'id': ent.get('entityId', ''),
                'source_table': '',
                'target_table': ''
            }
            
            # Extract source/target table info
            agents = ent.get('agentEntities', [])
            if len(agents) > 0:
                src = agents[0].get('table', {})
                metadata_cache[entity_name]['source_table'] = f"{src.get('schema', '')}.{src.get('name', '')}"
            if len(agents) > 1:
                tgt = agents[1].get('table', {})
                metadata_cache[entity_name]['target_table'] = f"{tgt.get('schema', '')}.{tgt.get('name', '')}"
    except Exception as e:
        print(f"[ws] Warning: Could not load metadata: {e}")

    try:
        rest_client = get_gluesync_client()
        token = rest_client.token

        ws_client = GlueSyncWebSocketClient(GLUESYNC_URL, token, verify_ssl=False)
        ws_client.subscribe(pipeline_id, entity_list)

        loop = asyncio.get_event_loop()

        def on_message(data):
            try:
                parsed_data = None
                if isinstance(data, bytes):
                    parsed_data = parse_protobuf(data)
                elif isinstance(data, dict):
                    parsed_data = data

                if parsed_data and isinstance(parsed_data, dict):
                    msg_type = parsed_data.get("Field_1_string", "")

                    if msg_type == "MetricsMessage":
                        # Extract I/U/D fields and persist to SQLite
                        inner = (parsed_data
                                 .get("Field_2_message", {})
                                 .get("Field_1_message", {})
                                 .get("Field_1_message", {})
                                 .get("Field_2_message", {}))
                        if inner:
                            raw_entity = inner.get("Field_1_string", "")
                            inserts   = inner.get("Field_4_varint", 0) or 0
                            updates   = inner.get("Field_5_varint", 0) or 0
                            deletes   = inner.get("Field_6_varint", 0) or 0
                            total_ops = inner.get("Field_7_varint", 0) or 0
                            timestamp = inner.get("Field_2_string", "")  # May be timestamp

                            # Resolve entity name from metadata cache (handles both ID and name)
                            meta = metadata_cache.get(raw_entity)
                            if not meta:
                                for entity_name, entity_meta in metadata_cache.items():
                                    if entity_meta.get('id') == raw_entity:
                                        meta = entity_meta
                                        break
                            entity_name = meta.get('name', raw_entity) if meta else raw_entity

                            pid = _entity_pipeline_map.get(entity_name, pipeline_id)
                            if entity_name and total_ops > 0:
                                store_metrics(pid, entity_name, inserts, updates, deletes, total_ops)
                                # Receiving active metrics implies the entity is RUNNING
                                store_entity_status(pid, entity_name, "RUNNING")

                    elif msg_type == "EntityStatusMessage":
                        # EntityStatusMessage.content.entitiesStatus is a LIST of EntityStatus objects
                        # Each EntityStatus has: pipelineId, entityId, isMigrationActive,
                        #                        isSyncActive, isBusy, snapshotWriteMethod
                        # Protobuf field layout based on webApp.js model order:
                        #   Field_1 = pipelineId (string)
                        #   Field_2 = entityId (string)
                        #   Field_3 = isMigrationActive (bool/varint)
                        #   Field_4 = isSyncActive (bool/varint)
                        #   Field_5 = isBusy (bool/varint)
                        #   Field_6 = snapshotWriteMethod (string)
                        # The message wraps: EntityStatusMessage -> content -> entitiesStatus (list)
                        content_msg = (parsed_data
                                       .get("Field_2_message", {}))
                        # content is Field_1_message of EntityStatusMessage
                        content = content_msg.get("Field_1_message", {})
                        # entitiesStatus items — may be a single message or repeated
                        # In protobuf repeated fields each appears as Field_N_message (list)
                        # Try both list and single
                        raw_items = content.get("Field_1_message_list", None)
                        if raw_items is None:
                            single = content.get("Field_1_message")
                            raw_items = [single] if single else []

                        for item in raw_items:
                            if not item:
                                continue
                            raw_entity = item.get("Field_2_string", "")
                            is_migration = bool(item.get("Field_3_varint", 0))
                            is_sync_active = bool(item.get("Field_4_varint", 0))
                            is_busy = bool(item.get("Field_5_varint", 0))

                            # Map to status string
                            if is_sync_active:
                                status_str = 'RUNNING'
                            elif is_migration:
                                status_str = 'MIGRATING'
                            elif is_busy:
                                status_str = 'BUSY'
                            else:
                                status_str = 'STOPPED'

                            # Resolve entity name from metadata cache
                            meta = metadata_cache.get(raw_entity)
                            if not meta:
                                for ename, emeta in metadata_cache.items():
                                    if emeta.get('id') == raw_entity:
                                        meta = emeta
                                        break
                            entity_name = meta.get('name', raw_entity) if meta else raw_entity

                            pid = _entity_pipeline_map.get(entity_name, pipeline_id)
                            if entity_name:
                                store_entity_status(pid, entity_name, status_str)
                                print(f"[ws] EntityStatus: {entity_name} -> {status_str} (syncActive={is_sync_active}, busy={is_busy})", flush=True)

                    elif msg_type == "AgentInformationMessage":
                        # AgentInformationMessage.pipelineId + content.agentInformation
                        # AgentInformation fields (from webApp.js model):
                        #   Field_1=agentId, Field_2=agentNickname, Field_3=agentVersion,
                        #   Field_4=startTimestamp, Field_5=connectedDatabaseHost,
                        #   Field_6=connectedDatabaseName, Field_7=connectedDatabaseVersion,
                        #   Field_8=status (HEALTHY/UNHEALTHY enum),
                        #   Field_9=agentAddressIp, Field_10=connectionStatus (CONNECTED/DISCONNECTED enum),
                        #   Field_11=timestamp, Field_12=connectionError, Field_13=statusError
                        pid_from_msg = parsed_data.get("Field_1_string", pipeline_id)
                        content_msg = parsed_data.get("Field_2_message", {})
                        agent_info = content_msg.get("Field_1_message", {})

                        if agent_info:
                            agent_id_raw = agent_info.get("Field_1_string", "")
                            db_host = agent_info.get("Field_5_string", "")
                            db_name = agent_info.get("Field_6_string", "")
                            # status enum: 0=HEALTHY, 1=UNHEALTHY
                            health_val = agent_info.get("Field_8_varint", -1)
                            health_map = {0: 'HEALTHY', 1: 'UNHEALTHY'}
                            health_status = health_map.get(health_val, 'UNKNOWN')
                            # connectionStatus enum: 0=CONNECTED, 1=DISCONNECTED
                            conn_val = agent_info.get("Field_10_varint", -1)
                            conn_map = {0: 'CONNECTED', 1: 'DISCONNECTED'}
                            conn_status = conn_map.get(conn_val, 'UNKNOWN')
                            conn_error = agent_info.get("Field_12_string", "")

                            if agent_id_raw:
                                # Determine agent type from the agents we loaded
                                agent_type_str = 'UNKNOWN'
                                try:
                                    rest = get_gluesync_client()
                                    agents_cfg = rest.list_agents(pid_from_msg or pipeline_id)
                                    for a in agents_cfg:
                                        if a.get('agentId') == agent_id_raw:
                                            agent_type_str = a.get('agentType', 'UNKNOWN')
                                            break
                                except:
                                    pass

                                store_agent_health(
                                    pid_from_msg or pipeline_id,
                                    agent_id_raw,
                                    agent_type_str,
                                    conn_status,
                                    health_status,
                                    db_host,
                                    db_name,
                                    conn_error
                                )
                                print(f"[ws] AgentHealth: {agent_id_raw} ({agent_type_str}) -> conn={conn_status}, health={health_status}, db={db_host}/{db_name}", flush=True)

                    # Forward EntityStatusMessage, MetricsMessage, AND AgentInformationMessage to browser
                    if msg_type in ["EntityStatusMessage", "MetricsMessage", "AgentInformationMessage"]:
                        enriched_data = enrich_with_metadata(parsed_data, metadata_cache) if parsed_data else parsed_data
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_json(enriched_data or parsed_data), loop
                        )
            except Exception as e:
                print(f"[ws] Error processing message: {e}")

        await loop.run_in_executor(None, ws_client.connect, on_message)

    except WebSocketDisconnect:
        print("[ws] Client disconnected")
    except Exception as e:
        print(f"[ws] Error: {e}")
        try:
            await websocket.close(code=1011, reason=str(e)[:100])
        except:
            pass


def enrich_with_metadata(data: dict, metadata_cache: dict) -> dict:
    """
    Enrich WebSocket metrics with human-readable metadata:
    - Add entity/pipeline names with IDs
    - Add field descriptions for numeric fields
    - Convert timestamps to human-readable format
    """
    if not data:
        return data
    
    msg_type = data.get("Field_1_string", "")
    
    if msg_type == "MetricsMessage":
        inner = (data
                 .get("Field_2_message", {})
                 .get("Field_1_message", {})
                 .get("Field_1_message", {})
                 .get("Field_2_message", {}))
        
        if inner:
            # Field_1_string could be entity NAME or entity ID from WebSocket
            raw_entity = inner.get("Field_1_string", "")
            
            # Try to find in metadata cache by name first, then by ID
            meta = metadata_cache.get(raw_entity)
            
            # If not found by name, search by ID
            if not meta:
                for entity_name, entity_meta in metadata_cache.items():
                    if entity_meta.get('id') == raw_entity:
                        meta = entity_meta
                        break
            
            entity_name = meta.get('name', raw_entity) if meta else raw_entity
            entity_id = meta.get('id', raw_entity) if meta else raw_entity
            source_table = meta.get('source_table', '') if meta else ''
            target_table = meta.get('target_table', '') if meta else ''
            
            inserts = inner.get("Field_4_varint", 0) or 0
            updates = inner.get("Field_5_varint", 0) or 0
            deletes = inner.get("Field_6_varint", 0) or 0
            total_ops = inner.get("Field_7_varint", 0) or 0
            
            # Add enriched fields - show ID first, then NAME
            inner['_enriched'] = {
                'entity_display': f'{entity_id} "{entity_name}" entity',
                'entity_name': entity_name,
                'entity_id': entity_id,
                'source_table': source_table,
                'target_table': target_table,
                'fields': {
                    'Field_4_varint': {'name': 'inserts', 'value': inserts, 'description': 'Number of INSERT operations'},
                    'Field_5_varint': {'name': 'updates', 'value': updates, 'description': 'Number of UPDATE operations'},
                    'Field_6_varint': {'name': 'deletes', 'value': deletes, 'description': 'Number of DELETE operations'},
                    'Field_7_varint': {'name': 'total_ops', 'value': total_ops, 'description': 'Total operations (inserts + updates + deletes)'}
                }
            }
            
            # If there's a timestamp field, add human-readable version
            timestamp = inner.get("Field_2_string", "")
            if timestamp:
                try:
                    # Try to parse as ISO timestamp or epoch
                    ts_obj = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    inner['_enriched']['timestamp_human'] = ts_obj.strftime('%Y-%m-%d %H:%M:%S %Z')
                except:
                    try:
                        # Try as epoch milliseconds
                        ts_obj = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
                        inner['_enriched']['timestamp_human'] = ts_obj.strftime('%Y-%m-%d %H:%M:%S UTC')
                    except:
                        inner['_enriched']['timestamp_human'] = timestamp
    
    elif msg_type == "EntityStatusMessage":
        inner = (data
                 .get("Field_2_message", {})
                 .get("Field_1_message", {})
                 .get("Field_1_message", {}))
        
        if inner:
            # Field_2_string could be entity NAME or entity ID
            raw_entity = inner.get("Field_2_string", "")
            status_val = inner.get("Field_4_varint", 0)
            
            # Try to find in metadata cache by name first, then by ID
            meta = metadata_cache.get(raw_entity)
            
            # If not found by name, search by ID
            if not meta:
                for entity_name, entity_meta in metadata_cache.items():
                    if entity_meta.get('id') == raw_entity:
                        meta = entity_meta
                        break
            
            entity_name = meta.get('name', raw_entity) if meta else raw_entity
            entity_id = meta.get('id', raw_entity) if meta else raw_entity
            
            status_map = {
                0: 'STOPPED',
                1: 'RUNNING',
                2: 'PAUSED',
                3: 'ERROR'
            }
            status_str = status_map.get(status_val, f'UNKNOWN({status_val})')
            
            inner['_enriched'] = {
                'entity_display': f'{entity_id} "{entity_name}" entity',
                'entity_name': entity_name,
                'entity_id': entity_id,
                'status_code': status_val,
                'status_text': status_str,
                'status_description': f'Entity is currently {status_str.lower()}'
            }
    
    return data


# ─────────────────────────────────────────────
# Scheduler & Profiling System
# ─────────────────────────────────────────────

class SettingsRequest(BaseModel):
    log_path: str

class ReportProfileRequest(BaseModel):
    id: Optional[int] = None
    name: str
    pipeline_id: str
    entities: List[str]
    skip_if_all_pass: bool

class MailerProfileRequest(BaseModel):
    id: Optional[int] = None
    name: str
    emails: str
    subject: str
    body_header: Optional[str] = ""
    body_ending: Optional[str] = ""

class SchedulerJobRequest(BaseModel):
    id: Optional[int] = None
    name: str
    pipeline_id: str
    cron_expression: str
    report_profile_id: int
    mailer_profile_id: int
    enabled: Optional[bool] = True

def cron_matches(cron_expr: str, dt) -> bool:
    parts = cron_expr.split()
    if len(parts) != 5:
        return False
    def match_part(part, val):
        if part == '*':
            return True
        if ',' in part:
            return any(match_part(p, val) for p in part.split(','))
        if '-' in part:
            start, end = map(int, part.split('-'))
            return start <= val <= end
        if part.startswith('*/'):
            step = int(part[2:])
            return val % step == 0
        return int(part) == val
    cron_dow = (dt.weekday() + 1) % 7
    return (match_part(parts[0], dt.minute) and
            match_part(parts[1], dt.hour) and
            match_part(parts[2], dt.day) and
            match_part(parts[3], dt.month) and
            match_part(parts[4], cron_dow))

def write_scheduler_log(log_path: str, message: str):
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as lf:
            lf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception as e:
        print(f"[scheduler] Error writing log: {e}")

def save_mock_email(emails: str, subject: str, body_html: str):
    try:
        mock_dir = "/app/replica-mon/cache/mock_emails"
        os.makedirs(mock_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"email_{timestamp}.html"
        filepath = os.path.join(mock_dir, filename)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"<!-- TO: {emails} -->\n<!-- SUBJECT: {subject} -->\n{body_html}")
    except Exception as e:
        print(f"[scheduler] Error saving mock email: {e}")

def execute_scheduler_job(job: dict, log_path: str):
    import traceback
    job_name = job['name']
    pipeline_id = job['pipeline_id']
    entities_to_verify = json.loads(job['entities'])
    skip_if_all_pass = bool(job['skip_if_all_pass'])
    emails = job['emails']
    subject_tmpl = job['subject']
    body_header = job['body_header'] or ""
    body_ending = job['body_ending'] or ""
    
    write_scheduler_log(log_path, f"Starting job '{job_name}' for pipeline '{pipeline_id}'")
    
    try:
        client = get_gluesync_client()
        all_entities = client.list_entities(pipeline_id)
        entities_map = {e.get('entityName'): e for e in all_entities if e.get('entityName')}
        
        results = []
        for ent_name in entities_to_verify:
            ent = entities_map.get(ent_name)
            if not ent:
                results.append({
                    "entity_name": ent_name,
                    "src_library": "",
                    "src_table": "",
                    "tgt_schema": "",
                    "tgt_table": "",
                    "source_count": None,
                    "source_last_ts": None,
                    "target_count": None,
                    "target_last_ts": None,
                    "diff": None,
                    "error": "Entity not found in GlueSync pipeline config"
                })
                continue
                
            agents    = ent.get("agentEntities", [])
            src_agent = agents[0] if len(agents) > 0 else {}
            tgt_agent = agents[1] if len(agents) > 1 else {}
            src_info   = src_agent.get("table", {})
            src_library = src_info.get("schema", "")
            src_table   = src_info.get("name", "")
            tgt_info   = tgt_agent.get("table", {})
            tgt_schema = tgt_info.get("schema", "")
            tgt_table  = tgt_info.get("name", "")

            res = {
                "entity_name":    ent_name,
                "src_library":    src_library,
                "src_table":      src_table,
                "tgt_schema":     tgt_schema,
                "tgt_table":      tgt_table,
                "source_count":   None,
                "source_last_ts": None,
                "target_count":   None,
                "target_last_ts": None,
                "diff":           None,
                "error":          None,
            }
            
            if src_library and src_table:
                try:
                    src_cnt, src_ts = _count_as400(src_library, src_table)
                    res["source_count"] = src_cnt
                    res["source_last_ts"] = src_ts or "—"
                except Exception as e:
                    res["error"] = f"AS400 Error: {str(e)[:150]}"
            else:
                res["error"] = "No source table info"
                
            if tgt_schema and tgt_table:
                try:
                    tgt_cnt, tgt_ts = _count_mssql(tgt_schema, tgt_table)
                    res["target_count"] = tgt_cnt
                    res["target_last_ts"] = tgt_ts or "—"
                except Exception as e:
                    res["error"] = f"MSSQL Error: {str(e)[:150]}"
            else:
                res["error"] = "No target table info"
                
            if res["source_count"] is not None and res["target_count"] is not None:
                res["diff"] = res["source_count"] - res["target_count"]
                
            results.append(res)
            
        total_entities = len(results)
        pass_count = sum(1 for r in results if r.get('diff') == 0)
        fail_count = sum(1 for r in results if r.get('diff') is not None and r.get('diff') != 0)
        error_count = sum(1 for r in results if r.get('error') is not None and r.get('target_count') is None)
        is_all_pass = (fail_count == 0 and error_count == 0)
        
        job_mock_store = {
            "status": "done",
            "total": total_entities,
            "done_count": total_entities,
            "results": results
        }
        filename = save_verification_report(pipeline_id, job_mock_store)
        report_url = f"/reports/{pipeline_id}/{filename}"
        
        write_scheduler_log(log_path, f"Job '{job_name}': report saved to {filename} (Pass: {pass_count}/{total_entities}, Fail: {fail_count}, Error: {error_count})")
        
        if skip_if_all_pass and is_all_pass:
            write_scheduler_log(log_path, f"Job '{job_name}': skip mailing report because all entities are in sync.")
            return
            
        body_header_html = (body_header or "").replace('\n', '<br>')
        body_ending_html = (body_ending or "").replace('\n', '<br>')
        
        mail_subject = subject_tmpl.replace("{pipeline_id}", pipeline_id).replace("{job_name}", job_name)
        summary_html = f"""
        <div style="font-family: sans-serif; padding: 20px; color: #333; max-width: 600px; border: 1px solid #ddd; border-radius: 8px;">
            <p>{body_header_html}</p>
            <hr style="border: 1px solid #eee; margin: 15px 0;">
            <h3 style="color:#1e3a8a;">Verification Summary</h3>
            <ul>
                <li><strong>Pipeline:</strong> {pipeline_id}</li>
                <li><strong>Job Name:</strong> {job_name}</li>
                <li><strong>Entities Checked:</strong> {total_entities}</li>
                <li><strong>In Sync:</strong> {pass_count}</li>
                <li><strong>Discrepancies:</strong> {fail_count}</li>
                <li><strong>Errors:</strong> {error_count}</li>
            </ul>
            <p style="margin: 20px 0;">
               <a href="http://localhost:8081{report_url}" style="display:inline-block; padding: 10px 20px; background-color: #3b82f6; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">
                  Open Full Report ↗
               </a>
            </p>
            <hr style="border: 1px solid #eee; margin: 15px 0;">
            <p>{body_ending_html}</p>
        </div>
        """
        save_mock_email(emails, mail_subject, summary_html)
        write_scheduler_log(log_path, f"Job '{job_name}': Simulated email summary successfully generated for recipient(s): {emails}")
    except Exception as e:
        err_msg = f"Error executing job '{job_name}': {str(e)}\n{traceback.format_exc()}"
        print(f"[scheduler] {err_msg}", flush=True)
        write_scheduler_log(log_path, err_msg)

def run_scheduled_jobs():
    dt = datetime.now()
    log_path = "/app/replica-mon/logs/scheduler.log"
    try:
        conn = sqlite3.connect(METRICS_DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM scheduler_settings WHERE key = 'log_path'").fetchone()
        if row:
            log_path = row['value']
        jobs = conn.execute("""
            SELECT j.*, rp.name as rp_name, rp.entities, rp.skip_if_all_pass,
                   mp.name as mp_name, mp.emails, mp.subject, mp.body_header, mp.body_ending
            FROM scheduler_jobs j
            JOIN report_profiles rp ON j.report_profile_id = rp.id
            JOIN mailer_profiles mp ON j.mailer_profile_id = mp.id
            WHERE j.enabled = 1
        """).fetchall()
        conn.close()
    except Exception as e:
        print(f"[scheduler] DB error: {e}", flush=True)
        return

    for job in jobs:
        cron_expr = job['cron_expression']
        if cron_matches(cron_expr, dt):
            t = threading.Thread(target=execute_scheduler_job, args=(dict(job), log_path), daemon=True)
            t.start()

def scheduler_loop():
    import time
    print("[scheduler] Thread started", flush=True)
    while True:
        try:
            now = datetime.now()
            sleep_time = 60 - now.second - (now.microsecond / 1000000.0)
            time.sleep(sleep_time)
            run_scheduled_jobs()
        except Exception as e:
            print(f"[scheduler] Error in loop: {e}", flush=True)
            time.sleep(10)

# Start scheduler on import
t = threading.Thread(target=scheduler_loop, daemon=True)
t.start()

# ── Settings API ──
@app.get("/api/settings")
def get_settings():
    try:
        conn = get_db()
        rows = conn.execute("SELECT key, value FROM scheduler_settings").fetchall()
        return {r['key']: r['value'] for r in rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/settings")
def save_settings(req: SettingsRequest):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO scheduler_settings (key, value) VALUES ('log_path', ?)", (req.log_path,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Report Profiles API ──
@app.get("/api/profiles/report")
def list_report_profiles(pipeline_id: str = None):
    try:
        conn = get_db()
        if pipeline_id:
            rows = conn.execute("SELECT * FROM report_profiles WHERE pipeline_id = ?", (pipeline_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM report_profiles").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['entities'] = json.loads(d['entities'])
            d['skip_if_all_pass'] = bool(d['skip_if_all_pass'])
            result.append(d)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/profiles/report")
def save_report_profile(req: ReportProfileRequest):
    try:
        conn = get_db()
        if req.id is not None:
            conn.execute("""
                UPDATE report_profiles 
                SET name = ?, pipeline_id = ?, entities = ?, skip_if_all_pass = ?
                WHERE id = ?
            """, (req.name, req.pipeline_id, json.dumps(req.entities), 1 if req.skip_if_all_pass else 0, req.id))
        else:
            conn.execute("""
                INSERT INTO report_profiles (name, pipeline_id, entities, skip_if_all_pass)
                VALUES (?, ?, ?, ?)
            """, (req.name, req.pipeline_id, json.dumps(req.entities), 1 if req.skip_if_all_pass else 0))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/profiles/report/{profile_id}")
def delete_report_profile(profile_id: int):
    try:
        conn = get_db()
        conn.execute("DELETE FROM report_profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Mailer Profiles API ──
@app.get("/api/profiles/mailer")
def list_mailer_profiles():
    try:
        conn = get_db()
        rows = conn.execute("SELECT * FROM mailer_profiles").fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/profiles/mailer")
def save_mailer_profile(req: MailerProfileRequest):
    try:
        conn = get_db()
        if req.id is not None:
            conn.execute("""
                UPDATE mailer_profiles 
                SET name = ?, emails = ?, subject = ?, body_header = ?, body_ending = ?
                WHERE id = ?
            """, (req.name, req.emails, req.subject, req.body_header, req.body_ending, req.id))
        else:
            conn.execute("""
                INSERT INTO mailer_profiles (name, emails, subject, body_header, body_ending)
                VALUES (?, ?, ?, ?, ?)
            """, (req.name, req.emails, req.subject, req.body_header, req.body_ending))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/profiles/mailer/{profile_id}")
def delete_mailer_profile(profile_id: int):
    try:
        conn = get_db()
        conn.execute("DELETE FROM mailer_profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Scheduler Jobs API ──
@app.get("/api/scheduler/jobs")
def list_scheduler_jobs():
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT j.*, rp.name as report_profile_name, mp.name as mailer_profile_name
            FROM scheduler_jobs j
            LEFT JOIN report_profiles rp ON j.report_profile_id = rp.id
            LEFT JOIN mailer_profiles mp ON j.mailer_profile_id = mp.id
        """).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d['enabled'] = bool(d['enabled'])
            result.append(d)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/scheduler/jobs")
def save_scheduler_job(req: SchedulerJobRequest):
    try:
        conn = get_db()
        if req.id is not None:
            conn.execute("""
                UPDATE scheduler_jobs 
                SET name = ?, pipeline_id = ?, cron_expression = ?, report_profile_id = ?, mailer_profile_id = ?, enabled = ?
                WHERE id = ?
            """, (req.name, req.pipeline_id, req.cron_expression, req.report_profile_id, req.mailer_profile_id, 1 if req.enabled else 0, req.id))
        else:
            conn.execute("""
                INSERT INTO scheduler_jobs (name, pipeline_id, cron_expression, report_profile_id, mailer_profile_id, enabled)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (req.name, req.pipeline_id, req.cron_expression, req.report_profile_id, req.mailer_profile_id, 1 if req.enabled else 0))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/scheduler/jobs/{job_id}")
def delete_scheduler_job(job_id: int):
    try:
        conn = get_db()
        conn.execute("DELETE FROM scheduler_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/scheduler/jobs/{job_id}/toggle")
def toggle_scheduler_job(job_id: int, enabled: bool):
    try:
        conn = get_db()
        conn.execute("UPDATE scheduler_jobs SET enabled = ? WHERE id = ?", (1 if enabled else 0, job_id))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/scheduler/logs")
def get_scheduler_logs(lines: int = 50):
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM scheduler_settings WHERE key = 'log_path'").fetchone()
        log_path = row['value'] if row else "/app/replica-mon/logs/scheduler.log"
        if not os.path.exists(log_path):
            return []
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.readlines()
        return [l.strip() for l in content[-lines:]]
    except Exception as e:
         raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8081, reload=True)
