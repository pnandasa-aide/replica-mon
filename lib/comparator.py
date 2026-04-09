"""Compare source vs target changes."""

from typing import Any, Optional


class ChangeComparator:
    """Compare changes between source and target."""
    
    def compare(self, source_changes: dict, target_changes: dict) -> dict:
        """
        Compare source and target change counts.
        
        Args:
            source_changes: Dict with 'total', 'inserts', 'updates', 'deletes'
            target_changes: Dict with 'total', 'inserts', 'updates', 'deletes'
            
        Returns:
            Comparison result with discrepancies
        """
        discrepancies = []
        
        # Compare totals
        source_total = source_changes.get('total', 0)
        target_total = target_changes.get('total', 0)
        
        if source_total != target_total:
            discrepancies.append(
                f"Total count mismatch: source={source_total}, target={target_total}"
            )
        
        # Compare by operation type
        for op in ['inserts', 'updates', 'deletes']:
            source_count = source_changes.get(op, 0)
            target_count = target_changes.get(op, 0)
            
            if source_count != target_count:
                discrepancies.append(
                    f"{op.capitalize()} count mismatch: source={source_count}, target={target_count}"
                )
        
        return {
            'difference': source_total - target_total,
            'discrepancies': discrepancies,
            'match': len(discrepancies) == 0
        }
    
    def compare_records(self, source_record: dict, target_record: dict) -> list[str]:
        """
        Compare two records field by field.
        
        Args:
            source_record: Source record dict
            target_record: Target record dict
            
        Returns:
            List of field differences
        """
        differences = []
        
        # Get all unique keys
        all_keys = set(source_record.keys()) | set(target_record.keys())
        
        for key in all_keys:
            source_val = source_record.get(key)
            target_val = target_record.get(key)
            
            # Skip None/null comparisons
            if source_val is None and target_val is None:
                continue
            
            if source_val != target_val:
                differences.append(
                    f"{key}: source={source_val!r}, target={target_val!r}"
                )
        
        return differences
    
    def find_missing_pks(self, source_pks: set, target_pks: set) -> dict:
        """
        Find primary keys that exist in one system but not the other.
        
        Args:
            source_pks: Set of PKs in source
            target_pks: Set of PKs in target
            
        Returns:
            Dict with 'missing_in_target' and 'missing_in_source'
        """
        return {
            'missing_in_target': list(source_pks - target_pks),
            'missing_in_source': list(target_pks - source_pks),
            'common': list(source_pks & target_pks)
        }
