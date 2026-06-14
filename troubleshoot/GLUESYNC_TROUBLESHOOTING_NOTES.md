# Gluesync Troubleshooting Notes: Database Replication Truncation

## Issue Description
Replication from the DB2/AS400 source table `GSLIBTST.THAI_TEST` to the Microsoft SQL Server target tables (`dbo.THAI_Test` and `dbo.THAI_Test_vanila`) failed with the following error:
```
String or binary data would be truncated in table 'tempdb.dbo.#dbo_THAI_Test_tmp...', column 'STORE_ID'. Truncated value: ''.
```

---

## Root Cause Analysis

1. **Source Schema**: Two columns in `GSLIBTST.THAI_TEST` are defined with **CCSID 65535** (raw BIT DATA):
   - `STORE_ID` — `CHAR(10)` CCSID=65535
   - `RAW_DATA`  — `VARCHAR(50)` CCSID=65535
2. **Data Content**: Rows contain multi-byte EBCDIC byte sequences (e.g. `0xD7B4ED7CCB4040404040`).
3. **Data Type Translation & Expansion**:
   - GlueSync's JDBC driver reads `CHARACTER FOR BIT DATA` and maps it into Java `String` objects.
   - When bulk-inserting into SQL Server, the driver encodes the Java string back into bytes.
   - Multi-byte expansion (UTF-8 / UTF-16 re-encoding of non-ASCII bytes) inflates the byte length beyond the original column size, causing SQL Server to throw the truncation error.
4. **Target Schema Incompatibility**: The original target columns used fixed-length `BINARY(10)` / `BINARY(50)`, which are too small to hold the expanded byte payloads.

---

## Schema Compatibility Matrix

| Column | Source Type | CCSID | Target (before fix) | Target (after fix) | Status |
|--------|------------|-------|--------------------|--------------------|--------|
| `ID` | `INTEGER(4)` | — | `int` | `int` | ✅ OK |
| `FIRSTNAME_TH` | `VARCHAR(100)` | 838 (Thai) | `nvarchar(100)` | `nvarchar(100)` | ✅ OK |
| `LASTNAME_TH` | `VARCHAR(100)` | 838 (Thai) | `nvarchar(100)` | `nvarchar(100)` | ✅ OK |
| **`STORE_ID`** | **`CHAR(10)`** | **65535** | `binary(10)` | **`varbinary(400)`** | ⚠️ Fixed |
| `STORE_ID_EBCDIC` | `CHAR(10)` | 37 (EBCDIC) | `varchar(10)` | `varchar(10)` | ✅ OK |
| **`RAW_DATA`** | **`VARCHAR(50)`** | **65535** | `binary(50)` | **`varbinary(100)`** | ⚠️ Fixed |
| `CREATED_AT` | `TIMESTMP` | 37 | `datetime` | `datetime` | ✅ OK |
| `FULLNAME_TH` | `VARCHAR(200)` | 1208 (UTF-8) | `nvarchar(200)` | `nvarchar(200)` | ✅ OK |
| `Operation` | *(target only)* | — | `varchar(20)` | `varchar(20)` | ➕ UDF |
| `LastUpdate` | *(target only)* | — | `varchar(30)` | `varchar(30)` | ➕ UDF |

---

## Resolution: Option A (Applied)

Two CCSID=65535 columns were expanded in both target tables to accommodate multi-byte character encoding expansion:

### Migration Commands Executed:
```sql
-- STORE_ID: CHAR(10) BIT DATA -> VARBINARY(400)
-- Worst case: 10 source bytes × 4 bytes/char (EBCDIC→UTF-16 surrogate pairs) = 40 bytes;
-- 400 gives a 40x safety margin for any edge-case encoding path.
ALTER TABLE dbo.THAI_Test       ALTER COLUMN STORE_ID VARBINARY(400) NULL;
ALTER TABLE dbo.THAI_Test_vanila ALTER COLUMN STORE_ID VARBINARY(400) NULL;

-- RAW_DATA: VARCHAR(50) BIT DATA -> VARBINARY(100)
ALTER TABLE dbo.THAI_Test       ALTER COLUMN RAW_DATA VARBINARY(100) NULL;
ALTER TABLE dbo.THAI_Test_vanila ALTER COLUMN RAW_DATA VARBINARY(100) NULL;
```

These changes were applied via the [fix_mssql.py](file:///home/ubuntu/_qoder/replica-mon/troubleshoot/fix_mssql.py) utility and verified using the [check_schema_compatibility.py](file:///home/ubuntu/_qoder/replica-mon/troubleshoot/check_schema_compatibility.py) script.

## Diagnostic Improvement

Verbose truncation warnings were also enabled on the target database so future errors report the exact column and value (error 2628) instead of the generic error 8152:

```sql
ALTER DATABASE SCOPED CONFIGURATION SET VERBOSE_TRUNCATION_WARNINGS = ON;
```

> [!IMPORTANT]
> **Container Restart Required**: The GlueSync Core Hub engine caches target database schemas on startup. After running the migration script, you **must restart** the Core Hub container to clear its cache:
> ```bash
> podman restart gluesync_gluesync-core-hub_1
> ```
