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


def load_pipeline_config(config_path: str = None) -> Dict:
    """
    Load pipeline configuration from gluesync config.
    
    Returns:
        Dictionary with pipeline and entity information
    """
    # For now, we'll use a simple entity list
    # In production, this would read from GlueSync API or config file
    config_file = config_path or "entities.json"
    
    if os.path.exists(config_file):
        with open(config_file, 'r') as f:
            return json.load(f)
    
    # Default example configuration
    return {
        "pipeline": "main-pipeline",
        "entities": [
            {
                "source": "GSLIBTST.CUSTOMERS",
                "target": "dbo.Customers",
                "status": "active"
            },
            {
                "source": "GSLIBTST.CUSTOMERS2",
                "target": "dbo.Customers2",
                "status": "active"
            }
        ]
    }


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
    
    args = parser.parse_args()
    
    # Load configuration
    config = load_pipeline_config(args.config)
    
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
