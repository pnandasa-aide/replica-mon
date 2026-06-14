#!/usr/bin/env python3
"""Test script to check GlueSync entity status"""
import os
import json
from replica_msdk import GlueSyncClient

def main():
    print("="*70)
    print("GLUESYNC ENTITY STATUS TEST")
    print("="*70)
    
    # Connect to GlueSync
    print("\n1. Connecting to GlueSync...")
    try:
        client = GlueSyncClient(
            base_url='https://localhost:1717',
            username='admin',
            password='P@ssw0rd'
        )
        print("   ✅ Connected!")
    except Exception as e:
        print(f"   ❌ Connection failed: {e}")
        return
    
    # List pipelines
    print("\n2. Fetching pipelines...")
    pipelines = client.list_pipelines()
    print(f"   Found {len(pipelines)} pipeline(s)")
    
    if not pipelines:
        print("   No pipelines found!")
        return
    
    # Process each pipeline
    for pipeline in pipelines:
        pipeline_id = pipeline.get('id')
        pipeline_name = pipeline.get('name', 'N/A')
        
        print(f"\n{'='*70}")
        print(f"PIPELINE: {pipeline_name} ({pipeline_id})")
        print(f"{'='*70}")
        
        # Fetch entities
        print(f"\n3. Fetching entities...")
        try:
            entities = client.list_entities(pipeline_id)
            print(f"   Found {len(entities)} entity(ies)")
        except Exception as e:
            print(f"   ❌ Failed: {e}")
            continue
        
        # Display each entity
        for i, ent in enumerate(entities, 1):
            print(f"\n{'─'*70}")
            print(f"ENTITY {i}: {ent.get('entityName', 'N/A')}")
            print(f"{'─'*70}")
            
            # Basic info
            print(f"  Entity ID: {ent.get('entityId', 'N/A')}")
            print(f"  Status: '{ent.get('status', 'N/A')}'")
            
            # Agent entities
            agent_entities = ent.get('agentEntities', [])
            print(f"  Agent Entities: {len(agent_entities)}")
            
            for j, agent in enumerate(agent_entities, 1):
                agent_type = agent.get('agentType', 'N/A')
                table_info = agent.get('table', {})
                entity_type = agent.get('entityType', {})
                
                print(f"\n  Agent {j} ({agent_type}):")
                print(f"    Schema: {table_info.get('schema', 'N/A')}")
                print(f"    Table: {table_info.get('name', 'N/A')}")
                print(f"    EntityType: {json.dumps(entity_type, indent=6) if entity_type else 'N/A'}")
                
                # Look for snapshot-related fields
                print(f"    All keys: {list(agent.keys())}")
            
            # Check for other interesting fields
            print(f"\n  All entity keys: {list(ent.keys())}")
            
            # Check specific fields that might indicate snapshot status
            for field in ['lastStartedAt', 'lastSnapshotAt', 'snapshotStatus', 'startHistory', 'completedAt']:
                if field in ent:
                    print(f"  {field}: {ent[field]}")

if __name__ == '__main__':
    main()
