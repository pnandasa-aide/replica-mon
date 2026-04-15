#!/usr/bin/env python3
"""
Continuous Journal Feeder

Streams journal entries from AS400 to local SQLite cache.
Runs on a schedule (every 1-5 minutes).
Minimal processing - just fetch and store.

Architecture:
- Fetches ONLY new entries since last cached position
- Stores in SQLite cache (fast local operation)
- NO complex filtering on AS400 (minimal impact)
- All filtering/aggregation happens locally on cache

Usage:
    # Run continuously
    python3 -m lib.journal_feeder --tables GSLIBTST.CUSTOMERS GSLIBTST.ORDERS --interval 300
    
    # Run once (for testing)
    python3 -m lib.journal_feeder --tables GSLIBTST.CUSTOMERS --once
    
    # Run as daemon
    python3 -m lib.journal_feeder --tables GSLIBTST.CUSTOMERS --daemon
"""

import sys
import os
import time
import logging
import argparse
import subprocess
import json
from datetime import datetime
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.sqlite_journal_cache import SQLiteJournalCache


class JournalFeeder:
    """
    Continuous journal feeder for AS400 → SQLite cache.
    
    Minimizes AS400 impact by:
    - Using incremental fetch (only new entries)
    - Sequential reads (no complex queries)
    - No filtering/aggregation on AS400
    """
    
    def __init__(
        self,
        qadmcli_path: str = "../qadmcli/qadmcli.sh",
        cache_dir: str = "cache",
        poll_interval: int = 300,
        retention_days: int = 7
    ):
        """
        Initialize journal feeder.
        
        Args:
            qadmcli_path: Path to qadmcli.sh
            cache_dir: Directory for SQLite cache
            poll_interval: Seconds between fetches (default: 300 = 5 min)
            retention_days: Days to keep entries in cache
        """
        self.qadmcli_path = qadmcli_path
        self.poll_interval = poll_interval
        self.cache = SQLiteJournalCache(cache_dir, retention_days=retention_days)
        
        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    def fetch_and_store(self, table: str) -> dict:
        """
        Fetch new journal entries from AS400 and store in cache.
        
        Args:
            table: Table name in format LIBRARY.TABLE
            
        Returns:
            Dictionary with fetch statistics
        """
        self.logger.info(f"Fetching entries for {table}...")
        
        # Parse table name
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        # Get last cached position (for incremental fetch)
        cache_info = self.cache.get_cache_info(table)
        last_sequence = cache_info.get('last_sequence', 0)
        last_timestamp = cache_info.get('last_timestamp')
        
        self.logger.info(f"  Last cached: sequence={last_sequence}, timestamp={last_timestamp}")
        
        # Build qadmcli command
        cmd = [
            self.qadmcli_path,
            "journal", "entries",
            "-t", table_name,
            "-l", library,
            "--format", "json"  # Get structured JSON output
        ]
        
        # Incremental fetch: only get entries since last timestamp
        if last_timestamp:
            cmd.extend(["--from-time", last_timestamp])
            self.logger.info(f"  → Fetching NEW entries since {last_timestamp}")
        else:
            self.logger.info(f"  → First fetch (will get ALL entries)")
        
        # Execute command
        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            fetch_time = time.time() - start_time
            
            if result.returncode != 0:
                self.logger.error(f"  ❌ qadmcli failed: {result.stderr}")
                return {
                    'table': table,
                    'status': 'error',
                    'entries_fetched': 0,
                    'fetch_time': fetch_time,
                    'error': result.stderr
                }
            
            # Parse JSON output
            output = result.stdout.strip()
            
            # qadmcli returns JSON array directly
            entries = json.loads(output)
            
            if not isinstance(entries, list):
                # Sometimes wrapped in object
                entries = entries.get('entries', [])
            
            self.logger.info(f"  → Received {len(entries)} entries from AS400 ({fetch_time:.1f}s)")
            
            # Store in SQLite cache (fast local operation)
            if entries:
                store_start = time.time()
                
                # Extract last sequence and timestamp for metadata
                last_entry = entries[-1]
                last_seq = last_entry.get('entry_number', 0)
                last_ts = last_entry.get('entry_timestamp')
                
                new_count = self.cache.store_entries(
                    table, 
                    entries,
                    last_sequence=last_seq,
                    last_timestamp=last_ts
                )
                store_time = time.time() - store_start
                
                self.logger.info(f"  ✓ Stored {new_count} new entries in cache ({store_time:.2f}s)")
                self.logger.info(f"  → Cache updated: last_sequence={last_seq}, last_timestamp={last_ts}")
                
                return {
                    'table': table,
                    'status': 'success',
                    'entries_fetched': len(entries),
                    'entries_stored': new_count,
                    'fetch_time': fetch_time,
                    'store_time': store_time,
                    'last_sequence': last_seq,
                    'last_timestamp': last_ts
                }
            else:
                self.logger.info(f"  → No new entries (already up-to-date)")
                return {
                    'table': table,
                    'status': 'no_new_entries',
                    'entries_fetched': 0,
                    'entries_stored': 0,
                    'fetch_time': fetch_time,
                    'store_time': 0
                }
        
        except subprocess.TimeoutExpired:
            self.logger.error(f"  ❌ Command timed out after 300 seconds")
            return {
                'table': table,
                'status': 'timeout',
                'entries_fetched': 0,
                'fetch_time': 300,
                'error': 'Timeout'
            }
        
        except Exception as e:
            self.logger.error(f"  ❌ Error: {e}")
            return {
                'table': table,
                'status': 'error',
                'entries_fetched': 0,
                'fetch_time': time.time() - start_time,
                'error': str(e)
            }
    
    def run_once(self, tables: list) -> list:
        """
        Run one feed cycle for all tables.
        
        Args:
            tables: List of tables to feed (e.g., ['GSLIBTST.CUSTOMERS', ...])
            
        Returns:
            List of result dictionaries
        """
        self.logger.info(f"=" * 80)
        self.logger.info(f"Journal Feed Cycle: {len(tables)} tables")
        self.logger.info(f"=" * 80)
        
        results = []
        cycle_start = time.time()
        
        for table in tables:
            try:
                result = self.fetch_and_store(table)
                results.append(result)
            except Exception as e:
                self.logger.error(f"Error processing {table}: {e}")
                results.append({
                    'table': table,
                    'status': 'error',
                    'error': str(e)
                })
        
        cycle_time = time.time() - cycle_start
        
        # Summary
        self.logger.info(f"\n{'=' * 80}")
        self.logger.info(f"Feed Cycle Complete ({cycle_time:.1f}s)")
        self.logger.info(f"{'=' * 80}")
        
        total_fetched = sum(r.get('entries_fetched', 0) for r in results)
        total_stored = sum(r.get('entries_stored', 0) for r in results)
        errors = sum(1 for r in results if r.get('status') == 'error')
        
        self.logger.info(f"Tables processed: {len(tables)}")
        self.logger.info(f"Total entries fetched: {total_fetched}")
        self.logger.info(f"Total entries stored: {total_stored}")
        self.logger.info(f"Errors: {errors}")
        
        return results
    
    def run_continuous(self, tables: list):
        """
        Run continuous journal feed loop.
        
        Args:
            tables: List of tables to monitor
        """
        self.logger.info(f"{'=' * 80}")
        self.logger.info(f"CONTINUOUS JOURNAL FEEDER")
        self.logger.info(f"{'=' * 80}")
        self.logger.info(f"Tables: {len(tables)}")
        self.logger.info(f"Poll interval: {self.poll_interval} seconds ({self.poll_interval/60:.1f} min)")
        self.logger.info(f"Cache retention: {self.cache.retention_days} days")
        self.logger.info(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"{'=' * 80}")
        self.logger.info(f"Press Ctrl+C to stop\n")
        
        cycle_count = 0
        
        try:
            while True:
                cycle_count += 1
                self.logger.info(f"\n{'─' * 80}")
                self.logger.info(f"Cycle #{cycle_count} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                self.logger.info(f"{'─' * 80}")
                
                # Run feed cycle
                results = self.run_once(tables)
                
                # Check for errors
                errors = [r for r in results if r.get('status') == 'error']
                if errors:
                    self.logger.warning(f"⚠️  {len(errors)} table(s) had errors this cycle")
                    for err in errors:
                        self.logger.warning(f"  - {err['table']}: {err.get('error', 'Unknown')}")
                
                # Wait for next cycle
                self.logger.info(f"\n⏳ Next cycle in {self.poll_interval} seconds...")
                time.sleep(self.poll_interval)
        
        except KeyboardInterrupt:
            self.logger.info(f"\n\n{'=' * 80}")
            self.logger.info(f"⏹️  Journal feeder stopped after {cycle_count} cycles")
            self.logger.info(f"{'=' * 80}")
            sys.exit(0)


def main():
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Continuous Journal Feeder - Stream AS400 journal entries to SQLite cache",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run continuously (every 5 minutes)
  python3 -m lib.journal_feeder --tables GSLIBTST.CUSTOMERS GSLIBTST.ORDERS
  
  # Run with custom interval (every 1 minute)
  python3 -m lib.journal_feeder --tables GSLIBTST.CUSTOMERS --interval 60
  
  # Run once (for testing)
  python3 -m lib.journal_feeder --tables GSLIBTST.CUSTOMERS --once
  
  # Custom qadmcli path
  python3 -m lib.journal_feeder --tables GSLIBTST.CUSTOMERS --qadmcli /path/to/qadmcli.sh
        """
    )
    
    parser.add_argument(
        "--tables",
        nargs="+",
        required=True,
        help="Tables to monitor (format: LIBRARY.TABLE)"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Poll interval in seconds (default: 300 = 5 min)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (for testing)"
    )
    parser.add_argument(
        "--qadmcli",
        default="../qadmcli/qadmcli.sh",
        help="Path to qadmcli.sh (default: ../qadmcli/qadmcli.sh)"
    )
    parser.add_argument(
        "--cache-dir",
        default="cache",
        help="Cache directory (default: cache)"
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=7,
        help="Days to keep entries in cache (default: 7)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging"
    )
    
    args = parser.parse_args()
    
    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    # Create feeder
    feeder = JournalFeeder(
        qadmcli_path=args.qadmcli,
        cache_dir=args.cache_dir,
        poll_interval=args.interval,
        retention_days=args.retention_days
    )
    
    # Run
    if args.once:
        feeder.run_once(args.tables)
    else:
        feeder.run_continuous(args.tables)


if __name__ == "__main__":
    main()
