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

# Ensure replica-msdk can be imported
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from replica_msdk import GlueSyncClient, GlueSyncWebSocketClient, parse_protobuf

# Import shared database utilities and configurations
try:
    from backend.shared import (
        get_db,
        get_gluesync_client,
        store_metrics,
        store_entity_status,
        get_all_cached_statuses,
        store_agent_health,
        get_all_agent_health,
        init_comparison_tables,
        GLUESYNC_URL,
        ADMIN_PASS,
        PROMETHEUS_URL,
        METRICS_DB_PATH,
        REPORTS_DIR,
        CACHE_DIR,
    )
    from backend.verify_wss import router as verify_wss_router
except ModuleNotFoundError:
    from shared import (
        get_db,
        get_gluesync_client,
        store_metrics,
        store_entity_status,
        get_all_cached_statuses,
        store_agent_health,
        get_all_agent_health,
        init_comparison_tables,
        GLUESYNC_URL,
        ADMIN_PASS,
        PROMETHEUS_URL,
        METRICS_DB_PATH,
        REPORTS_DIR,
        CACHE_DIR,
    )
    from verify_wss import router as verify_wss_router

app = FastAPI(title="Replica-Mon Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static reports directory
os.makedirs(REPORTS_DIR, exist_ok=True)
app.mount("/reports", StaticFiles(directory=REPORTS_DIR), name="reports")

# Include verify/wss router
app.include_router(verify_wss_router)

# Initialize comparison database tables
init_comparison_tables()

# ─────────────────────────────────────────────
# Static File Serving (Dashboard HTML)
# ─────────────────────────────────────────────

_dashboard_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

@app.get("/", include_in_schema=False)
@app.get("/monitor", include_in_schema=False)
def serve_dashboard():
    html_path = os.path.join(_dashboard_dir, "monitor.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Monitor HTML not found")

@app.get("/tool", include_in_schema=False)
def serve_verify_tool():
    html_path = os.path.join(_dashboard_dir, "verify_tool.html")
    if os.path.exists(html_path):
        return FileResponse(html_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Verify Tool HTML not found")

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
        
        # Enrich entities with group names from GlueSync
        try:
            groups = client.list_groups(pipeline_id)
            group_name_map = {}
            for g in (groups if isinstance(groups, list) else []):
                gid = g.get('groupId') or g.get('id')
                gname = g.get('groupName') or g.get('name') or gid
                if gid:
                    group_name_map[gid] = gname
        except Exception:
            group_name_map = {}

        cached_statuses = get_all_cached_statuses(pipeline_id)
        agent_health_list = get_all_agent_health(pipeline_id)
        agent_health_map = {ah['agent_id']: ah for ah in agent_health_list}
        
        try:
            agents_config = client.list_agents(pipeline_id)
        except:
            agents_config = []
        src_agent_id = next((a['agentId'] for a in agents_config if a.get('agentType') == 'SOURCE'), None)
        tgt_agent_id = next((a['agentId'] for a in agents_config if a.get('agentType') == 'TARGET'), None)
        
        tgt_health = agent_health_map.get(tgt_agent_id, {}) if tgt_agent_id else {}
        src_health = agent_health_map.get(src_agent_id, {}) if src_agent_id else {}
        
        try:
            conn = get_db()
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
                
                # Inject group name from GlueSync group config
                gid = ent.get('groupId')
                ent['groupName'] = group_name_map.get(gid, gid or 'default')

                if name in cached_statuses:
                    ent['status'] = cached_statuses[name]
                elif name in active_from_metrics:
                    ent['status'] = 'RUNNING'
                else:
                    ent['status'] = 'STOPPED'

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

@app.post("/api/pipelines/{pipeline_id}/entities/{entity_id}/resync")
def resync_entity(pipeline_id: str, entity_id: str, snapshot_write_method: str = "UPSERT"):
    try:
        client = get_gluesync_client()
        success = client.resync_entity(pipeline_id, entity_id, snapshot_write_method=snapshot_write_method)
        if success:
            return {"status": "success", "message": f"Entity {entity_id} resynced"}
        raise HTTPException(status_code=500, detail="Failed to trigger resync")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─────────────────────────────────────────────
# Metrics REST Endpoints (SQLite I/U/D store)
# ─────────────────────────────────────────────

@app.get("/api/metrics/{pipeline_id}/{entity_name}")
def get_entity_metrics(
    pipeline_id: str,
    entity_name: str,
    window_hours: int = Query(default=8, description="Time window in hours")
):
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
# Comparison Engine — REST Endpoints
# ─────────────────────────────────────────────

@app.get("/api/verify/{pipeline_id}/comparison")
def get_comparison(pipeline_id: str):
    """Return latest comparison results per entity."""
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT c.* FROM comparison_cache c
            INNER JOIN (
                SELECT entity_name, MAX(id) as max_id
                FROM comparison_cache
                WHERE pipeline_id = ?
                GROUP BY entity_name
            ) latest ON c.id = latest.max_id
            ORDER BY ABS(c.gap) DESC, c.status ASC
        """, (pipeline_id,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/verify/{pipeline_id}/comparison/history")
def get_comparison_history(
    pipeline_id: str,
    entity_name: str = Query(None),
    hours: int = Query(default=24)
):
    """Return comparison history for trending/gap analysis."""
    try:
        conn = get_db()
        query = """
            SELECT entity_name, gap, status, cycle_start, cycle_end, captured_at
            FROM comparison_cache
            WHERE pipeline_id = ? AND captured_at >= datetime('now', 'localtime', ?)
        """
        params = [pipeline_id, f'-{hours} hours']
        if entity_name:
            query += " AND entity_name = ?"
            params.append(entity_name)
        query += " ORDER BY captured_at ASC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
            raw_entity = inner.get("Field_1_string", "")
            
            meta = metadata_cache.get(raw_entity)
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
            
            timestamp = inner.get("Field_2_string", "")
            if timestamp:
                try:
                    ts_obj = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    inner['_enriched']['timestamp_human'] = ts_obj.strftime('%Y-%m-%d %H:%M:%S %Z')
                except:
                    try:
                        ts_obj = datetime.fromtimestamp(int(timestamp) / 1000, tz=timezone.utc)
                        inner['_enriched']['timestamp_human'] = ts_obj.strftime('%Y-%m-%d %H:%M:%S UTC')
                    except:
                        inner['_enriched']['timestamp_human'] = timestamp
    
    elif msg_type == "EntityStatusMessage":
        inner = (data
                 .get("Field_2_message", {})
                 .get("Field_1_message", {}))
        
        # Check list format as well
        raw_items = inner.get("Field_1_message_list", None)
        if raw_items is None:
            single = inner.get("Field_1_message")
            raw_items = [single] if single else []

        for item in raw_items:
            if not item:
                continue
            raw_entity = item.get("Field_2_string", "")
            status_val = item.get("Field_4_varint", 0)
            
            meta = metadata_cache.get(raw_entity)
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
            
            item['_enriched'] = {
                'entity_display': f'{entity_id} "{entity_name}" entity',
                'entity_name': entity_name,
                'entity_id': entity_id,
                'status_code': status_val,
                'status_text': status_str,
                'status_description': f'Entity is currently {status_str.lower()}'
            }
            
    return data

_entity_pipeline_map: dict = {}

@app.websocket("/ws/metrics")
async def websocket_endpoint(websocket: WebSocket, pipeline_id: str, entities: str):
    await websocket.accept()

    if not ADMIN_PASS:
        await websocket.close(code=1008, reason="No GlueSync credentials configured")
        return

    entity_list = [e.strip() for e in entities.split(",") if e.strip()]
    if not entity_list or not pipeline_id:
        await websocket.close(code=1008, reason="Missing pipeline_id or entities")
        return

    for name in entity_list:
        _entity_pipeline_map[name] = pipeline_id

    metadata_cache = {}
    try:
        rest_client = get_gluesync_client()
        pipelines = rest_client.list_pipelines()
        entities_meta = rest_client.list_entities(pipeline_id)
        
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
        # Subscription moved to after WS connection is established


        loop = asyncio.get_event_loop()

        def on_message(data):
            print(f"[ws] on_message received raw data type: {type(data)}", flush=True)
            try:
                normalized_messages = []
                
                # Check if this is the JSON telemetry format from GlueSync
                if isinstance(data, dict) and "type" in data and "content" in data:
                    msg_type = data.get("type", "")
                    content = data.get("content", {})
                    
                    if msg_type == "EntityStatusMessage":
                        pb_items = []
                        for es in content.get("entitiesStatus", []):
                            pb_items.append({
                                "Field_1_string": es.get("pipelineId", ""),
                                "Field_2_string": es.get("entityId", ""),
                                "Field_3_varint": 1 if es.get("isMigrationActive") else 0,
                                "Field_4_varint": 1 if es.get("isSyncActive") else 0,
                                "Field_5_varint": 1 if es.get("isBusy") else 0,
                            })
                        if pb_items:
                            normalized_messages.append({
                                "Field_1_string": "EntityStatusMessage",
                                "Field_2_message": {
                                    "Field_1_message": {
                                        "Field_1_message_list": pb_items,
                                        "Field_1_message": pb_items[0]
                                    }
                                }
                            })
                            
                    elif msg_type == "MetricsMessage":
                        pipelines_metrics = content.get("pipelinesMetrics", {})
                        for pid, pipe_data in pipelines_metrics.items():
                            entities_metrics = pipe_data.get("entitiesMetrics", {})
                            for eid, metric_list in entities_metrics.items():
                                if not metric_list:
                                    continue
                                m = metric_list[0]
                                inserts = m.get("inserts", 0)
                                updates = m.get("updates", 0)
                                deletes = m.get("deletes", 0)
                                total_ops = m.get("totalOps", 0)
                                timestamp_val = pipe_data.get("timestamp", 0)
                                
                                normalized_messages.append({
                                    "Field_1_string": "MetricsMessage",
                                    "Field_2_message": {
                                        "Field_1_message": {
                                            "Field_1_message": {
                                                "Field_2_message": {
                                                    "Field_1_string": eid,
                                                    "Field_2_string": str(timestamp_val),
                                                    "Field_4_varint": inserts,
                                                    "Field_5_varint": updates,
                                                    "Field_6_varint": deletes,
                                                    "Field_7_varint": total_ops
                                                }
                                            }
                                        }
                                    }
                                })
                                
                    elif msg_type == "PipelineStatusMessage":
                        for ps in content.get("pipelinesStatus", []):
                            pid = ps.get("pipelineId")
                            for agent in ps.get("agentsInformation", []):
                                agent_id = agent.get("agentId", "")
                                if agent_id:
                                    normalized_messages.append({
                                        "Field_1_string": "AgentHealthMessage",
                                        "Field_2_message": {
                                            "Field_1_message": {
                                                "Field_1_message": {
                                                    "Field_2_message": {
                                                        "Field_1_string": agent_id,
                                                        "Field_2_string": agent.get("agentNickname", ""),
                                                        "Field_3_string": agent.get("connectionStatus", "UNKNOWN"),
                                                        "Field_4_string": agent.get("status", "UNKNOWN"),
                                                        "Field_5_string": agent.get("connectedDatabaseHost", ""),
                                                        "Field_6_string": agent.get("connectedDatabaseName", ""),
                                                        "Field_7_string": agent.get("connectionError", "")
                                                    }
                                                }
                                            }
                                        }
                                    })
                else:
                    parsed_data = None
                    if isinstance(data, bytes):
                        parsed_data = parse_protobuf(data)
                    elif isinstance(data, dict):
                        parsed_data = data
                    if parsed_data:
                        normalized_messages.append(parsed_data)
                
                # Process each normalized message
                for parsed_data in normalized_messages:
                    msg_type = parsed_data.get("Field_1_string", "")
                    print(f"[ws] on_message parsed message type: {msg_type}", flush=True)

                    if msg_type == "MetricsMessage":
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
                                store_entity_status(pid, entity_name, "RUNNING")

                    elif msg_type == "EntityStatusMessage":
                        inner = (parsed_data
                                 .get("Field_2_message", {})
                                 .get("Field_1_message", {}))
                        
                        raw_items = inner.get("Field_1_message_list", None)
                        if raw_items is None:
                            single = inner.get("Field_1_message")
                            raw_items = [single] if single else []

                        for estatus in raw_items:
                            if not estatus:
                                continue
                            pid_raw = estatus.get("Field_1_string", "")
                            ent_id_raw = estatus.get("Field_2_string", "")
                            is_migration = bool(estatus.get("Field_3_varint", 0))
                            is_sync = bool(estatus.get("Field_4_varint", 0))
                            is_busy = bool(estatus.get("Field_5_varint", 0))

                            status_str = "STOPPED"
                            if is_migration:
                                status_str = "MIGRATING"
                            elif is_sync:
                                status_str = "RUNNING"
                            elif is_busy:
                                status_str = "PAUSED"

                            meta = metadata_cache.get(ent_id_raw)
                            if not meta:
                                for entity_name, entity_meta in metadata_cache.items():
                                    if entity_meta.get('id') == ent_id_raw:
                                        meta = entity_meta
                                        break
                            entity_name = meta.get('name', ent_id_raw) if meta else ent_id_raw
                            pid = pid_raw or pipeline_id
                            
                            if entity_name:
                                store_entity_status(pid, entity_name, status_str)
                                print(f"[ws] EntityStatus: {entity_name} -> {status_str} (sync={is_sync})", flush=True)

                    elif msg_type == "AgentHealthMessage":
                        inner = (parsed_data
                                 .get("Field_2_message", {})
                                 .get("Field_1_message", {})
                                 .get("Field_1_message", {}))
                        health_data = inner.get("Field_2_message", {}) if inner else {}
                        
                        agent_id = health_data.get("Field_1_string", "")
                        agent_type = health_data.get("Field_2_string", "")
                        connection_status = health_data.get("Field_3_string", "UNKNOWN")
                        health_status = health_data.get("Field_4_string", "UNKNOWN")
                        connected_db_host = health_data.get("Field_5_string")
                        connected_db_name = health_data.get("Field_6_string")
                        connection_error = health_data.get("Field_7_string")
                        
                        if agent_id:
                            store_agent_health(
                                pipeline_id,
                                agent_id,
                                agent_type,
                                connection_status,
                                health_status,
                                connected_db_host,
                                connected_db_name,
                                connection_error
                            )

                    # Forward JSON representation to the browser client
                    enriched_data = enrich_with_metadata(parsed_data, metadata_cache) if parsed_data else parsed_data
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_text(json.dumps(enriched_data)),
                        loop
                    )
            except Exception as e:
                print(f"[ws] Error processing message callback: {e}")

        ws_client.on_message = on_message
        # Start connection in background thread first
        def run_ws():
            try:
                ws_client.connect(on_message)
            except Exception as ex:
                print(f"[ws] ws_client.connect failed: {ex}")
                import traceback
                traceback.print_exc()

        future = loop.run_in_executor(None, run_ws)
        # Wait for WebSocket handshake to complete
        await asyncio.sleep(1.0)
        # Subscribe to metrics to trigger initial status stream
        ws_client.subscribe(pipeline_id, entity_list)
        
        # Keep endpoint alive while the background WS connection is running
        await future

    except WebSocketDisconnect:
        print("[ws] Browser disconnected")
    except Exception as e:
        print(f"[ws] WebSocket error: {e}")
    finally:
        try:
            ws_client.disconnect()
        except:
            pass

# ─────────────────────────────────────────────
# Background Comparison Engine
# ─────────────────────────────────────────────

class BackgroundComparisonEngine:
    """Periodically compares AS400 journal vs MSSQL CT changes per entity."""

    def __init__(self, cache_dir: str = None, interval: int = 60):
        self.cache_dir = cache_dir or CACHE_DIR
        self.interval = interval
        self._running = False
        self._task = None

    async def _query_journal_new(self, library: str, table_name: str, last_seq: int) -> dict:
        """Count new journal entries since last_seq."""
        jdb = os.path.join(self.cache_dir, "journal_cache.db")
        if not os.path.exists(jdb):
            return {'total': 0, 'inserts': 0, 'updates': 0, 'deletes': 0, 'max_seq': 0, 'error': 'no_cache'}
        try:
            conn = sqlite3.connect(jdb)
            conn.row_factory = sqlite3.Row
            full_name = f"{library}.{table_name}"
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN entry_type IN ('IR','PT') THEN 1 ELSE 0 END) as inserts,
                       SUM(CASE WHEN entry_type IN ('UP','UB') THEN 1 ELSE 0 END) as updates,
                       SUM(CASE WHEN entry_type = 'DL' THEN 1 ELSE 0 END) as deletes,
                       COALESCE(MAX(entry_number), 0) as max_seq
                FROM journal_entries
                WHERE table_name = ? AND entry_number > ?
            """, (full_name, last_seq)).fetchone()
            result = dict(row) if row else {'total': 0, 'inserts': 0, 'updates': 0, 'deletes': 0, 'max_seq': 0}
            conn.close()
            return result
        except Exception as e:
            print(f"[comparison] Error querying journal cache: {e}", flush=True)
            return {'total': 0, 'inserts': 0, 'updates': 0, 'deletes': 0, 'max_seq': last_seq, 'error': str(e)}

    async def _query_ct_new(self, table_name: str, last_version: int) -> dict:
        """Count new CT changes since last_version."""
        cdb = os.path.join(self.cache_dir, "ct_cache.db")
        if not os.path.exists(cdb):
            return {'total': 0, 'inserts': 0, 'updates': 0, 'deletes': 0, 'max_version': 0, 'error': 'no_cache'}
        try:
            conn = sqlite3.connect(cdb)
            conn.row_factory = sqlite3.Row
            row = conn.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN sys_change_operation = 'I' THEN 1 ELSE 0 END) as inserts,
                       SUM(CASE WHEN sys_change_operation = 'U' THEN 1 ELSE 0 END) as updates,
                       SUM(CASE WHEN sys_change_operation = 'D' THEN 1 ELSE 0 END) as deletes,
                       COALESCE(MAX(sys_change_version), 0) as max_version
                FROM ct_changes
                WHERE table_name = ? AND sys_change_version > ?
            """, (table_name, last_version)).fetchone()
            result = dict(row) if row else {'total': 0, 'inserts': 0, 'updates': 0, 'deletes': 0, 'max_version': 0}
            conn.close()
            return result
        except Exception as e:
            print(f"[comparison] Error querying CT cache: {e}", flush=True)
            return {'total': 0, 'inserts': 0, 'updates': 0, 'deletes': 0, 'max_version': last_version, 'error': str(e)}

    async def run_cycle(self):
        """Run one comparison cycle for all active pipelines."""
        try:
            client = get_gluesync_client()
            pipelines = client.list_pipelines()
        except Exception as e:
            print(f"[comparison] Cannot get pipelines: {e}", flush=True)
            return

        for pipeline in (pipelines if isinstance(pipelines, list) else []):
            pid = pipeline.get('pipelineId') or pipeline.get('id', '')
            if not pid:
                continue

            try:
                entities = client.list_entities(pid)
            except Exception as e:
                print(f"[comparison] Cannot get entities for {pid}: {e}", flush=True)
                continue

            conn = get_db()
            cycle_start = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            for ent in (entities if isinstance(entities, list) else []):
                if not isinstance(ent, dict):
                    continue
                entity_name = ent.get('entityName', '')
                if not entity_name:
                    continue

                # Cache entity status from active REST API polling if present
                if 'status' in ent and ent['status'] is not None:
                    store_entity_status(pid, entity_name, ent['status'].upper())

                # Extract source and target table info from agent entities
                src_agent = None
                tgt_agent = None
                for ae in ent.get('agentEntities', []):
                    etype = ae.get('entityType', {}).get('type', '')
                    if etype == 'Source':
                        src_agent = ae
                    elif etype == 'Target':
                        tgt_agent = ae

                if not src_agent:
                    continue

                src_table = src_agent.get('table', {})
                library = src_table.get('schema', '')
                src_tbl = src_table.get('name', '')
                if not library or not src_tbl:
                    continue

                tgt_table = tgt_agent.get('table', {}) if tgt_agent else {}
                tgt_schema = tgt_table.get('schema', '')
                tgt_tbl = tgt_table.get('name', '')
                full_target = f"{tgt_schema}.{tgt_tbl}" if tgt_schema and tgt_tbl else ''

                # Get current checkpoint
                cp = conn.execute(
                    """SELECT last_journal_seq, last_ct_version FROM comparison_checkpoints
                       WHERE pipeline_id = ? AND entity_name = ?""",
                    (pid, entity_name)
                ).fetchone()
                last_seq = cp['last_journal_seq'] if cp else 0
                last_ct_ver = cp['last_ct_version'] if cp else 0

                # Query cache DBs
                journal = await self._query_journal_new(library, src_tbl, last_seq)
                ct = await self._query_ct_new(full_target or f"dbo.{src_tbl}", last_ct_ver)

                jsource = journal.get('total', 0)
                ctsource = ct.get('total', 0)
                gap = jsource - ctsource

                # Determine trend from previous cycle results
                prev = conn.execute(
                    """SELECT gap, consecutive_cycles_gap_positive FROM comparison_cache
                       WHERE pipeline_id = ? AND entity_name = ?
                       ORDER BY id DESC LIMIT 1""",
                    (pid, entity_name)
                ).fetchone()

                prev_gap = prev['gap'] if prev else 0
                prev_consecutive = prev['consecutive_cycles_gap_positive'] if prev else 0

                if gap > 0 and prev_gap > 0:
                    consecutive = prev_consecutive + 1
                    if gap > prev_gap:
                        trend = 'growing'
                    elif gap < prev_gap:
                        trend = 'shrinking'
                    else:
                        trend = 'stable'
                else:
                    consecutive = 1 if gap > 0 else 0
                    trend = 'stable'

                # Determine status
                if journal.get('error') == 'no_cache':
                    status = 'no_source_cache'
                elif ct.get('error') == 'no_cache':
                    status = 'no_target_cache'
                elif gap == 0 and consecutive == 0:
                    status = 'in_sync'
                elif gap > 0 and consecutive >= 3:
                    status = 'alert'
                elif gap > 0:
                    status = 'replicating'
                else:
                    status = 'in_sync'

                try:
                    conn.execute("""
                        INSERT INTO comparison_cache
                            (pipeline_id, entity_name, library, source_table, target_table,
                             source_changes, target_changes,
                             source_inserts, source_updates, source_deletes,
                             target_inserts, target_updates, target_deletes,
                             gap, gap_trend, consecutive_cycles_gap_positive, status,
                             cycle_start, cycle_end)
                        VALUES (?, ?, ?, ?, ?,
                                ?, ?,
                                ?, ?, ?,
                                ?, ?, ?,
                                ?, ?, ?, ?,
                                ?, ?)
                    """, (
                        pid, entity_name, library, f"{library}.{src_tbl}", full_target,
                        jsource, ctsource,
                        journal.get('inserts', 0), journal.get('updates', 0), journal.get('deletes', 0),
                        ct.get('inserts', 0), ct.get('updates', 0), ct.get('deletes', 0),
                        gap, trend, consecutive, status,
                        cycle_start, datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    ))

                    # Update checkpoint
                    max_seq = journal.get('max_seq', last_seq)
                    max_ver = ct.get('max_version', last_ct_ver)
                    conn.execute("""
                        INSERT INTO comparison_checkpoints
                            (pipeline_id, entity_name, source_table, target_table,
                             last_journal_seq, last_ct_version, last_verified_at)
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'))
                        ON CONFLICT(pipeline_id, entity_name) DO UPDATE SET
                            source_table=excluded.source_table,
                            target_table=excluded.target_table,
                            last_journal_seq=excluded.last_journal_seq,
                            last_ct_version=excluded.last_ct_version,
                            last_verified_at=excluded.last_verified_at
                    """, (
                        pid, entity_name, f"{library}.{src_tbl}", full_target,
                        max_seq, max_ver
                    ))
                    conn.commit()
                except Exception as e:
                    print(f"[comparison] Error storing result for {entity_name}: {e}", flush=True)

    async def start(self):
        """Start the comparison loop."""
        self._running = True
        print(f"[comparison] Engine started, interval={self.interval}s, cache_dir={self.cache_dir}", flush=True)
        while self._running:
            await self.run_cycle()
            await asyncio.sleep(self.interval)

    def stop(self):
        self._running = False


_background_engine: BackgroundComparisonEngine = None


@app.on_event("startup")
async def start_background_comparison():
    """Start the background comparison engine on app startup."""
    global _background_engine
    interval = int(os.getenv("COMPARISON_INTERVAL", "120"))
    engine = BackgroundComparisonEngine(cache_dir=CACHE_DIR, interval=interval)
    _background_engine = engine
    asyncio.create_task(engine.start())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8081, reload=True)
