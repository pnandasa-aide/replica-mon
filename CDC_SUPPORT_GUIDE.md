# Change Data Capture (CDC) Database Engine Support Guide

This guide details how different relational database engines support Change Data Capture (CDC), the specific terms and identifiers they use for tracking replication positions, and recommendations for appropriate column naming in multi-source monitoring dashboards.

---

## 1. CDC Terminology & Tracking Identifiers by Engine

| Database Engine | CDC Mechanism | Position/Sequence Identifier | Appropriate Column Name | How Position works |
| :--- | :--- | :--- | :--- | :--- |
| **AS/400 (Db2 for i)** | Journaling | **Journal Receiver Sequence Number** (`entry_number` / `seq`) | **Journal Sequence Number** (or **Journal Seq**) | Monotonically increasing 64-bit integer representing the offset in the journal receiver. |
| **Microsoft SQL Server** | Change Tracking (CT) / Change Data Capture (CDC) | **SYS_CHANGE_VERSION** (for CT) or **Log Sequence Number (LSN)** (for CDC) | **CT Version** / **LSN** | Monotonically increasing database-wide version number (CT) or binary LSN mapping to the transaction log (CDC). |
| **Oracle** | Oracle GoldenGate / LogMiner | **System Change Number (SCN)** | **Oracle SCN** | A database-wide logical timestamp that increases with every committed transaction. |
| **PostgreSQL** | Logical Replication (WAL) | **Log Sequence Number (LSN)** | **PG LSN** | A 64-bit integer representing a byte offset into the Write-Ahead Log stream (formatted as `X/Y`, e.g., `0/16A2F20`). |
| **MySQL / MariaDB** | Binary Log (Binlog) | **Binlog Filename + Position** or **GTID (Global Transaction Identifier)** | **Binlog Position** / **GTID** | Pair of filename and byte-offset (e.g., `mysql-bin.000124:4523`) or a UUID set representing transaction ranges (e.g., `3E11FA47-71CA-11E1-9E33-C80AA9429562:1-3`). |

---

## 2. Detailed Engine-Level CDC Mechanisms

### A. AS/400 (Db2 for i)
* **Mechanism**: System journaling (`STRJRNPF` command). When modifications occur, the OS writes journal entries containing the before/after images of rows to a Journal Receiver (`*JRNRCV`).
* **Position Tracking**: A sequence number (`Sequence Number` or `entry_number`) increments sequentially within the journal receivers. When journal receivers are rotated, the sequence numbers either continue or reset depending on config.
* **GlueSync Integration**: Reads journal entries via the database's native journal retrieval interfaces (API `QjoRetrieveJournalEntries` or system SQL tables) to feed targets.

### B. Microsoft SQL Server (MSSQL)
* **Change Tracking (CT)**: A lightweight synchronous tracking mechanism. It doesn't capture column value changes (only that a row changed), but it assigns a database-wide monotonically increasing `SYS_CHANGE_VERSION` to each change. Very fast, ideal for simple synchronization.
* **Change Data Capture (CDC)**: Asynchronously reads the SQL Server transaction log using SQL Server Agent. It captures full before-and-after column values and stores them in change tables.
* **Position Tracking**: Uses the **Log Sequence Number (LSN)**, a 10-byte structure consisting of `VLF Sequence Number : Offset : Slot`. LSNs are converted to hexadecimal strings (e.g. `00000024:00000b20:0001`) during replication.

### C. Oracle Database
* **Mechanism**: Redo Log mining using LogMiner or Oracle GoldenGate APIs. Transaction log files (Online Redo Logs and Archive Logs) are scanned asynchronously to capture changes.
* **Position Tracking**: Uses the **System Change Number (SCN)**. The SCN is Oracle's logical internal clock. Every commit increments the SCN. It is represented as a decimal number (e.g. `129847192`).

### D. PostgreSQL
* **Mechanism**: Logical Replication. A logical replication slot decodes Write-Ahead Log (WAL) records into logical changes using a logical decoding plugin (e.g. `pgoutput`).
* **Position Tracking**: Uses the **Log Sequence Number (LSN)**, which represents a pointer to a physical location inside the WAL. It is written as two hexadecimal numbers separated by a slash (e.g. `16H/A2F20`). When tracking replication, target status displays the *received* SCN/LSN vs the *flushed/applied* SCN/LSN.

### E. MySQL / MariaDB
* **Mechanism**: Binary Log (binlog) replication. Changes are logged to files containing SQL statements or row-based images.
* **Position Tracking**:
  1. **Classic Replication**: Identified by a combination of `File` (e.g., `binlog.000001`) and `Position` (integer byte offset).
  2. **GTID (Global Transaction Identifier)**: A unique identifier assigned to every transaction committed on the source server. It has the format `source_id:transaction_id` (e.g., `de305d54-75b4-4311-dec2-5a9b3d5c9012:1-103`).

---

## 3. Generic Dashboard Column Naming Recommendations

If you are designing a unified replication dashboard that supports multiple sources (not just AS400):
1. **Primary Column Name**: **`CDC Source Position`** (or **`Source Sequence ID`**)
   * *Why*: This generic term covers all sequence numbers, LSNs, SCNs, and binlog locations.
2. **Dynamic Format Adaptation**:
   * The dashboard should detect the source database type and render the value accordingly:
     * **AS400**: Display sequence number (e.g., `1,284,729`).
     * **MSSQL (CT)**: Display CT Version (e.g. `V: 3824`).
     * **MSSQL (CDC) / PostgreSQL**: Display LSN (e.g., `1A/4F2810` or `00000024:00000b20:0001`).
     * **Oracle**: Display SCN (e.g. `SCN: 948172948`).
     * **MySQL**: Display GTID or File:Pos (e.g. `mysql-bin.000012:940` / `GTID 1-45`).
