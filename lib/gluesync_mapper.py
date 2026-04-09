"""Get entity mapping from GlueSync API."""

import json
import subprocess
from typing import Optional


class GlueSyncMapper:
    """Get entity source/target mapping from GlueSync."""
    
    def __init__(self, gluesync_cli: str = "../gluesync-cli/gluesync_cli_v2.py"):
        self.gluesync_cli = gluesync_cli
    
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
        # Get entity details from gluesync-cli
        result = self._run_gluesync(
            "get", "entity", entity_id,
            "--pipeline", pipeline_id,
            "--output", "json"
        )
        
        if not result.get('success', True) and 'error' in result:
            raise RuntimeError(f"gluesync-cli failed: {result.get('error')}")
        
        # Parse entity configuration
        entity_data = result if 'source' not in result else result
        
        # Extract source and target from entity config
        # This is a simplified version - actual parsing depends on gluesync-cli output format
        source = entity_data.get('source', '')
        target = entity_data.get('target', '')
        pk_column = entity_data.get('pk_column', 'ID')
        
        if not source or not target:
            raise ValueError(f"Could not determine source/target for entity {entity_id}")
        
        return {
            'pipeline_id': pipeline_id,
            'entity_id': entity_id,
            'source': source,
            'target': target,
            'pk_column': pk_column,
            'raw_config': entity_data
        }
