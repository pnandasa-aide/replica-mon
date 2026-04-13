#!/usr/bin/env python3
"""
Automated Replication Monitor

Reads active pipelines/entities, runs comparison checks,
and displays results in table or JSON format.
Supports continuous monitoring with configurable intervals.
"""

import json
import sys
import time
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from lib.as400_journal import AS400JournalReader
from lib.mssql_ct import MSSQLCTReader
from lib.comparator import ChangeComparator
from lib.timezone import get_timezone_info, format_timezone_report, normalize_to_as400_time
from lib.journal_cache import JournalCache


def discover_entities_from_gluesync(gluesync_cli_path: str = None) -> Dict:
    """
    Auto-discover entities from GlueSync CLI.
    
    Args:
        gluesync_cli_path: Path to gluesync_cli.py
        
    Returns:
        Dictionary with pipeline and entity information
    """
    if gluesync_cli_path is None:
        # Auto-detect gluesync-cli location
        script_dir = Path(__file__).parent.parent
        gluesync_cli_path = script_dir / "gluesync-cli" / "gluesync_cli.py"
    
    if not os.path.exists(gluesync_cli_path):
        print(f"  ⚠️  GlueSync CLI not found at: {gluesync_cli_path}")
        print(f"  ℹ️  Falling back to entities.json if available")
        return None
    
    try:
        # Step 1: Get pipeline list
        result = subprocess.run(
            ["python3", str(gluesync_cli_path), "-o", "json", "pipeline", "list"],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode != 0:
            print(f"  ⚠️  Failed to get pipeline list: {result.stderr}")
            return None
        
        pipelines = json.loads(result.stdout)
        
        if not pipelines:
            print(f"  ⚠️  No pipelines found in GlueSync")
            return None
        
        # Use first pipeline (or you could add logic to select specific one)
        pipeline = pipelines[0]
        pipeline_id = pipeline.get('id')
        pipeline_name = pipeline.get('name', 'Unknown')
        
        if not pipeline_id:
            print(f"  ⚠️  Pipeline has no ID")
            return None
        
        print(f"  📡 Discovered pipeline: {pipeline_name} ({pipeline_id})")
        
        # Step 2: Get entities for this pipeline
        result = subprocess.run(
            ["python3", str(gluesync_cli_path), "-o", "json", "entity", "list", pipeline_id],
            capture_output=True,
            text=True,
            timeout=15
        )
        
        if result.returncode != 0:
            print(f"  ⚠️  Failed to get entities: {result.stderr}")
            return None
        
        entities_raw = json.loads(result.stdout)
        
        # Step 3: Transform to our format
        entities = []
        for entity in entities_raw:
            entity_name = entity.get('entityName', '')
            entity_id = entity.get('entityId', '')
            status = entity.get('status', 'unknown')
            
            # Parse AS400 table name (format: LIBRARY.TABLE)
            source_table = entity_name
            
            # Generate target table name (replace library with dbo)
            parts = entity_name.split('.')
            if len(parts) == 2:
                target_table = f"dbo.{parts[1]}"
            else:
                target_table = f"dbo.{entity_name}"
            
            entities.append({
                "entityId": entity_id,
                "source": source_table,
                "target": target_table,
                "status": "active" if status == "configured" else status,
                "description": f"Auto-discovered from GlueSync"
            })
        
        print(f"  ✓ Found {len(entities)} active entities")
        
        return {
            "pipeline": pipeline_id,
            "pipeline_name": pipeline_name,
            "description": f"Auto-discovered from GlueSync pipeline",
            "discovered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "entities": entities
        }
        
    except subprocess.TimeoutExpired:
        print(f"  ⚠️  GlueSync CLI command timed out")
        return None
    except json.JSONDecodeError as e:
        print(f"  ⚠️  Failed to parse GlueSync CLI output: {e}")
        return None
    except Exception as e:
        print(f"  ⚠️  Error discovering entities: {e}")
        return None


def load_pipeline_config(config_path: str = None, auto_discover: bool = True) -> Dict:
    """
    Load pipeline configuration.
    
    Priority:
    1. If config_path specified: Load from file
    2. If auto_discover=True: Try GlueSync CLI
    3. Fallback: Load from default entities.json
    
    Returns:
        Dictionary with pipeline and entity information
    """
    # Option 1: Explicit config file
    if config_path:
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
            print(f"  📄 Loaded config from: {config_path}")
            return config
        else:
            print(f"  ⚠️  Config file not found: {config_path}")
            if not auto_discover:
                return {"entities": []}
    
    # Option 2: Auto-discover from GlueSync CLI
    if auto_discover:
        print("\n🔍 Auto-discovering entities from GlueSync...")
        config = discover_entities_from_gluesync()
        if config:
            # Save discovered config for future reference
            default_config = Path(__file__).parent / "entities.json"
            with open(default_config, 'w') as f:
                json.dump(config, f, indent=2)
            print(f"  💾 Saved discovered config to: {default_config}")
            return config
    
    # Option 3: Fallback to default entities.json
    default_config = Path(__file__).parent / "entities.json"
    if default_config.exists():
        with open(default_config, 'r') as f:
            config = json.load(f)
        print(f"  📄 Loaded from cache: {default_config}")
        return config
    
    # No config available
    print(f"  ⚠️  No configuration available")
    return {"entities": []}


def get_entity_comparison(
    source_table: str,
    target_table: str,
    since: str = None,
    use_cache: bool = True,
    qadmcli_path: str = "../qadmcli/qadmcli.sh"
) -> Dict:
    """
    Run comparison for a single entity.
    
    Returns:
        Dictionary with comparison results
    """
    result = {
        "source": source_table,
        "target": target_table,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "unknown",
        "journal_total": 0,
        "ct_total": 0,
        "discrepancies": [],
        "cache_status": "unknown"
    }
    
    try:
        # Get cache info
        cache = JournalCache()
        cache_info = cache.get_cache_info(source_table)
        result["cache_status"] = cache_info.get('cache_level', 'none')
        result["requires_attention"] = cache_info.get('requires_attention', False)
        result["cached_at"] = cache_info.get('cached_at')
        
        # Detect timezones
        tz_info = get_timezone_info(qadmcli_path)
        as400_tz = tz_info['as400']['utc_offset']
        mssql_tz = tz_info['mssql']['utc_offset']
        
        # Normalize timestamp for AS400
        since_for_as400 = normalize_to_as400_time(since, mssql_tz, as400_tz) if since else None
        
        # Get AS400 journal summary
        journal_reader = AS400JournalReader(qadmcli_path=qadmcli_path, use_cache=use_cache)
        journal_summary = journal_reader.get_summary(source_table, since_for_as400)
        
        result["journal_total"] = journal_summary.get('total', 0)
        result["journal_inserts"] = journal_summary.get('inserts', 0)
        result["journal_updates"] = journal_summary.get('updates', 0)
        result["journal_deletes"] = journal_summary.get('deletes', 0)
        
        # Get MSSQL CT summary
        ct_reader = MSSQLCTReader(qadmcli_path=qadmcli_path)
        
        if ct_reader.is_ct_enabled(target_table):
            ct_summary = ct_reader.get_summary(target_table, since)
            result["ct_total"] = ct_summary.get('total', 0)
            result["ct_inserts"] = ct_summary.get('inserts', 0)
            result["ct_updates"] = ct_summary.get('updates', 0)
            result["ct_deletes"] = ct_summary.get('deletes', 0)
        else:
            result["ct_enabled"] = False
            result["ct_total"] = -1  # Indicates not available
        
        # Compare
        if result["ct_total"] >= 0:
            comparator = ChangeComparator()
            comparison = comparator.compare_summaries(journal_summary, ct_summary)
            
            result["match"] = comparison.get('match', False)
            result["discrepancies"] = comparison.get('discrepancies', [])
            
            if comparison.get('match', False):
                result["status"] = "✅ OK"
            else:
                result["status"] = "❌ MISMATCH"
        else:
            result["status"] = "⚠️  CT Not Enabled"
        
        return result
        
    except Exception as e:
        result["status"] = "❌ ERROR"
        result["error"] = str(e)
        return result


def display_results_table(results: List[Dict], show_cache: bool = True):
    """Display results in formatted table."""
    print("\n" + "=" * 120)
    print("REPLICATION MONITORING RESULTS")
    print("=" * 120)
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Table header
    if show_cache:
        print(f"{'Source Table':<25} {'Target Table':<25} {'Status':<12} {'Journal':>8} {'CT':>8} {'Diff':>6} {'Cache':<10} {'Attention':<10}")
        print("-" * 120)
    else:
        print(f"{'Source Table':<25} {'Target Table':<25} {'Status':<12} {'Journal':>8} {'CT':>8} {'Diff':>6}")
        print("-" * 90)
    
    # Table rows
    for r in results:
        journal = r.get('journal_total', 0)
        ct = r.get('ct_total', 0)
        diff = journal - ct if ct >= 0 else 0
        
        if show_cache:
            cache_status = r.get('cache_status', 'none')
            attention = "🚨 YES" if r.get('requires_attention', False) else "✓ No"
            print(f"{r['source']:<25} {r['target']:<25} {r['status']:<12} {journal:>8} {ct:>8} {diff:>+6} {cache_status:<10} {attention:<10}")
        else:
            print(f"{r['source']:<25} {r['target']:<25} {r['status']:<12} {journal:>8} {ct:>8} {diff:>+6}")
    
    print("=" * 120)
    
    # Summary
    ok_count = sum(1 for r in results if 'OK' in r['status'])
    error_count = sum(1 for r in results if 'ERROR' in r['status'] or 'MISMATCH' in r['status'])
    attention_count = sum(1 for r in results if r.get('requires_attention', False))
    
    print(f"\nSummary: {ok_count} OK, {error_count} Issues, {attention_count} Flagged for Attention")
    print()


def display_results_json(results: List[Dict]):
    """Display results in JSON format."""
    output = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_entities": len(results),
        "results": results,
        "summary": {
            "ok": sum(1 for r in results if 'OK' in r['status']),
            "errors": sum(1 for r in results if 'ERROR' in r['status'] or 'MISMATCH' in r['status']),
            "attention_required": sum(1 for r in results if r.get('requires_attention', False))
        }
    }
    
    print(json.dumps(output, indent=2))


