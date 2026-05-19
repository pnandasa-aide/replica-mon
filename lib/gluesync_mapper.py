"""Get entity mapping from GlueSync API."""

import json
import subprocess
from typing import Optional


class GlueSyncMapper:
    """Get entity source/target mapping from GlueSync."""
    
    def __init__(self, gluesync_cli: Optional[str] = None):
        import os
        self.gluesync_cli = gluesync_cli or os.environ.get("REPLICA_CLI_PATH") or os.environ.get("GLUESYNC_CLI_PATH") or "../replica-cli/gluesync_cli_v2.py"
    
    def _run_gluesync(self, *args) -> dict:
        """Run gluesync-cli command and return parsed output."""
        cmd = ["python3", self.gluesync_cli] + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {'output': result.stdout, 'success': True}
        except subprocess.CalledProcessError as e:
            return {'error': e.stderr or e.stdout, 'success': False}
    
    def get_entity_mapping(self, pipeline_id: str, entity_id: str) -> dict:
        """
        Get source and target table mapping for an entity.
        
        Args:
            pipeline_id: GlueSync pipeline ID
            entity_id: Entity ID
            
        Returns:
            Dict with 'source', 'target', 'pk_column', etc.
        """
        # Get entity details from gluesync-cli (with --output json at the top-level)
        result = self._run_gluesync(
            "--output", "json",
            "get", "entity", entity_id,
            "--pipeline", pipeline_id
        )
        
        if isinstance(result, dict) and not result.get('success', True) and 'error' in result:
            raise RuntimeError(f"gluesync-cli failed: {result.get('error')}")
        
        # Parse entity configuration
        entity_data = result
        
        # Extract source and target from entity config
        source = entity_data.get('source', '')
        target = entity_data.get('target', '')
        pk_column = entity_data.get('pk_column', '')
        
        # Fallback to parsing the raw GlueSync API entity payload
        if not source or not target:
            agent_entities = entity_data.get('agentEntities', [])
            for ae in agent_entities:
                ae_type = ae.get('entityType', {}).get('type')
                table = ae.get('table', {})
                schema = table.get('schema', '')
                name = table.get('name', '')
                full_table = f"{schema}.{name}" if schema else name
                
                if ae_type == 'Source':
                    source = full_table
                elif ae_type == 'Target':
                    target = full_table
                    # Try to extract the first primary key column name
                    keys = ae.get('keys', [])
                    if keys:
                        pk_column = keys[0].get('name', '')
                        
        if not pk_column:
            pk_column = 'ID'
            
        if not source or not target:
            raise ValueError(f"Could not determine source/target for entity {entity_id}. Raw config: {entity_data}")
        
        return {
            'pipeline_id': pipeline_id,
            'entity_id': entity_id,
            'source': source,
            'target': target,
            'pk_column': pk_column,
            'raw_config': entity_data
        }
