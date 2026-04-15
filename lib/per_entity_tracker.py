#!/usr/bin/env python3
"""
Per-Entity Progress Tracker

Provides per-table progress tracking using SQLite journal cache.
Extracts table-specific statistics from the monolithic AS400 journal.
"""

import sqlite3
import os
from typing import Dict, List, Optional
from collections import defaultdict
from datetime import datetime


class PerEntityTracker:
    """Track per-entity (per-table) progress from SQLite journal cache."""
    
    def __init__(self, cache_dir: str = "cache"):
        """
        Initialize per-entity tracker.
        
        Args:
            cache_dir: Directory containing SQLite cache databases
        """
        self.cache_dir = cache_dir
        self.journal_db = os.path.join(cache_dir, "journal_cache.db")
        self.ct_db = os.path.join(cache_dir, "ct_cache.db")
    
    def get_all_entity_stats(self, library: str = None) -> List[Dict]:
        """
        Get statistics for all entities in the journal cache.
        
        Args:
            library: Filter by library (e.g., 'GSLIBTST'), None for all
            
        Returns:
            List of dictionaries with per-entity statistics
        """
        if not os.path.exists(self.journal_db):
            return []
        
        conn = sqlite3.connect(self.journal_db)
        conn.row_factory = sqlite3.Row
        
        try:
            query = """
                SELECT 
                    object_name as table_name,
                    object_library as library,
                    COUNT(*) as total_changes,
                    MIN(entry_number) as first_sequence,
                    MAX(entry_number) as last_sequence,
                    MIN(entry_timestamp) as first_change,
                    MAX(entry_timestamp) as last_change,
                    SUM(CASE WHEN entry_type = 'IR' THEN 1 ELSE 0 END) as inserts,
                    SUM(CASE WHEN entry_type = 'UP' THEN 1 ELSE 0 END) as updates,
                    SUM(CASE WHEN entry_type = 'DL' THEN 1 ELSE 0 END) as deletes
                FROM journal_entries
            """
            
            params = []
            if library:
                query += " WHERE library = ?"
                params.append(library)
            
            query += " GROUP BY object_library, object_name ORDER BY object_library, object_name"
            
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            
            return [dict(row) for row in rows]
        
        finally:
            conn.close()
    
    def get_entity_in_timerange(self, library: str, table_name: str, 
                                 start_time: str, end_time: str = None) -> Dict:
        """
        Get entity statistics for a specific time range.
        
        Args:
            library: AS400 library
            table_name: Table name
            start_time: Start timestamp (ISO format)
            end_time: End timestamp (ISO format), None for now
            
        Returns:
            Dictionary with entity statistics for the time range
        """
        if not os.path.exists(self.journal_db):
            return {}
        
        conn = sqlite3.connect(self.journal_db)
        conn.row_factory = sqlite3.Row
        
        try:
            query = """
                SELECT 
                    COUNT(*) as changes,
                    MIN(entry_number) as min_seq,
                    MAX(entry_number) as max_seq,
                    SUM(CASE WHEN entry_type = 'IR' THEN 1 ELSE 0 END) as inserts,
                    SUM(CASE WHEN entry_type = 'UP' THEN 1 ELSE 0 END) as updates,
                    SUM(CASE WHEN entry_type = 'DL' THEN 1 ELSE 0 END) as deletes
                FROM journal_entries
                WHERE object_library = ? AND object_name = ? AND entry_timestamp >= ?
            """
            
            params = [library, table_name, start_time]
            
            if end_time:
                query += " AND timestamp <= ?"
                params.append(end_time)
            
            cursor = conn.execute(query, params)
            row = cursor.fetchone()
            
            return dict(row) if row else {}
        
        finally:
            conn.close()
    
    def get_ct_entity_stats(self) -> List[Dict]:
        """
        Get statistics for all entities in the CT cache.
        
        Returns:
            List of dictionaries with per-entity CT statistics
        """
        if not os.path.exists(self.ct_db):
            return []
        
        conn = sqlite3.connect(self.ct_db)
        
        try:
            # Query ct_changes table grouped by table_name
            cursor = conn.execute("""
                SELECT 
                    table_name,
                    COUNT(*) as total_changes,
                    MIN(sys_change_version) as first_version,
                    MAX(sys_change_version) as last_version,
                    MAX(sys_change_timestamp) as last_change
                FROM ct_changes
                GROUP BY table_name
                ORDER BY table_name
            """)
            
            rows = cursor.fetchall()
            
            stats = []
            for row in rows:
                if row[1] > 0:  # total_changes > 0
                    stats.append({
                        'table_name': row[0],
                        'total_changes': row[1],
                        'first_version': row[2],
                        'last_version': row[3],
                        'last_change': row[4]
                    })
            
            return stats
        
        finally:
            conn.close()
    
    def compare_entity_progress(self, library: str) -> List[Dict]:
        """
        Compare AS400 journal vs MSSQL CT progress per entity.
        
        Args:
            library: AS400 library to compare
            
        Returns:
            List of comparison results per entity
        """
        # Get AS400 stats
        as400_stats = self.get_all_entity_stats(library)
        
        # Get MSSQL CT stats
        ct_stats = self.get_ct_entity_stats()
        
        # Build lookup for CT stats
        ct_lookup = {}
        for ct in ct_stats:
            # Extract table name (remove schema prefix for matching)
            table_parts = ct['table_name'].split('.')
            if len(table_parts) == 2:
                ct_lookup[table_parts[1]] = ct
            else:
                ct_lookup[ct['table_name']] = ct
        
        # Compare
        comparisons = []
        for as400 in as400_stats:
            table_name = as400['table_name']
            ct = ct_lookup.get(table_name)
            
            comparison = {
                'table_name': table_name,
                'library': library,
                'as400_total_changes': as400['total_changes'],
                'as400_first_seq': as400['first_sequence'],
                'as400_last_seq': as400['last_sequence'],
                'as400_last_change': as400['last_change'],
                'as400_inserts': as400['inserts'],
                'as400_updates': as400['updates'],
                'as400_deletes': as400['deletes'],
            }
            
            if ct:
                comparison['ct_total_changes'] = ct['total_changes']
                comparison['ct_first_version'] = ct['first_version']
                comparison['ct_last_version'] = ct['last_version']
                comparison['ct_last_change'] = ct['last_change']
                comparison['change_count_diff'] = as400['total_changes'] - ct['total_changes']
                
                # Determine status
                if comparison['change_count_diff'] == 0:
                    comparison['status'] = '✅ Current'
                elif comparison['change_count_diff'] > 0:
                    comparison['status'] = f"⚠️  Behind by {comparison['change_count_diff']}"
                else:
                    comparison['status'] = '✅ Current (CT ahead)'
            else:
                comparison['ct_total_changes'] = None
                comparison['ct_first_version'] = None
                comparison['ct_last_version'] = None
                comparison['ct_last_change'] = None
                comparison['change_count_diff'] = None
                comparison['status'] = '❌ No CT cache'
            
            comparisons.append(comparison)
        
        return comparisons
    
    def format_per_entity_report(self, library: str, time_window_start: str = None) -> str:
        """
        Format a human-readable per-entity progress report.
        
        Args:
            library: AS400 library
            time_window_start: Optional time window start for delta counting
            
        Returns:
            Formatted report string
        """
        comparisons = self.compare_entity_progress(library)
        
        if not comparisons:
            return "  ⚠️  No entity data available in cache"
        
        lines = []
        lines.append("")
        lines.append("Per-Entity Progress Report:")
        lines.append("=" * 100)
        
        if time_window_start:
            lines.append(f"Time Window: {time_window_start} → Now")
            lines.append("")
        
        # Header
        lines.append(f"{'Entity':<20} {'AS400 Changes':>14} {'AS400 Last Seq':>14} {'CT Changes':>12} {'CT Last Ver':>12} {'Diff':>6} {'Status':<20}")
        lines.append("-" * 100)
        
        for comp in comparisons:
            table = comp['table_name']
            as400_changes = comp['as400_total_changes'] or 0
            as400_last_seq = comp['as400_last_seq'] or 'N/A'
            ct_changes = comp.get('ct_total_changes')
            ct_last_ver = comp.get('ct_last_version')
            diff = comp.get('change_count_diff')
            status = comp['status']
            
            # Format values
            ct_changes_str = str(ct_changes) if ct_changes is not None else 'N/A'
            ct_last_ver_str = str(ct_last_ver) if ct_last_ver is not None else 'N/A'
            as400_last_seq_str = str(as400_last_seq) if as400_last_seq != 'N/A' else 'N/A'
            
            # Format diff
            if diff is not None:
                diff_str = f"{diff:+d}" if diff != 0 else "0"
            else:
                diff_str = "N/A"
            
            lines.append(
                f"{table:<20} {as400_changes:>14} {as400_last_seq_str:>14} {ct_changes_str:>12} {ct_last_ver_str:>12} {diff_str:>6} {status:<20}"
            )
        
        lines.append("-" * 100)
        lines.append(f"Total entities tracked: {len(comparisons)}")
        
        # Summary
        current_count = sum(1 for c in comparisons if 'Current' in c['status'])
        behind_count = sum(1 for c in comparisons if 'Behind' in c['status'])
        no_cache_count = sum(1 for c in comparisons if 'No CT cache' in c['status'])
        
        lines.append(f"  ✅ Current: {current_count}")
        if behind_count > 0:
            lines.append(f"  ⚠️  Behind: {behind_count}")
        if no_cache_count > 0:
            lines.append(f"  ❌ No CT cache: {no_cache_count}")
        
        return "\n".join(lines)
    
    def get_entity_gaps(self, library: str, table_name: str) -> Dict:
        """
        Identify potential gaps in replication for a specific entity.
        
        Args:
            library: AS400 library
            table_name: Table name
            
        Returns:
            Dictionary with gap analysis
        """
        comparisons = self.compare_entity_progress(library)
        
        # Find this table
        comp = None
        for c in comparisons:
            if c['table_name'] == table_name:
                comp = c
                break
        
        if not comp:
            return {'error': f'Table {table_name} not found'}
        
        gap_analysis = {
            'table_name': table_name,
            'library': library,
            'has_gap': False,
            'gap_details': {}
        }
        
        # Check if there's a discrepancy
        if comp.get('change_count_diff') is not None and comp['change_count_diff'] > 0:
            gap_analysis['has_gap'] = True
            gap_analysis['gap_details'] = {
                'missing_changes': comp['change_count_diff'],
                'as400_total': comp['as400_total_changes'],
                'ct_total': comp['ct_total_changes'],
                'as400_seq_range': f"{comp['as400_first_seq']} → {comp['as400_last_seq']}",
                'ct_version_range': f"{comp['ct_first_version']} → {comp['ct_last_version']}",
            }
        
        return gap_analysis


# Convenience function for quick usage
def show_per_entity_report(library: str = None, cache_dir: str = "cache", 
                           time_window_start: str = None):
    """
    Show per-entity progress report.
    
    Args:
        library: AS400 library (None for all)
        cache_dir: Cache directory
        time_window_start: Optional time window start
    """
    tracker = PerEntityTracker(cache_dir)
    print(tracker.format_per_entity_report(library, time_window_start))


if __name__ == "__main__":
    # Demo usage
    show_per_entity_report(library="GSLIBTST")
