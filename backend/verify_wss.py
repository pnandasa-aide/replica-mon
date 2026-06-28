import os
import sys
import json
import sqlite3
import threading
import logging
import httpx
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel

try:
    from backend.shared import (
        get_db,
        get_gluesync_client,
        get_gluesync_token,
        _count_as400,
        _count_mssql,
        _count_oracle,
        REPORTS_DIR,
        METRICS_DB_PATH,
        GLUESYNC_URL,
        configure_app_logging,
        APP_EXTERNAL_URL,
    )
except ModuleNotFoundError:
    from shared import (
        get_db,
        get_gluesync_client,
        get_gluesync_token,
        _count_as400,
        _count_mssql,
        _count_oracle,
        REPORTS_DIR,
        METRICS_DB_PATH,
        GLUESYNC_URL,
        configure_app_logging,
        APP_EXTERNAL_URL,
    )

logger = logging.getLogger("replica_mon.verify_wss")

router = APIRouter()

# ─────────────────────────────────────────────
# Verify Tool Models & In-Memory Job Store
# ─────────────────────────────────────────────

class VerifyRunRequest(BaseModel):
    entities: Optional[List[str]] = None

class ApiProxyRequest(BaseModel):
    target_url: str
    method: str = "GET"
    headers: Optional[Dict[str, str]] = None
    body: Optional[Any] = None

class SettingsRequest(BaseModel):
    log_path: str
    app_log_path: Optional[str] = None
    app_log_level: Optional[str] = None
    app_log_max_size: Optional[int] = None

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

_verify_jobs: dict = {}   # pipeline_id → { "status": "running"|"done", "results": [...], "started_at": ... }
_verify_lock = threading.Lock()

# ─────────────────────────────────────────────
# Verification Background Worker
# ─────────────────────────────────────────────

def _get_agent_tag_map(pipeline_id: str) -> dict:
    try:
        client = get_gluesync_client()
        agents = client.list_agents(pipeline_id)
        return {a["agentId"]: a.get("agentTag", "").lower() for a in agents}
    except Exception as e:
        logger.warning(f"[verify] Failed to build agent tag map: {e}")
        return {}

def _get_database_count(tag: str, schema: str, table: str, pipeline_id: str, agent_id: str) -> tuple:
    tag = (tag or "").lower()
    if "oracle" in tag:
        return _count_oracle(schema, table, pipeline_id, agent_id)
    elif "iseries" in tag or "as400" in tag:
        return _count_as400(schema, table)
    elif "mssql" in tag or "sqlserver" in tag:
        return _count_mssql(schema, table)
    else:
        # Fallback defaults
        if tag == "source":
            return _count_as400(schema, table)
        else:
            return _count_mssql(schema, table)