def run_monitoring_cycle(
    config: Dict,
    since: str = None,
    output_format: str = "table",
    use_cache: bool = True,
    show_cache: bool = True,
    qadmcli_path: str = "../qadmcli/qadmcli.sh"
) -> List[Dict]:
    """
    Run one monitoring cycle for all entities.
    
    Returns:
        List of comparison results
    """
    entities = config.get('entities', [])
    results = []
    
    print(f"\n📊 Monitoring {len(entities)} entities...")
    print("=" * 120)
    
    for i, entity in enumerate(entities, 1):
        source = entity.get('source', '')
        target = entity.get('target', '')
        status = entity.get('status', 'active')
        
        if status != 'active':
            print(f"  [{i}/{len(entities)}] Skipping {source} (status: {status})")
            continue
        
        print(f"  [{i}/{len(entities)}] Checking {source} → {target}...")
        
        result = get_entity_comparison(
            source_table=source,
            target_table=target,
            since=since,
            use_cache=use_cache,
            qadmcli_path=qadmcli_path
        )
        
        results.append(result)
        
        # Print status immediately
        print(f"    → {result['status']} (Journal: {result.get('journal_total', 0)}, CT: {result.get('ct_total', 0)})")
    
    # Display consolidated results
    if output_format == "json":
        display_results_json(results)
    else:
        display_results_table(results, show_cache=show_cache)
    
    return results


