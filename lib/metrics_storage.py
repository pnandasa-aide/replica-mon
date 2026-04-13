#!/usr/bin/env python3
"""
Simple file-based metrics storage for replica-mon.

Stores monitoring results as CSV files for time-series analysis.
Can be imported into Excel, Grafana, or any analytics tool.
"""

import csv
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional


class MetricsFileStorage:
    """
    Store monitoring metrics as CSV files.
    
    Creates one CSV file per day for easy management.
    """
    
    def __init__(self, metrics_dir: str = "metrics"):
        """
        Initialize metrics storage.
        
        Args:
            metrics_dir: Directory to store metric files
        """
        self.metrics_dir = Path(metrics_dir)
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_today_filename(self) -> str:
        """Get filename for today's metrics."""
        return f"metrics_{datetime.now().strftime('%Y-%m-%d')}.csv"
    
    def _get_filepath(self, date: Optional[str] = None) -> Path:
        """Get file path for a specific date."""
        if date:
            return self.metrics_dir / f"metrics_{date}.csv"
        return self.metrics_dir / self._get_today_filename()
    
    def save_metrics(self, results: List[Dict], timestamp: Optional[datetime] = None):
        """
        Save monitoring results to CSV.
        
        Args:
            results: List of monitoring result dictionaries
            timestamp: Optional timestamp (defaults to now)
        """
        timestamp = timestamp or datetime.now()
        filepath = self._get_filepath()
        
        # Define CSV columns
        fieldnames = [
            'timestamp',
            'source_table',
            'target_table',
            'status',
            'journal_inserts',
            'journal_updates',
            'journal_deletes',
            'journal_total',
            'ct_inserts',
            'ct_updates',
            'ct_deletes',
            'ct_total',
            'replication_lag',
            'cache_status',
            'monitoring_interval_sec'
        ]
        
        # Check if file exists (to write header or not)
        file_exists = filepath.exists()
        
        # Append to CSV
        with open(filepath, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            
            # Write header if new file
            if not file_exists:
                writer.writeheader()
            
            # Write each result
            for result in results:
                writer.writerow({
                    'timestamp': timestamp.isoformat(),
                    'source_table': result.get('source', result.get('source_table', '')),
                    'target_table': result.get('target', result.get('target_table', '')),
                    'status': result.get('status', 'UNKNOWN'),
                    'journal_inserts': result.get('journal_inserts', 0),
                    'journal_updates': result.get('journal_updates', 0),
                    'journal_deletes': result.get('journal_deletes', 0),
                    'journal_total': result.get('journal_total', 0),
                    'ct_inserts': result.get('ct_inserts', 0),
                    'ct_updates': result.get('ct_updates', 0),
                    'ct_deletes': result.get('ct_deletes', 0),
                    'ct_total': result.get('ct_total', 0),
                    'replication_lag': result.get('journal_total', 0) - result.get('ct_total', 0),
                    'cache_status': result.get('cache_status', 'unknown'),
                    'monitoring_interval_sec': result.get('interval', 300)
                })
        
        print(f"  ✓ Metrics saved to {filepath.name}")
    
    def get_time_series(self, table: str, hours: int = 24) -> List[Dict]:
        """
        Get time-series data for a table.
        
        Args:
            table: Table name (e.g., 'GSLIBTST.CUSTOMERS')
            hours: How many hours back to retrieve
            
        Returns:
            List of metric dictionaries
        """
        from datetime import timedelta
        
        cutoff = datetime.now() - timedelta(hours=hours)
        metrics = []
        
        # Read today's file and yesterday's file
        for days_back in [0, 1]:
            date = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - 
                   timedelta(days=days_back)).strftime('%Y-%m-%d')
            filepath = self._get_filepath(date)
            
            if not filepath.exists():
                continue
            
            with open(filepath, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Filter by table and time
                    if row['source_table'] == table:
                        row_time = datetime.fromisoformat(row['timestamp'])
                        if row_time >= cutoff:
                            metrics.append(row)
        
        return metrics
    
    def export_to_json(self, output_file: Optional[str] = None) -> str:
        """
        Export all metrics to JSON file.
        
        Args:
            output_file: Output filename (defaults to metrics.json)
            
        Returns:
            Path to exported file
        """
        if output_file is None:
            output_file = str(self.metrics_dir / "metrics.json")
        
        all_metrics = []
        
        # Read all CSV files
        for csv_file in sorted(self.metrics_dir.glob("metrics_*.csv")):
            with open(csv_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    all_metrics.append(row)
        
        # Write JSON
        with open(output_file, 'w') as f:
            json.dump(all_metrics, f, indent=2)
        
        print(f"✓ Exported {len(all_metrics)} metrics to {output_file}")
        return output_file
    
    def get_summary_stats(self, table: str, hours: int = 24) -> Dict:
        """
        Get summary statistics for a table.
        
        Args:
            table: Table name
            hours: Time window
            
        Returns:
            Summary statistics dictionary
        """
        metrics = self.get_time_series(table, hours)
        
        if not metrics:
            return {'error': 'No data found'}
        
        total_journal = sum(int(m['journal_total']) for m in metrics)
        total_ct = sum(int(m['ct_total']) for m in metrics)
        avg_lag = sum(int(m['replication_lag']) for m in metrics) / len(metrics)
        
        return {
            'table': table,
            'time_window_hours': hours,
            'data_points': len(metrics),
            'total_journal_changes': total_journal,
            'total_ct_changes': total_ct,
            'avg_replication_lag': round(avg_lag, 2),
            'max_lag': max(int(m['replication_lag']) for m in metrics),
            'min_lag': min(int(m['replication_lag']) for m in metrics)
        }


class FullJournalStorage:
    """
    Store full journal entries for detailed investigation.
    
    Used when tables are flagged with requires_attention=True.
    Stores complete journal entry details for root cause analysis.
    """
    
    def __init__(self, journal_dir: str = "metrics/journals"):
        """
        Initialize full journal storage.
        
        Args:
            journal_dir: Directory to store journal files
        """
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_journal_path(self, table: str, date: str) -> Path:
        """Get journal file path."""
        safe_name = table.replace('.', '_').upper()
        return self.journal_dir / f"{safe_name}_{date}.json"
    
    def save_journal_entries(self, table: str, entries: List[Dict], date: Optional[str] = None):
        """
        Save full journal entries to file.
        
        Args:
            table: Table name
            entries: List of journal entry dictionaries
            date: Date string (YYYY-MM-DD), defaults to today
        """
        date = date or datetime.now().strftime('%Y-%m-%d')
        filepath = self._get_journal_path(table, date)
        
        # Load existing entries if file exists
        existing_entries = []
        if filepath.exists():
            try:
                with open(filepath, 'r') as f:
                    existing_entries = json.load(f)
            except:
                pass
        
        # Merge and deduplicate by sequence number
        existing_seqs = {e.get('entry_number') for e in existing_entries}
        new_entries = [e for e in entries if e.get('entry_number') not in existing_seqs]
        all_entries = existing_entries + new_entries
        
        # Sort by sequence number
        all_entries.sort(key=lambda x: x.get('entry_number', 0))
        
        # Save
        with open(filepath, 'w') as f:
            json.dump(all_entries, f, indent=2)
        
        print(f"  ✓ Saved {len(new_entries)} new journal entries to {filepath.name}")
        print(f"    Total entries: {len(all_entries)}")
    
    def load_journal_entries(self, table: str, date: Optional[str] = None) -> List[Dict]:
        """
        Load journal entries from file.
        
        Args:
            table: Table name
            date: Date string, defaults to today
            
        Returns:
            List of journal entries
        """
        date = date or datetime.now().strftime('%Y-%m-%d')
        filepath = self._get_journal_path(table, date)
        
        if not filepath.exists():
            return []
        
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except:
            return []
    
    def get_journal_size(self, table: str, date: Optional[str] = None) -> Dict:
        """
        Get journal file size information.
        
        Args:
            table: Table name
            date: Date string
            
        Returns:
            Size information dictionary
        """
        date = date or datetime.now().strftime('%Y-%m-%d')
        filepath = self._get_journal_path(table, date)
        
        if not filepath.exists():
            return {'exists': False, 'size_bytes': 0, 'entry_count': 0}
        
        size_bytes = filepath.stat().st_size
        
        # Count entries
        entries = self.load_journal_entries(table, date)
        
        return {
            'exists': True,
            'size_bytes': size_bytes,
            'size_mb': round(size_bytes / 1024 / 1024, 2),
            'entry_count': len(entries),
            'file': str(filepath)
        }


# Convenience functions
def save_monitoring_metrics(results: List[Dict]):
    """Quick function to save monitoring results."""
    storage = MetricsFileStorage()
    storage.save_metrics(results)


def get_table_metrics(table: str, hours: int = 24) -> List[Dict]:
    """Quick function to get time-series for a table."""
    storage = MetricsFileStorage()
    return storage.get_time_series(table, hours)


def get_table_summary(table: str, hours: int = 24) -> Dict:
    """Quick function to get summary stats."""
    storage = MetricsFileStorage()
    return storage.get_summary_stats(table, hours)


if __name__ == "__main__":
    # Test the storage
    print("Testing MetricsFileStorage...")
    
    storage = MetricsFileStorage()
    
    # Test data
    test_results = [
        {
            'source': 'GSLIBTST.CUSTOMERS',
            'target': 'dbo.CUSTOMERS',
            'status': '✅ OK',
            'journal_total': 34559,
            'journal_inserts': 25433,
            'journal_updates': 6826,
            'journal_deletes': 1175,
            'ct_total': 34559,
            'ct_inserts': 25433,
            'ct_updates': 6826,
            'ct_deletes': 1175,
            'cache_status': 'hit'
        }
    ]
    
    # Save metrics
    storage.save_metrics(test_results)
    
    # Get summary
    summary = storage.get_summary_stats('GSLIBTST.CUSTOMERS', hours=1)
    print(f"\nSummary: {json.dumps(summary, indent=2)}")