def _verify_worker(pipeline_id: str, entities: list):
    """Background thread: counts each entity one by one, updates job state incrementally."""
    import sys
    sys.stderr.write(f"[verify] [{pipeline_id}] ===== Worker STARTED, {len(entities)} entities =====\n")
    sys.stderr.flush()
    print(f"[verify] [{pipeline_id}] ===== Worker STARTED, {len(entities)} entities =====", flush=True)
    logger.info(f"[verify] [{pipeline_id}] Worker STARTED, {len(entities)} entities")
    
    agent_tag_map = _get_agent_tag_map(pipeline_id)

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
        src_agent_id = src_agent.get("agentId")
        src_tag = agent_tag_map.get(src_agent_id, "source")

        tgt_info   = tgt_agent.get("table", {})
        tgt_schema = tgt_info.get("schema", "")
        tgt_table  = tgt_info.get("name", "")
        tgt_agent_id = tgt_agent.get("agentId")
        tgt_tag = agent_tag_map.get(tgt_agent_id, "target")

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

        # ── Source COUNT ──
        if src_library and src_table:
            try:
                logger.info(f"[verify]   Source ({src_tag}) → Counting {src_library}.{src_table}...")
                src_cnt, src_ts = _get_database_count(src_tag, src_library, src_table, pipeline_id, src_agent_id)
                
                # Use current local check timestamp instead of database last modified timestamp
                from zoneinfo import ZoneInfo
                try:
                    local_tz = ZoneInfo("Asia/Bangkok")
                except:
                    local_tz = None
                now_local = datetime.now(local_tz) if local_tz else datetime.now()
                src_check_time = now_local.strftime("%Y-%m-%d %H:%M:%S")
                
                result["source_count"]   = src_cnt
                result["source_last_ts"] = src_check_time
                logger.info(f"[verify]   Source ({src_tag}) ✓ Count={src_cnt}")
            except Exception as e:
                import traceback
                error_msg = str(e)
                error_detail = f"Error: {error_msg[:200]}"
                result["source_last_ts"] = error_detail
                result["error"] = error_detail
                logger.error(f"[verify]   Source ({src_tag}) ✗ {error_msg}")
                logger.error(f"[verify]   Source ({src_tag}) Traceback: {traceback.format_exc()}")
        else:
            result["source_last_ts"] = "No source table info"
            logger.warning(f"[verify]   Source ({src_tag}) ⚠ No source table info (library={src_library}, table={src_table})")

        # ── Target COUNT ──
        if tgt_schema and tgt_table:
            try:
                logger.info(f"[verify]   Target ({tgt_tag}) → Counting {tgt_schema}.{tgt_table}...")
                cnt, ts = _get_database_count(tgt_tag, tgt_schema, tgt_table, pipeline_id, tgt_agent_id)
                
                # Use current local check timestamp instead of database last modified timestamp
                from zoneinfo import ZoneInfo
                try:
                    local_tz = ZoneInfo("Asia/Bangkok")
                except:
                    local_tz = None
                now_local = datetime.now(local_tz) if local_tz else datetime.now()
                tgt_check_time = now_local.strftime("%Y-%m-%d %H:%M:%S")
                
                result["target_count"]   = cnt
                result["target_last_ts"] = tgt_check_time
                logger.info(f"[verify]   Target ({tgt_tag}) ✓ Count={cnt}")
            except Exception as e:
                err = str(e)
                result["error"] = err[:200]
                logger.error(f"[verify]   Target ({tgt_tag}) ✗ {err}")
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

# ─────────────────────────────────────────────
# REST Endpoints
# ─────────────────────────────────────────────

@router.post("/api/verify/{pipeline_id}/run")
def start_verify(pipeline_id: str, req: Optional[VerifyRunRequest] = None):
    import sys
    sys.stderr.write(f"[verify] ===== start_verify CALLED pipeline={pipeline_id} =====\n")
    sys.stderr.flush()
    print(f"[verify] ===== start_verify CALLED pipeline={pipeline_id} =====", flush=True)
    logger.info(f"[verify] start_verify pipeline={pipeline_id}")
    
    try:
        client   = get_gluesync_client()
        entities = client.list_entities(pipeline_id)
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

@router.get("/api/verify/debug")
def verify_debug():
    import sys
    return {
        "message": "Debug endpoint working",
        "python_version": sys.version,
        "stderr_writable": sys.stderr.writable(),
        "stdout_writable": sys.stdout.writable(),
    }

@router.get("/api/verify/{pipeline_id}/results")
def get_verify_results(pipeline_id: str):
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
# Reports Logic
# ─────────────────────────────────────────────

def cleanup_old_reports(retention_days: int = 30):
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
    from zoneinfo import ZoneInfo
    try:
        local_tz = ZoneInfo("Asia/Bangkok")
    except:
        local_tz = None
    
    now_local = datetime.now(local_tz) if local_tz else datetime.now()
    timestamp = now_local.strftime("%Y%m%d_%H%M%S")
    display_time = now_local.strftime("%Y-%m-%d %H:%M:%S") + (f" {now_local.tzname()}" if now_local.tzname() else "")
    
    try:
        client = get_gluesync_client()
        pipeline = client.get_pipeline(pipeline_id)
        pipeline_name = pipeline.get('name', pipeline_id) if pipeline else pipeline_id
    except Exception as e:
        print(f"[report] Warning: Could not fetch pipeline name: {e}")
        pipeline_name = pipeline_id
    
    pipeline_dir = os.path.join(REPORTS_DIR, pipeline_id)
    os.makedirs(pipeline_dir, exist_ok=True)

    results = job.get("results", [])
    total_entities = len(results)
    pass_count = sum(1 for r in results if r.get('diff') == 0)
    fail_count = sum(1 for r in results if r.get('diff') is not None and r.get('diff') != 0)
    error_count = sum(1 for r in results if r.get('error') and r.get('target_count') is None)
    summary_status = "pass" if (total_entities > 0 and fail_count == 0 and error_count == 0) else "fail"

    filename = f"report_{pipeline_id}_{timestamp}_{summary_status}.html"
    filepath = os.path.join(pipeline_dir, filename)
    
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

