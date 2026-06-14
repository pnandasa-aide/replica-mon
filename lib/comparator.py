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

    def compare_with_delay_window(
        self,
        source_entries: list[dict],
        target_changes: list[dict],
        pk_column: str = "ID",
        delay_seconds: float = 30.0,
        as400_tz_offset: int = 0,
        mssql_tz_offset: int = 7
    ) -> dict:
        """
        Compare source vs target using PK-level matching within a sliding delay window.
        
        Args:
            source_entries: List of AS400 journal entry dictionaries
            target_changes: List of MSSQL Change Tracking dictionaries
            pk_column: Name of primary key column to match on
            delay_seconds: Maximum allowed replication lag window in seconds
            as400_tz_offset: AS400 timezone offset (default 0)
            mssql_tz_offset: MSSQL timezone offset (default 7)
            
        Returns:
            Dictionary containing verification statistics and discrepancy details
        """
        import json
        from datetime import datetime, timedelta
        from .timezone import convert_timestamp

        def get_pk_value(entry: dict, pk_col: str, entry_type: str) -> Optional[str]:
            images = []
            if entry_type == 'DL':
                images = [entry.get('before_image')]
            elif entry_type == 'PT':
                images = [entry.get('after_image')]
            else:
                images = [entry.get('after_image'), entry.get('before_image')]
            
            for img in images:
                if not img:
                    continue
                if isinstance(img, dict):
                    for k, v in img.items():
                        if k.upper() == pk_col.upper():
                            return str(v)
                elif isinstance(img, str) and not img.startswith('@@@'):
                    try:
                        d = json.loads(img)
                        if isinstance(d, dict):
                            for k, v in d.items():
                                if k.upper() == pk_col.upper():
                                    return str(v)
                    except Exception:
                        pass
            return None

        def get_target_pk_value(change: dict, pk_col: str) -> Optional[str]:
            pk_val = change.get('PRIMARY_KEY_VALUES')
            if not pk_val:
                return None
            if isinstance(pk_val, dict):
                for k, v in pk_val.items():
                    if k.upper() == pk_col.upper():
                        return str(v)
            return str(pk_val)

        def map_as400_op(op_type: str) -> str:
            if op_type == 'PT':
                return 'I'
            elif op_type in ('UP', 'UB'):
                return 'U'
            elif op_type == 'DL':
                return 'D'
            return op_type

        # Index target changes by PK + operation for fast lookup
        target_by_pk_op = {}
        for tc in target_changes:
            pk = get_target_pk_value(tc, pk_column)
            op = tc.get('sys_change_operation', '')
            if pk:
                key = (pk, op)
                target_by_pk_op.setdefault(key, []).append(tc)

        verified = []
        in_flight = []
        discrepancies = []

        now_local = datetime.now()

        for se in source_entries:
            op_type = se.get('entry_type', '')
            mapped_op = map_as400_op(op_type)
            pk = get_pk_value(se, pk_column, op_type)

            if not pk:
                discrepancies.append({
                    'entry': se,
                    'reason': f"Could not extract PK '{pk_column}' from record images"
                })
                continue

            # Convert entry timestamp from AS400 timezone to MSSQL/local timezone
            src_ts_raw = se.get('entry_timestamp', '')
            if '.' in src_ts_raw:
                src_ts_raw = src_ts_raw.split('.')[0]
            src_ts_local_str = convert_timestamp(src_ts_raw, as400_tz_offset, mssql_tz_offset)
            
            try:
                t_src = datetime.strptime(src_ts_local_str, "%Y-%m-%d %H:%M:%S")
            except Exception:
                discrepancies.append({
                    'entry': se,
                    'reason': f"Invalid entry timestamp format: {se.get('entry_timestamp')}"
                })
                continue

            matched = False
            candidates = target_by_pk_op.get((pk, mapped_op), [])
            for tc in candidates:
                tc_ts_raw = tc.get('sys_change_timestamp', '')
                if tc_ts_raw and '.' in tc_ts_raw:
                    tc_ts_raw = tc_ts_raw.split('.')[0]
                
                try:
                    t_tgt = datetime.strptime(tc_ts_raw, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue

                # Is target change timestamp within delay window after source change?
                # Using a sliding window from T_source to T_source + delay_seconds
                if t_src <= t_tgt <= (t_src + timedelta(seconds=delay_seconds)):
                    matched = True
                    break

            if matched:
                verified.append(se)
            else:
                # If the transaction is younger than the delay window, it might still be in transit (in-flight)
                if (now_local - t_src).total_seconds() <= delay_seconds:
                    in_flight.append(se)
                else:
                    discrepancies.append({
                        'entry': se,
                        'reason': f"No matching MSSQL CT change found for PK {pk} ({mapped_op}) within {delay_seconds}s delay window"
                    })

        return {
            'total_source': len(source_entries),
            'verified': len(verified),
            'in_flight': len(in_flight),
            'discrepancies_count': len(discrepancies),
            'discrepancies': discrepancies
        }
