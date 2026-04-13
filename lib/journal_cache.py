"""
Journal cache for AS400 entries.

Caches journal entries locally to avoid re-querying immutable historical data.
Only fetches new entries since last check.
"""

import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional


class JournalCache:
    """
    Local cache for AS400 journal entries.
    
    Stores entries by table and timestamp range.
    Only fetches new entries not already in cache.
    """
    
    def __init__(self, cache_dir: str = ".cache"):
        """
        Initialize journal cache.
        
        Args:
            cache_dir: Directory to store cache files
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_cache_path(self, table: str) -> Path:
        """Get cache file path for a table."""
        # Sanitize table name for filename
        safe_name = table.replace('.', '_').upper()
        return self.cache_dir / f"{safe_name}.json"
    
    def _get_metadata_path(self, table: str) -> Path:
        """Get metadata file path for a table."""
        safe_name = table.replace('.', '_').upper()
        return self.cache_dir / f"{safe_name}.meta.json"
    
    def load_cache(self, table: str) -> dict:
        """
        Load cached journal entries for a table.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            
        Returns:
            Dictionary with 'entries', 'last_timestamp', 'last_sequence'
        """
        cache_path = self._get_cache_path(table)
        meta_path = self._get_metadata_path(table)
        
        if not cache_path.exists():
            return {
                'entries': [],
                'last_timestamp': None,
                'last_sequence': 0,
                'cached_at': None
            }
        
        try:
            with open(cache_path, 'r') as f:
                entries = json.load(f)
            
            metadata = {}
            if meta_path.exists():
                with open(meta_path, 'r') as f:
                    metadata = json.load(f)
            
            return {
                'entries': entries,
                'last_timestamp': metadata.get('last_timestamp'),
                'last_sequence': metadata.get('last_sequence', 0),
                'cached_at': metadata.get('cached_at'),
                'entry_count': len(entries)
            }
        except Exception as e:
            print(f"  ⚠️  Warning: Could not load cache: {e}")
            return {
                'entries': [],
                'last_timestamp': None,
                'last_sequence': 0,
                'cached_at': None
            }
    
    def save_cache(self, table: str, entries: list, last_timestamp: str = None, last_sequence: int = 0):
        """
        Save journal entries to cache.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            entries: List of journal entry dictionaries
            last_timestamp: Latest entry timestamp
            last_sequence: Latest entry sequence number
        """
        cache_path = self._get_cache_path(table)
        meta_path = self._get_metadata_path(table)
        
        # Save entries
        with open(cache_path, 'w') as f:
            json.dump(entries, f, indent=2)
        
        # Save metadata
        metadata = {
            'table': table,
            'last_timestamp': last_timestamp,
            'last_sequence': last_sequence,
            'cached_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            'entry_count': len(entries)
        }
        
        with open(meta_path, 'w') as f:
            json.dump(metadata, f, indent=2)
    
    def append_entries(self, table: str, new_entries: list, last_timestamp: str = None, last_sequence: int = 0):
        """
        Append new entries to existing cache.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            new_entries: New journal entries to append
            last_timestamp: Latest entry timestamp
            last_sequence: Latest entry sequence number
        """
        cache = self.load_cache(table)
        existing_entries = cache['entries']
        
        # Merge and deduplicate by sequence number
        existing_seqs = {e.get('entry_number') for e in existing_entries}
        unique_new = [e for e in new_entries if e.get('entry_number') not in existing_seqs]
        
        all_entries = existing_entries + unique_new
        
        # Sort by sequence number
        all_entries.sort(key=lambda x: x.get('entry_number', 0))
        
        self.save_cache(table, all_entries, last_timestamp, last_sequence)
        
        return len(unique_new)
    
    def get_entries_since(self, table: str, since: str) -> list:
        """
        Get cached entries since a timestamp.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            since: Timestamp in format "YYYY-MM-DD HH:MM:SS"
            
        Returns:
            List of entries since the timestamp
        """
        cache = self.load_cache(table)
        entries = cache['entries']
        
        if not since:
            return entries
        
        # Filter by timestamp
        filtered = []
        for entry in entries:
            entry_ts = entry.get('entry_timestamp', '')
            if entry_ts and entry_ts >= since:
                filtered.append(entry)
        
        return filtered
    
    def get_summary_from_cache(self, table: str, since: str = None) -> dict:
        """
        Get journal summary from cached entries.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            since: Optional timestamp filter
            
        Returns:
            Summary dictionary matching qadmcli format
        """
        entries = self.get_entries_since(table, since) if since else self.load_cache(table)['entries']
        
        summary = {
            'table': table,
            'from_time': since,
            'total': 0,
            'inserts': 0,
            'updates': 0,
            'deletes': 0,
            'commits': 0,
            'other': 0,
            'entries': [],
            'from_cache': True
        }
        
        # Count by entry type
        type_counts = {}
        for entry in entries:
            entry_type = entry.get('entry_type', 'UNKNOWN')
            type_counts[entry_type] = type_counts.get(entry_type, 0) + 1
        
        # Categorize
        for entry_type, count in type_counts.items():
            entry_info = {'code': 'R', 'type': entry_type, 'count': count}
            
            if entry_type == 'PT':
                summary['inserts'] += count
                entry_info['operation'] = 'INSERT'
            elif entry_type in ('UP', 'UB'):
                summary['updates'] += count
                entry_info['operation'] = 'UPDATE'
            elif entry_type == 'DL':
                summary['deletes'] += count
                entry_info['operation'] = 'DELETE'
            elif entry_type == 'CG':
                summary['commits'] += count
                entry_info['operation'] = 'COMMIT'
            else:
                summary['other'] += count
                entry_info['operation'] = 'OTHER'
            
            summary['entries'].append(entry_info)
            summary['total'] += count
        
        return summary
    
    def clear_cache(self, table: str = None):
        """
        Clear cache for a table or all tables.
        
        Args:
            table: Optional table name. If None, clears all caches.
        """
        if table:
            cache_path = self._get_cache_path(table)
            meta_path = self._get_metadata_path(table)
            
            if cache_path.exists():
                cache_path.unlink()
            if meta_path.exists():
                meta_path.unlink()
        else:
            # Clear all caches
            for path in self.cache_dir.glob("*.json"):
                path.unlink()
    
    def get_cache_info(self, table: str) -> dict:
        """
        Get cache information.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            
        Returns:
            Dictionary with cache status
        """
        cache = self.load_cache(table)
        meta_path = self._get_metadata_path(table)
        
        cache_size = 0
        if meta_path.exists():
            cache_size = meta_path.stat().st_size + self._get_cache_path(table).stat().st_size
        
        return {
            'table': table,
            'cached': cache['cached_at'] is not None,
            'entry_count': cache.get('entry_count', 0),
            'last_timestamp': cache['last_timestamp'],
            'last_sequence': cache['last_sequence'],
            'cached_at': cache['cached_at'],
            'cache_size_bytes': cache_size,
            'cache_size_mb': round(cache_size / 1024 / 1024, 2)
        }