@router.post("/api/verify/{pipeline_id}/save")
def save_verify_job_report(pipeline_id: str):
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

@router.get("/api/reports")
def list_reports(
    pipeline_id: str = None,
    search: str = None,
    status: str = None,
    from_date: str = None,
    to_date: str = None
):
    import re
    reports = []
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
            pipeline_name = pipeline_names.get(subdir, subdir)
            for f in os.listdir(subdir_path):
                if f.endswith('.html') and f.startswith('report_'):
                    filepath = os.path.join(subdir_path, f)
                    stat = os.stat(filepath)
                    base = f.replace('.html', '')
                    parts = base.split('_')
                    timestamp_str = "Unknown"
                    summary_status = None
                    entity_count = 0
                    
                    if parts[-1] in ('pass', 'fail'):
                        summary_status = parts[-1]
                        date_part = parts[-3] if len(parts) >= 3 else ''
                        time_part = parts[-2] if len(parts) >= 2 else ''
                    else:
                        date_part = parts[-2] if len(parts) >= 2 else ''
                        time_part = parts[-1]
                    if len(date_part) == 8 and len(time_part) == 6:
                        timestamp_str = f"{date_part[0:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[0:2]}:{time_part[2:4]}:{time_part[4:6]}"
                    
                    if status and summary_status != status:
                        continue
                    if from_date or to_date:
                        try:
                            file_dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                            if from_date:
                                fd = datetime.fromisoformat(from_date.replace('Z', '+00:00')).replace(tzinfo=None)
                                if file_dt < fd:
                                    continue
                            if to_date:
                                td = datetime.fromisoformat(to_date.replace('Z', '+00:00')).replace(tzinfo=None)
                                if file_dt > td:
                                    continue
                        except Exception as ex:
                            print(f"[reports] Date filter parsing error: {ex}")
                    
                    # Extract entity count and entity names from HTML content
                    entities = []
                    entities_data = []
                    try:
                        with open(filepath, 'r', encoding='utf-8') as rf:
                            content = rf.read()
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
                        
                    # Apply search filter
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
                        "created_at": timestamp_str,
                        "summary_status": summary_status or "unknown",
                        "entity_count": entity_count,
                        "entities": entities,
                        "size_bytes": stat.st_size,
                        "url": f"/reports/{subdir}/{f}"
                    })
        return sorted(reports, key=lambda x: x["created_at"], reverse=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class BulkDeleteRequest(BaseModel):
    reports: List[dict]  # list of {"pipeline_id": str, "filename": str}

@router.delete("/api/reports/{pipeline_id}/{filename}")
def delete_report(pipeline_id: str, filename: str):
    if ".." in pipeline_id or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid path")
    filepath = os.path.join(REPORTS_DIR, pipeline_id, filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        os.remove(filepath)
        # Remove subdir if it's empty
        subdir_path = os.path.join(REPORTS_DIR, pipeline_id)
        if os.path.exists(subdir_path) and not os.listdir(subdir_path):
            os.rmdir(subdir_path)
        return {"status": "success", "message": "Report deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/reports/delete-bulk")
def delete_reports_bulk(req: BulkDeleteRequest):
    deleted_count = 0
    errors = []
    for r in req.reports:
        pipeline_id = r.get("pipeline_id")
        filename = r.get("filename")
        if not pipeline_id or not filename:
            continue
        if ".." in pipeline_id or ".." in filename or "/" in filename or "\\" in filename:
            errors.append(f"Invalid path: {pipeline_id}/{filename}")
            continue
        filepath = os.path.join(REPORTS_DIR, pipeline_id, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
                deleted_count += 1
                # Clean up empty subdirectory
                subdir_path = os.path.join(REPORTS_DIR, pipeline_id)
                if os.path.exists(subdir_path) and not os.listdir(subdir_path):
                    os.rmdir(subdir_path)
            except Exception as e:
                errors.append(f"Failed to delete {filename}: {str(e)}")
    return {
        "status": "success",
        "deleted_count": deleted_count,
        "errors": errors
    }

# ─────────────────────────────────────────────
# Proxy Request (used by wss API Explorer)
# ─────────────────────────────────────────────

@router.post("/api/proxy")
def api_proxy(req: ApiProxyRequest):
    import urllib.request
    import urllib.parse
    target_url = req.target_url
    if not target_url.startswith("http://") and not target_url.startswith("https://"):
        target_url = target_url.lstrip('/')
        target_url = f"{GLUESYNC_URL}/{target_url}"
    
    try:
        token = get_gluesync_token()
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Authentication token generation failed: {e}")
        
    headers = req.headers or {}
    headers["Authorization"] = f"Bearer {token}"
    if req.body is not None:
        headers["Content-Type"] = "application/json"
        data_payload = json.dumps(req.body).encode("utf-8")
    else:
        data_payload = None
        
    method = req.method.upper()
    try:
        request_obj = urllib.request.Request(
            url=target_url,
            data=data_payload,
            headers=headers,
            method=method
        )
        
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(request_obj, context=ctx, timeout=10) as response:
            resp_body = response.read().decode("utf-8")
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return json.loads(resp_body)
            return resp_body
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        status = e.code
        try:
            return JSONResponse(status_code=status, content=json.loads(err_body))
        except:
            raise HTTPException(status_code=status, detail=err_body or e.reason)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bad Gateway proxying to GlueSync: {e}")

# ─────────────────────────────────────────────
# Settings API
# ─────────────────────────────────────────────

@router.get("/api/settings")
def get_settings():
    try:
        conn = get_db()
        rows = conn.execute("SELECT key, value FROM scheduler_settings").fetchall()
        return {r['key']: r['value'] for r in rows}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/settings")
def save_settings(req: SettingsRequest):
    try:
        conn = get_db()
        conn.execute("INSERT OR REPLACE INTO scheduler_settings (key, value) VALUES ('log_path', ?)", (req.log_path,))
        conn.execute("INSERT OR REPLACE INTO scheduler_settings (key, value) VALUES ('app_log_path', ?)", (req.app_log_path or '',))
        conn.execute("INSERT OR REPLACE INTO scheduler_settings (key, value) VALUES ('app_log_level', ?)", (req.app_log_level or 'INFO',))
        conn.execute("INSERT OR REPLACE INTO scheduler_settings (key, value) VALUES ('app_log_max_size', ?)", (str(req.app_log_max_size) if req.app_log_max_size is not None else '10',))
        conn.commit()
        # Dynamically apply the new log settings
        configure_app_logging(conn)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────
# Report Profiles API
# ─────────────────────────────────────────────

@router.get("/api/profiles/report")
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

@router.post("/api/profiles/report")
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

@router.delete("/api/profiles/report/{profile_id}")
def delete_report_profile(profile_id: int):
    try:
        conn = get_db()
        conn.execute("DELETE FROM report_profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────
# Mailer Profiles API
# ─────────────────────────────────────────────

@router.get("/api/profiles/mailer")
def list_mailer_profiles():
    try:
        conn = get_db()
        rows = conn.execute("SELECT * FROM mailer_profiles").fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/profiles/mailer")
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

@router.delete("/api/profiles/mailer/{profile_id}")
def delete_mailer_profile(profile_id: int):
    try:
        conn = get_db()
        conn.execute("DELETE FROM mailer_profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────
# Scheduler Jobs API
# ─────────────────────────────────────────────

@router.get("/api/scheduler/jobs")
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

@router.post("/api/scheduler/jobs")
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

@router.delete("/api/scheduler/jobs/{job_id}")
def delete_scheduler_job(job_id: int):
    try:
        conn = get_db()
        conn.execute("DELETE FROM scheduler_jobs WHERE id = ?", (job_id,))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/api/scheduler/jobs/{job_id}/toggle")
def toggle_scheduler_job(job_id: int, enabled: bool):
    try:
        conn = get_db()
        conn.execute("UPDATE scheduler_jobs SET enabled = ? WHERE id = ?", (1 if enabled else 0, job_id))
        conn.commit()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/scheduler/logs")
def get_scheduler_logs(lines: int = 50):
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM scheduler_settings WHERE key = 'log_path'").fetchone()
        default_log_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../logs/scheduler.log"))
        log_path = row['value'] if row else default_log_path
        if not os.path.exists(log_path):
            return []
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.readlines()
        return [l.strip() for l in content[-lines:]]
    except Exception as e:
         raise HTTPException(status_code=500, detail=str(e))

# ─────────────────────────────────────────────
# Scheduler Engine
# ─────────────────────────────────────────────

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
        agent_tag_map = _get_agent_tag_map(pipeline_id)
        
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
            src_agent_id = src_agent.get("agentId")
            src_tag = agent_tag_map.get(src_agent_id, "source")

            tgt_info   = tgt_agent.get("table", {})
            tgt_schema = tgt_info.get("schema", "")
            tgt_table  = tgt_info.get("name", "")
            tgt_agent_id = tgt_agent.get("agentId")
            tgt_tag = agent_tag_map.get(tgt_agent_id, "target")

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
                    src_cnt, src_ts = _get_database_count(src_tag, src_library, src_table, pipeline_id, src_agent_id)
                    
                    from zoneinfo import ZoneInfo
                    try:
                        local_tz = ZoneInfo("Asia/Bangkok")
                    except:
                        local_tz = None
                    now_local = datetime.now(local_tz) if local_tz else datetime.now()
                    src_check_time = now_local.strftime("%Y-%m-%d %H:%M:%S")
                    
                    res["source_count"] = src_cnt
                    res["source_last_ts"] = src_check_time
                except Exception as e:
                    res["error"] = f"{src_tag.upper()} Error: {str(e)[:150]}"
            else:
                res["error"] = "No source table info"
                
            if tgt_schema and tgt_table:
                try:
                    tgt_cnt, tgt_ts = _get_database_count(tgt_tag, tgt_schema, tgt_table, pipeline_id, tgt_agent_id)
                    
                    from zoneinfo import ZoneInfo
                    try:
                        local_tz = ZoneInfo("Asia/Bangkok")
                    except:
                        local_tz = None
                    now_local = datetime.now(local_tz) if local_tz else datetime.now()
                    tgt_check_time = now_local.strftime("%Y-%m-%d %H:%M:%S")
                    
                    res["target_count"] = tgt_cnt
                    res["target_last_ts"] = tgt_check_time
                except Exception as e:
                    res["error"] = f"{tgt_tag.upper()} Error: {str(e)[:150]}"
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
               <a href="{APP_EXTERNAL_URL}{report_url}" style="display:inline-block; padding: 10px 20px; background-color: #3b82f6; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">
                  Open Full Report ↗
               </a>
            </p>
            <hr style="border: 1px solid #eee; margin: 15px 0;">
            <p>{body_ending_html}</p>
        </div>
        """
        save_mock_email(emails, mail_subject, summary_html)
        write_scheduler_log(log_path, f"Job '{job_name}': Simulated email summary successfully generated for recipient(s): {emails}")
        
        # Write custom email log to mail.log
        try:
            mail_log_dir = os.path.dirname(log_path)
            mail_log_path = os.path.join(mail_log_dir, "mail.log")
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(mail_log_path, 'a', encoding='utf-8') as mf:
                mf.write(f"[{now_str}] EMAIL \"Please find daily report in {APP_EXTERNAL_URL}{report_url}\"\n")
        except Exception as ex:
            print(f"[scheduler] Error writing mail.log: {ex}", flush=True)
    except Exception as e:
        err_msg = f"Error executing job '{job_name}': {str(e)}\n{traceback.format_exc()}"
        print(f"[scheduler] {err_msg}", flush=True)
        write_scheduler_log(log_path, err_msg)

def run_scheduled_jobs():
    dt = datetime.now()
    log_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../logs/scheduler.log"))
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
