import os
import sys
import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
import urllib.request
import urllib.parse

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
        CREATE INDEX IF NOT EXISTS idx_entity_time
        ON ws_metrics(entity_name, captured_at)
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

# Initialize DB on startup
init_metrics_db()

# ─────────────────────────────────────────────
# GlueSync Client Helper
# ─────────────────────────────────────────────

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
        return client.list_entities(pipeline_id)
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
    try:
        from qadmcli.config import load_config
        from qadmcli.db.connection import AS400ConnectionManager
        from pathlib import Path

        config_path = _get_qadmcli_config_path()
        if not config_path:
            raise FileNotFoundError("qadmcli connection.yaml not found")

        config = load_config(Path(config_path))

        # Override credentials from env if set
        as400_user = os.getenv("AS400_USER")
        as400_pass = os.getenv("AS400_PASSWORD")
        if as400_user or as400_pass:
            config.as400 = config.as400.copy_with_overrides(
                user=as400_user or config.as400.user,
                password=as400_pass or config.as400.password,
            )

        print(f"[verify] AS400 Connecting: {config.as400.host} {library}.{table}", flush=True)
        with AS400ConnectionManager(config) as conn:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {library}.{table}")
            row = cursor.fetchone()
            count = int(row[0]) if row else 0
            cursor.close()

        print(f"[verify]   AS400 {library}.{table} count={count}", flush=True)
        return count, None

    except Exception as e:
        raise RuntimeError(str(e)) from e


# ── Async Job Store (in-memory) ──────────────────────────────────────────────
# Stores verify job results keyed by pipeline_id
_verify_jobs: dict = {}   # pipeline_id → { "status": "running"|"done", "results": [...], "started_at": ... }
_verify_lock = threading.Lock()



def _verify_worker(pipeline_id: str, entities: list):
    """Background thread: counts each entity one by one, updates job state incrementally."""
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
            "source_count":   None,
            "source_last_ts": None,
            "target_count":   None,
            "target_last_ts": None,
            "diff":           None,
            "error":          None,
        }

        print(f"[verify] [{pipeline_id}] {result['entity_name']}  src={src_library}.{src_table}  tgt={tgt_schema}.{tgt_table}", flush=True)

        # ── Source COUNT (AS400) ──
        if src_library and src_table:
            try:
                src_cnt, src_ts = _count_as400(src_library, src_table)
                result["source_count"]   = src_cnt
                result["source_last_ts"] = src_ts or "—"
            except Exception as e:
                result["source_last_ts"] = f"Error: {str(e)[:100]}"
                print(f"[verify]   AS400 ✗ {e}", flush=True)
        else:
            result["source_last_ts"] = "No source table info"

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



@app.post("/api/verify/{pipeline_id}/run")
def start_verify(pipeline_id: str):
    """
    Start async verification job. Returns immediately with job metadata.
    Poll GET /api/verify/{pipeline_id}/results for incremental results.
    """
    print(f"[verify] start_verify pipeline={pipeline_id}", flush=True)
    try:
        client   = get_gluesync_client()
        entities = client.list_entities(pipeline_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list entities: {e}")

    if not entities:
        raise HTTPException(status_code=404, detail="No entities found for this pipeline")

    # Kick off background worker
    t = threading.Thread(target=_verify_worker, args=(pipeline_id, entities), daemon=True)
    t.start()

    return {
        "status": "started",
        "pipeline_id": pipeline_id,
        "total": len(entities),
        "poll_url": f"/api/verify/{pipeline_id}/results",
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
                            entity_name = inner.get("Field_1_string", "")
                            inserts   = inner.get("Field_4_varint", 0) or 0
                            updates   = inner.get("Field_5_varint", 0) or 0
                            deletes   = inner.get("Field_6_varint", 0) or 0
                            total_ops = inner.get("Field_7_varint", 0) or 0

                            pid = _entity_pipeline_map.get(entity_name, pipeline_id)
                            if entity_name and total_ops > 0:
                                store_metrics(pid, entity_name, inserts, updates, deletes, total_ops)

                    if msg_type in ["EntityStatusMessage", "MetricsMessage"]:
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_json(parsed_data), loop
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
