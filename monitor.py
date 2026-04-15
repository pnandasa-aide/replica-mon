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
from lib.per_entity_tracker import PerEntityTracker


def check_entity_prerequisites(source_table: str, target_table: str, qadmcli_path: str = "../qadmcli/qadmcli.sh") -> dict:
    """
    Check if entity has proper prerequisites (journal on AS400, CT on MSSQL).
    
    Args:
        source_table: AS400 table (LIBRARY.TABLE)
        target_table: MSSQL table (SCHEMA.TABLE)
        qadmcli_path: Path to qadmcli
        
    Returns:
        Dictionary with prerequisite status
    """
    result = {
        'source_table': source_table,
        'target_table': target_table,
        'journal_enabled': False,
        'ct_enabled': False,
        'ready': False,
        'issues': []
    }
    
    # Check AS400 journal (quick check - just verify journal exists)
    try:
        parts = source_table.split('.')
        if len(parts) == 2:
            library, table = parts
            # Use --format json for clean parsing
            cmd = [qadmcli_path, "journal", "info", "-t", table, "-l", library, "--format", "json"]
            proc_result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            
            if proc_result.returncode == 0:
                import json
                import re
                
                # Parse JSON output
                output = proc_result.stdout
                # Strip ANSI escape codes
                ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
                clean_output = ansi_escape.sub('', output)
                
                # Find and parse JSON
                start_idx = clean_output.find('{')
                if start_idx >= 0:
                    json_str = clean_output[start_idx:]
                    data = json.loads(json_str)
                    
                    # Check is_journaled field
                    if data.get('is_journaled', False):
                        result['journal_enabled'] = True
                    else:
                        result['issues'].append(f"AS400 table {source_table} is not journaled")
                else:
                    result['issues'].append(f"Failed to parse journal info for {source_table}")
            else:
                result['issues'].append(f"Could not check journal status for {source_table}")
    except Exception as e:
        result['issues'].append(f"Error checking AS400 journal: {str(e)}")
    
    # Check MSSQL CT
    try:
        ct_reader = MSSQLCTReader(qadmcli_path=qadmcli_path, use_cache=False)
        result['ct_enabled'] = ct_reader.is_ct_enabled(target_table)
        
        if not result['ct_enabled']:
            result['issues'].append(f"MSSQL Change Tracking not enabled on {target_table}")
    except Exception as e:
        result['issues'].append(f"Error checking MSSQL CT: {str(e)}")
    
    # Overall readiness
    result['ready'] = result['journal_enabled'] and result['ct_enabled']
    
    return result


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
    qadmcli_path: str = "../qadmcli/qadmcli.sh",
    verbose: bool = False,
    time_window_start: str = None  # NEW: Start of time window for delta counting
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
        # Check prerequisites first
        if verbose:
            print(f"    → Checking prerequisites...")
        prereq = check_entity_prerequisites(source_table, target_table, qadmcli_path)
        result['journal_enabled'] = prereq['journal_enabled']
        result['ct_enabled'] = prereq['ct_enabled']
        result['prerequisites_met'] = prereq['ready']
        
        if not prereq['ready']:
            result['status'] = "⚠️  PREREQ FAILED"
            result['issues'] = prereq['issues']
            if verbose:
                for issue in prereq['issues']:
                    print(f"    → ⚠️  {issue}")
            return result
        
        if verbose:
            print(f"    → ✓ Prerequisites OK (Journal: Yes, CT: Yes)")
        
        # Get cache info
        if verbose:
            print(f"    → Checking cache status...")
        cache = JournalCache()
        cache_info = cache.get_cache_info(source_table)
        result["cache_status"] = cache_info.get('cache_level', 'none')
        result["requires_attention"] = cache_info.get('requires_attention', False)
        result["cached_at"] = cache_info.get('cached_at')
        
        if verbose:
            if cache_info.get('cached'):
                print(f"    → Cache found: {cache_info.get('entry_count', 0)} entries, last updated: {cache_info.get('cached_at')}")
            else:
                print(f"    → No cache found, will query AS400")
        
        # Detect timezones
        if verbose:
            print(f"    → Detecting timezones...")
        tz_info = get_timezone_info(qadmcli_path)
        as400_tz = tz_info['as400']['utc_offset']
        mssql_tz = tz_info['mssql']['utc_offset']
        
        # Normalize timestamp for AS400
        since_for_as400 = normalize_to_as400_time(since, mssql_tz, as400_tz) if since else None
        
        # Get AS400 journal summary
        if verbose:
            if time_window_start:
                print(f"    → Querying AS400 journal (time window: {time_window_start} to now)...")
            else:
                print(f"    → Querying AS400 journal (this may take 60-120s for first run)...")
        
        journal_reader = AS400JournalReader(qadmcli_path=qadmcli_path, use_cache=use_cache)
        
        # Use time-windowed aggregation if we have a window start
        if time_window_start and use_cache:
            # Aggregate from cache for this time window (FAST!)
            journal_summary = journal_reader.get_summary(
                source_table, 
                since=time_window_start,
                use_time_window=True  # Enable time-windowed aggregation
            )
        else:
            # First run or no cache - fetch all entries
            journal_summary = journal_reader.get_summary(
                source_table, 
                since=since_for_as400,
                use_time_window=False
            )
        
        result["journal_total"] = journal_summary.get('total', 0)
        result["journal_inserts"] = journal_summary.get('inserts', 0)
        result["journal_updates"] = journal_summary.get('updates', 0)
        result["journal_deletes"] = journal_summary.get('deletes', 0)
        
        if verbose:
            from_cache = journal_summary.get('from_cache', False)
            if from_cache:
                print(f"    → ✓ AS400 journal: {result['journal_total']} entries (from cache)")
            else:
                print(f"    → ✓ AS400 journal: {result['journal_total']} entries (queried from AS400)")
        
        # Get MSSQL CT summary
        if verbose:
            if time_window_start:
                print(f"    → Querying MSSQL CT (time window: {time_window_start} to now)...")
            else:
                print(f"    → Querying MSSQL Change Tracking...")
        ct_reader = MSSQLCTReader(qadmcli_path=qadmcli_path, use_cache=use_cache)
        
        if ct_reader.is_ct_enabled(target_table):
            # Use time-windowed aggregation if we have a window start
            if time_window_start and use_cache:
                # Aggregate from cache for this time window (FAST!)
                ct_summary = ct_reader.get_summary(
                    target_table, 
                    since=time_window_start,
                    use_time_window=True  # Enable time-windowed aggregation
                )
            else:
                # First run or no cache - fetch all changes
                ct_summary = ct_reader.get_summary(
                    target_table, 
                    since=since,
                    use_time_window=False
                )
            
            result["ct_total"] = ct_summary.get('total', 0)
            result["ct_inserts"] = ct_summary.get('inserts', 0)
            result["ct_updates"] = ct_summary.get('updates', 0)
            result["ct_deletes"] = ct_summary.get('deletes', 0)
            
            if verbose:
                from_cache = ct_summary.get('from_cache', False)
                if from_cache:
                    print(f"    → ✓ MSSQL CT: {result['ct_total']} entries (from cache)")
                else:
                    print(f"    → ✓ MSSQL CT: {result['ct_total']} entries (queried from MSSQL)")
        else:
            result["ct_enabled"] = False
            result["ct_total"] = -1  # Indicates not available
            if verbose:
                print(f"    → ⚠️  CT not enabled on {target_table}")
        
        # Compare
        if result["ct_total"] >= 0:
            comparator = ChangeComparator()
            comparison = comparator.compare(journal_summary, ct_summary)
            
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
        import traceback
        result["traceback"] = traceback.format_exc()
        
        # Print error details if verbose mode
        if verbose:
            print(f"    → ❌ EXCEPTION: {e}")
            print(f"    → Traceback:")
            for line in traceback.format_exc().split('\n'):
                if line.strip():
                    print(f"      {line}")
        
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
        print(f"{'Source Table':<25} {'Target Table':<25} {'Status':<15} {'Journal':>8} {'CT':>8} {'Diff':>6} {'Cache':<10} {'Attention':<10}")
        print("-" * 120)
    else:
        print(f"{'Source Table':<25} {'Target Table':<25} {'Status':<15} {'Journal':>8} {'CT':>8} {'Diff':>6}")
        print("-" * 90)
    
    # Table rows
    for r in results:
        journal = r.get('journal_total', 0)
        ct = r.get('ct_total', 0)
        diff = journal - ct if ct >= 0 else 0
        
        if show_cache:
            cache_status = r.get('cache_status', 'none')
            attention = "🚨 YES" if r.get('requires_attention', False) else "✓ No"
            print(f"{r['source']:<25} {r['target']:<25} {r['status']:<15} {journal:>8} {ct:>8} {diff:>+6} {cache_status:<10} {attention:<10}")
        else:
            print(f"{r['source']:<25} {r['target']:<25} {r['status']:<15} {journal:>8} {ct:>8} {diff:>+6}")
        
        # Show issues if any
        if r.get('issues'):
            for issue in r['issues']:
                print(f"  ⚠️  {issue}")
    
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
    verbose: bool = False,
    qadmcli_path: str = "../qadmcli/qadmcli.sh",
    time_window_start: str = None,  # NEW: Start of time window for aggregation
    show_per_entity: bool = True  # NEW: Show per-entity progress report
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
            qadmcli_path=qadmcli_path,
            verbose=verbose,
            time_window_start=time_window_start  # NEW: Pass time window
        )
        
        results.append(result)
        
        # Print status immediately
        print(f"    → {result['status']} (Journal: {result.get('journal_total', 0)}, CT: {result.get('ct_total', 0)})")
    
    # Display consolidated results
    if output_format == "json":
        display_results_json(results)
    else:
        display_results_table(results, show_cache=show_cache)
    
    # NEW: Display per-entity progress report
    if show_per_entity and output_format == "table":
        # Extract library from first entity (assuming all entities use same library)
        if entities:
            first_source = entities[0].get('source', '')
            if '.' in first_source:
                library = first_source.split('.')[0]
                
                # Initialize per-entity tracker
                cache_dir = os.path.join(os.path.dirname(__file__), 'cache')
                tracker = PerEntityTracker(cache_dir)
                
                # Show report
                print(tracker.format_per_entity_report(library, time_window_start))
    
    return results


