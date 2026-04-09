# ReplicaMon - Replication Monitoring & Reconciliation

Monitor and verify data replication between AS400 (source) and MSSQL (target).

## Projects Integration

| Project | Purpose | This Project Uses |
|---------|---------|-------------------|
| **qadmcli** | Database management | AS400 journal reader |
| **gluesync-cli** | GlueSync lifecycle | Entity mapping API |
| **replica-mon** | Replication monitoring | Both above |

## Features

- Compare source (AS400 journal) vs target (MSSQL CT/CDC) changes
- Reconcile by primary key
- Report discrepancies
- Track replication lag

## Usage

```bash
# Compare changes since timestamp
./cli.py compare --pipeline 8aeb9fb6 --entity 98fd97b8 --since "2025-04-09 10:00:00"

# Reconcile specific PK
./cli.py reconcile --pipeline 8aeb9fb6 --entity 98fd97b8 --pk 12345
```