def run_continuous_monitoring(
    config: Dict,
    interval_seconds: int = 300,
    since: str = None,
    output_format: str = "table",
    use_cache: bool = True,
    show_cache: bool = True,
    qadmcli_path: str = "../qadmcli/qadmcli.sh"
):
    """
    Run monitoring in continuous loop.
    
    Args:
        config: Pipeline configuration
        interval_seconds: Seconds between checks (default: 300 = 5 minutes)
        since: Timestamp filter
        output_format: "table" or "json"
        use_cache: Enable caching
        show_cache: Show cache status in table
        qadmcli_path: Path to qadmcli
    """
    print("=" * 120)
    print("CONTINUOUS REPLICATION MONITOR")
    print("=" * 120)
    print(f"Interval: {interval_seconds} seconds ({interval_seconds/60:.1f} minutes)")
    print(f"Output Format: {output_format}")
    print(f"Caching: {'Enabled' if use_cache else 'Disabled'}")
    print(f"Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 120)
    print("\nPress Ctrl+C to stop\n")
    
    cycle_count = 0
    
    try:
        while True:
            cycle_count += 1
            
            # Print cycle header
            if output_format == "table":
                print(f"\n{'='*120}")
                print(f"🔄 MONITORING CYCLE #{cycle_count} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*120}")
            
            # Run monitoring
            results = run_monitoring_cycle(
                config=config,
                since=since,
                output_format=output_format,
                use_cache=use_cache,
                show_cache=show_cache,
                qadmcli_path=qadmcli_path
            )
            
            # Wait for next cycle
            if interval_seconds > 0:
                print(f"\n⏳ Next check in {interval_seconds} seconds... (Ctrl+C to stop)")
                time.sleep(interval_seconds)
            
    except KeyboardInterrupt:
        print(f"\n\n{'='*120}")
        print(f"⏹️  Monitoring stopped after {cycle_count} cycles")
        print(f"{'='*120}")
        sys.exit(0)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Automated Replication Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single monitoring run (table output)
  python3 monitor.py
  
  # Single run (JSON output for applications/UI)
  python3 monitor.py --format json
  
  # Continuous monitoring every 5 minutes (default)
  python3 monitor.py --continuous
  
  # Continuous monitoring every 1 minute
  python3 monitor.py --continuous --interval 60
  
  # Check last hour only
  python3 monitor.py --since "$(date -d '1 hour ago' '+%Y-%m-%d %H:%M:%S')"
  
  # Use custom entity config
  python3 monitor.py --config my_entities.json
        """
    )
    
    parser.add_argument("--config", help="Path to entities config file (JSON)")
    parser.add_argument("--since", help="Filter since timestamp")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format")
    parser.add_argument("--continuous", action="store_true", help="Run in continuous mode")
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds (default: 300 = 5 min)")
    parser.add_argument("--no-cache", action="store_true", help="Disable caching")
    parser.add_argument("--no-cache-status", action="store_true", help="Hide cache status in table")
    parser.add_argument("--no-auto-discover", action="store_true", help="Disable auto-discovery from GlueSync CLI (use entities.json only)")
    
    args = parser.parse_args()
    
    # Load configuration with auto-discovery
    config = load_pipeline_config(
        config_path=args.config,
        auto_discover=not args.no_auto_discover
    )
    
    # Run monitoring
    if args.continuous:
        run_continuous_monitoring(
            config=config,
            interval_seconds=args.interval,
            since=args.since,
            output_format=args.format,
            use_cache=not args.no_cache,
            show_cache=not args.no_cache_status
        )
    else:
        results = run_monitoring_cycle(
            config=config,
            since=args.since,
            output_format=args.format,
            use_cache=not args.no_cache,
            show_cache=not args.no_cache_status
        )