def run_continuous_monitoring(
    config: Dict,
    interval_seconds: int = 300,
    since: str = None,
    output_format: str = "table",
    use_cache: bool = True,
    show_cache: bool = True,
    verbose: bool = False,
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
    last_cycle_time = None  # Track time window for aggregation
    
    try:
        while True:
            cycle_count += 1
            cycle_start = datetime.now()
            
            # Print cycle header
            if output_format == "table":
                print(f"\n{'='*120}")
                print(f"🔄 MONITORING CYCLE #{cycle_count} - {cycle_start.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"{'='*120}")
            
            # Calculate time window for this cycle
            # First cycle: use --since parameter or beginning of time
            # Subsequent cycles: use last cycle time (delta counting)
            time_window_start = last_cycle_time
            
            # Run monitoring
            results = run_monitoring_cycle(
                config=config,
                since=since,
                output_format=output_format,
                use_cache=use_cache,
                show_cache=show_cache,
                verbose=verbose,
                qadmcli_path=qadmcli_path,
                time_window_start=time_window_start  # NEW: Pass time window
            )
            
            # Update last cycle time for next iteration
            last_cycle_time = cycle_start.strftime("%Y-%m-%d %H:%M:%S")
            
            # Save metrics to file-based storage
            try:
                from lib.metrics_storage import save_monitoring_metrics
                save_monitoring_metrics(results)
            except Exception as e:
                if verbose:
                    print(f"  ⚠️  Warning: Could not save metrics: {e}")
            
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
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed progress logging")
    parser.add_argument("--no-per-entity", action="store_true", help="Hide per-entity progress report")
    
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
            show_cache=not args.no_cache_status,
            verbose=args.verbose,
            show_per_entity=not args.no_per_entity
        )
    else:
        results = run_monitoring_cycle(
            config=config,
            since=args.since,
            output_format=args.format,
            use_cache=not args.no_cache,
            show_cache=not args.no_cache_status,
            verbose=args.verbose,
            show_per_entity=not args.no_per_entity
        )
