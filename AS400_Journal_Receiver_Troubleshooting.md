# AS400 Journal Receiver Troubleshooting Guide

## Problem Summary

Journal receiver `GSJNRC0002` in library `GSTESTLIB` accumulated **5.7 million entries**, causing severe performance degradation on journal queries. Attempts to delete the receiver after rollover consistently failed with `CPF0006` errors.

## Root Cause

Two previous `DLTJRNRCV` / `journal cleanup` commands were **Ctrl+C'd from the client**, but the corresponding AS400 server jobs (`QZDASOINIT`) remained alive in **MSGW (Message Wait)** status. These jobs were waiting for an operator reply to inquiry message `CPA7025` ("Receiver never fully saved"), and while waiting, they **held locks on the journal receiver**, preventing any subsequent delete attempts.

---

## Investigation Timeline

### Phase 1: Identify the Bloated Receiver

**Command:** List all receivers for a journal
```bash
./qadmcli.sh journal receivers -j GSTESTJNR -l GSTESTLIB
```

**Result:**
```
 Receiver   | Status   | Entries   | Size    | Cleanup Status
 GSJNRC0003 | ATTACHED | 48,643    | 0.04 MB | KEEP (Attached)
 GSJNRC0002 | ONLINE   | 5,785,712 | 1.13 MB | Safe to cleanup
```

> [!IMPORTANT]
> `GSJNRC0002` had 5.7M entries and was detached (ONLINE) but could not be deleted.

---

### Phase 2: Attempt Deletion — All Failed with CPF0006

**Attempt 1:** Direct delete with ignore-inquiry option
```bash
./qadmcli.sh sql execute -q "CALL QSYS2.QCMDEXC('DLTJRNRCV JRNRCV(GSTESTLIB/GSJNRC0002) DLTOPT(*IGNINQSG)', 56)"
```
Result: `CPF0006` — Errors occurred in command.

**Attempt 2:** Journal cleanup via qadmcli
```bash
./qadmcli.sh journal cleanup -j GSTESTJNR -l GSTESTLIB --keep 1
```
Result: Hung indefinitely (Ctrl+C'd).

**Attempt 3:** Enable auto-delete on the journal
```bash
./qadmcli.sh sql execute -q "CALL QSYS2.QCMDEXC('CHGJRN JRN(GSTESTLIB/GSTESTJNR) DLTRCV(*YES)')"
```
Result: `CPF70E3` — "Only attached receivers allowed in receiver directory." AS400 refuses to enable auto-delete while detached receivers exist.

> [!WARNING]
> `DLTOPT(*IGNINQSG)` may not be a valid parameter for `DLTJRNRCV` on all IBM i versions. If you get `CPF0006`, try the command **without** `DLTOPT` first to isolate whether the parameter itself is causing the error.

---

### Phase 3: Check Job Log for Detailed Errors

**Command:** View recent job log messages
```bash
./qadmcli.sh sql query -q "SELECT MESSAGE_ID, MESSAGE_TEXT FROM TABLE(QSYS2.JOBLOG_INFO('*')) ORDER BY ORDINAL_POSITION DESC FETCH FIRST 10 ROWS ONLY"
```

> [!NOTE]
> Each `qadmcli` invocation creates a **new JDBC session** (new AS400 job). The job log query only sees messages from the *current* session, not from previous ones. This means you cannot retrieve error details from a previous failed command unless you query that specific job's log (see Phase 5).

---

### Phase 4: Discover Available JOURNAL_INFO Columns

The column names in `QSYS2.JOURNAL_INFO` vary by IBM i version. Several attempts to query `DELETE_RECEIVER`, `MANAGE_RECEIVER`, and `RECEIVER_SIZE_OPTIONS` all failed with `SQL0206` (column not found).

**Command:** List all available columns
```bash
./qadmcli.sh sql query -q "SELECT COLUMN_NAME, DATA_TYPE FROM QSYS2.SYSCOLUMNS WHERE TABLE_SCHEMA = 'QSYS2' AND TABLE_NAME = 'JOURNAL_INFO' ORDER BY ORDINAL_POSITION"
```

**Actual column names discovered (65 columns total):**

| Expected Column Name | Actual Column Name on This System |
|----------------------|-----------------------------------|
| `MANAGE_RECEIVER` | `MANAGE_RECEIVER_OPTION` |
| `DELETE_RECEIVER` | `DELETE_RECEIVER_OPTION` |
| `RECEIVER_SIZE_OPTIONS` | `RECEIVER_MAXIMUM_SIZE` |

**Correct query for journal settings:**
```bash
./qadmcli.sh sql query -q "SELECT JOURNAL_NAME, MANAGE_RECEIVER_OPTION, DELETE_RECEIVER_OPTION, DELETE_RECEIVER_DELAY, RECEIVER_MAXIMUM_SIZE FROM QSYS2.JOURNAL_INFO WHERE JOURNAL_LIBRARY = 'GSTESTLIB' AND JOURNAL_NAME = 'GSTESTJNR'"
```

**Key columns explained:**

| Column | Meaning |
|--------|---------|
| `MANAGE_RECEIVER_OPTION` | `SYSTEM` = auto-rollover when size limit reached; `USER` = manual rollover |
| `DELETE_RECEIVER_OPTION` | `YES` = auto-delete old receivers; `NO` = manual delete required |
| `DELETE_RECEIVER_DELAY` | Days to wait before auto-deleting a detached receiver |
| `MANAGE_RECEIVER_DELAY` | Minutes to wait before auto-rollover |
| `RECEIVER_MAXIMUM_SIZE` | Size threshold triggering rollover (e.g., `MAXOPT2`, `MAXOPT3`) |
| `NUMBER_JOURNAL_RECEIVERS` | Total count of receivers in the chain |
| `TOTAL_SIZE_JOURNAL_RECEIVERS` | Combined size of all receivers (bytes) |

> [!TIP]
> If `SELECT *` from `JOURNAL_INFO` returns garbled output, it's because the table has 65+ columns and the terminal renderer can't fit them. Always query specific columns or use the `SYSCOLUMNS` approach to discover names first.

---

### Phase 5: Find the Lock Holders — QZDASOINIT Jobs

**Command:** List all active JDBC server jobs
```bash
./qadmcli.sh sql query -q "SELECT JOB_NAME, JOB_USER, JOB_STATUS FROM TABLE(QSYS2.ACTIVE_JOB_INFO()) WHERE JOB_NAME LIKE '%QZDASOINIT%'"
```

**Result:** 35 JDBC server jobs found, including **2 in MSGW status**.

> [!NOTE]
> `QSYS2.OBJECT_LOCK_INFO()` does **not exist** on this IBM i version. Use the `ACTIVE_JOB_INFO()` approach instead.

#### Job Status Codes Reference

| Status | Full Name | Meaning | Action Required? |
|--------|-----------|---------|------------------|
| **RUN** | Running | Actively executing | No — normal |
| **MSGW** | Message Wait | ⚠️ Stuck waiting for operator reply | **Yes — likely holding locks** |
| **LCKW** | Lock Wait | Waiting to acquire a lock | Maybe — check what it's waiting on |
| **TIMW** | Time Wait | Idle with timeout | No — normal pooled connection |
| **DEQW** | Dequeue Wait | Idle, waiting for work | No — normal pre-started job |
| **EVTW** | Event Wait | Waiting for event signal | No — normal |
| **PSRW** | Procedure Start Request Wait | Waiting for procedure dispatch | No — normal |

---

### Phase 6: Confirm the Root Cause — MSGW Job Log

**Command:** Check what message a specific MSGW job is waiting on
```bash
./qadmcli.sh sql query -q "SELECT MESSAGE_ID, MESSAGE_TEXT, MESSAGE_TYPE FROM TABLE(QSYS2.JOBLOG_INFO('160938/QUSER/QZDASOINIT')) ORDER BY ORDINAL_POSITION DESC FETCH FIRST 5 ROWS ONLY"
```

**Result:**
```
CPA7025 | Receiver GSJNRCV001 in GSTESTLIB never fully saved. (I C) | SENDER
```

**Explanation:** The job was executing a `DLTJRNRCV` command. The receiver had never been saved (backed up), so the system sent inquiry message `CPA7025` asking the operator to reply:
- **I** = Ignore (proceed with delete anyway)
- **C** = Cancel (abort the delete)

When the user Ctrl+C'd the client, the AS400 job stayed alive waiting for this reply, **holding a lock on the receiver**.

---

## Resolution Playbook

### Step 1: Kill Stuck MSGW Jobs

```bash
# End each MSGW job using the full qualified name: number/user/jobname
./qadmcli.sh sql execute -q "CALL QSYS2.QCMDEXC('ENDJOB JOB(160938/QUSER/QZDASOINIT) OPTION(*IMMED)')"
./qadmcli.sh sql execute -q "CALL QSYS2.QCMDEXC('ENDJOB JOB(161028/QUSER/QZDASOINIT) OPTION(*IMMED)')"
```

### Step 2: Verify Jobs Are Gone

```bash
./qadmcli.sh sql query -q "SELECT JOB_NAME, JOB_STATUS FROM TABLE(QSYS2.ACTIVE_JOB_INFO()) WHERE JOB_NAME LIKE '%QZDASOINIT%' AND JOB_STATUS = 'MSGW'"
```

Expected: 0 rows returned.

### Step 3: Delete the Receiver

```bash
# Try without DLTOPT first
./qadmcli.sh sql execute -q "CALL QSYS2.QCMDEXC('DLTJRNRCV JRNRCV(GSTESTLIB/GSJNRC0002)')"
```

If this hangs (new MSGW because receiver is unsaved), you have two options:

**Option A:** From another terminal, find the new MSGW job and reply to the inquiry:
```bash
./qadmcli.sh sql execute -q "CALL QSYS2.QCMDEXC('SNDBRKMSG MSG(''Reply I to CPA7025'') TOMSGQ(*REQUESTER)')"
```

**Option B:** Pre-set the job to auto-reply to inquiry messages, then delete:
```bash
# This must run in the SAME session as the delete — may require code changes to qadmcli
CHGJOB INQMSGRPY(*DFT)
DLTJRNRCV JRNRCV(GSTESTLIB/GSJNRC0002)
```

### Step 4: Verify Cleanup

```bash
./qadmcli.sh journal receivers -j GSTESTJNR -l GSTESTLIB
```

Expected: Only `GSJNRC0003` (ATTACHED) should remain.

### Step 5: Enable Auto-Delete (Optional)

Once all detached receivers are removed:
```bash
./qadmcli.sh sql execute -q "CALL QSYS2.QCMDEXC('CHGJRN JRN(GSTESTLIB/GSTESTJNR) DLTRCV(*YES)')"
```

Verify:
```bash
./qadmcli.sh sql query -q "SELECT JOURNAL_NAME, DELETE_RECEIVER_OPTION, DELETE_RECEIVER_DELAY FROM QSYS2.JOURNAL_INFO WHERE JOURNAL_LIBRARY = 'GSTESTLIB' AND JOURNAL_NAME = 'GSTESTJNR'"
```

---

## Quick Reference: Common CL Commands via QCMDEXC

| Task | Command |
|------|---------|
| **Rollover journal** | `CALL QSYS2.QCMDEXC('CHGJRN JRN(lib/jrn) JRNRCV(*GEN)')` |
| **Delete receiver** | `CALL QSYS2.QCMDEXC('DLTJRNRCV JRNRCV(lib/rcv)')` |
| **Enable auto-delete** | `CALL QSYS2.QCMDEXC('CHGJRN JRN(lib/jrn) DLTRCV(*YES)')` |
| **Enable auto-manage** | `CALL QSYS2.QCMDEXC('CHGJRN JRN(lib/jrn) MNGRCV(*SYSTEM)')` |
| **End a stuck job** | `CALL QSYS2.QCMDEXC('ENDJOB JOB(number/user/name) OPTION(*IMMED)')` |
| **List JDBC jobs** | `SELECT * FROM TABLE(QSYS2.ACTIVE_JOB_INFO()) WHERE JOB_NAME LIKE '%QZDASOINIT%'` |
| **Check job log** | `SELECT * FROM TABLE(QSYS2.JOBLOG_INFO('number/user/name')) ORDER BY ORDINAL_POSITION DESC FETCH FIRST 10 ROWS ONLY` |
| **Journal settings** | `SELECT * FROM QSYS2.JOURNAL_INFO WHERE JOURNAL_LIBRARY = 'lib' AND JOURNAL_NAME = 'jrn'` |
| **List columns** | `SELECT COLUMN_NAME FROM QSYS2.SYSCOLUMNS WHERE TABLE_SCHEMA = 'QSYS2' AND TABLE_NAME = 'table'` |

---

## Lessons Learned

> [!CAUTION]
> **Never Ctrl+C a `DLTJRNRCV` or `journal cleanup` command.** The AS400 job remains alive in MSGW status, holding locks that block all future delete attempts. If the command hangs, investigate and reply to the inquiry message instead.

> [!TIP]
> **Always check for MSGW jobs** before troubleshooting `CPF0006` errors on journal operations. Stuck MSGW jobs from previous sessions are a common hidden cause of lock conflicts.

> [!IMPORTANT]
> **Column names in QSYS2 views vary by IBM i version.** Always use `QSYS2.SYSCOLUMNS` to discover the actual column names before writing queries. Never assume documentation column names match your system.


------------------------------------------------------------------------------------------
PK Note:
ubuntu@apimdev2:~/_qoder/qadmcli$ ./qadmcli.sh journal receivers -j GSTESTJNR -l GSTESTLIB
[sudo] password for ubuntu: 
📦 Using existing image: qadmcli
🚀 Running: qadmcli journal receivers -j GSTESTJNR -l GSTESTLIB
[05/21/26 02:05:12] INFO     Connected to AS400: 161.82.146.249                                                                              connection.py:93
            Journal Receivers: GSTESTLIB.GSTESTJNR             
 Receiver   | Status   | Entries   | Size    | Cleanup Status  
------------+----------+-----------+---------+-----------------
 GSJNRC0003 | ATTACHED | 49,643    | 0.04 MB | KEEP (Attached) 
 GSJNRCV001 | ONLINE   | 20,535    | 0.01 MB | Safe to cleanup 
 GSJNRC0001 | ONLINE   | 480       | 0.00 MB | Safe to cleanup 
 GSJNRC0002 | ONLINE   | 5,785,712 | 1.13 MB | Safe to cleanup 
None

Summary: 4 receivers total (1 attached, 3 online, 0 other)
Tip: 3 receiver(s) can be saved and deleted to free space
[05/21/26 02:05:13] INFO     Disconnected from AS400  


---------------------------------------------------------------------------------------
How to find which tables are using a particular journal?
The easiest way via SQL is to query the system catalogs (if your OS version supports it). You can check QSYS2.SYSTABLES or QSYS2.SYSTABLESTAT:

bash
./qadmcli.sh sql query -q "SELECT TABLE_SCHEMA, TABLE_NAME FROM QSYS2.SYSTABLES WHERE FILE_TYPE = 'D'"
(Note: Not all DB2 versions expose the journal name in SYSTABLES. If they don't, you have to use the native AS400 green screen command: WRKJRNA JRN(GSTESTLIB/GSTESTJNR) and press F19 to display all journaled objects).

┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ TABLE_SCHEMA ┃ TABLE_NAME                ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩

---------------------------------------------------------------------------------------
Find the column of the table
./qadmcli.sh sql query -q "SELECT COLUMN_NAME, DATA_TYPE FROM QSYS2.SYSCOLUMNS WHERE TABLE_SCHEMA = 'QSYS2' AND TABLE_NAME = 'SYSTABLES' ORDER BY ORDINAL_POSITION"
       Query Results (32 rows)        
┏━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┓
┃ COLUMN_NAME            ┃ DATA_TYPE ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━┩
│ TABLE_NAME             │ VARCHAR   │
│ TABLE_OWNER            │ VARCHAR   │
│ TABLE_TYPE             │ CHAR      │
│ COLUMN_COUNT           │ INTEGER   │
│ ROW_LENGTH             │ INTEGER   │
│ TABLE_TEXT             │ VARG      │
│ LONG_COMMENT           │ VARG      │
│ TABLE_SCHEMA           │ VARCHAR   │
│ LAST_ALTERED_TIMESTAMP │ TIMESTMP  │
│ SYSTEM_TABLE_NAME      │ CHAR      │
│ SYSTEM_TABLE_SCHEMA    │ CHAR      │
│ FILE_TYPE              │ CHAR      │
│ BASE_TABLE_CATALOG     │ VARCHAR   │
│ BASE_TABLE_SCHEMA      │ VARCHAR   │
│ BASE_TABLE_NAME        │ VARCHAR   │
│ BASE_TABLE_MEMBER      │ VARCHAR   │
│ SYSTEM_TABLE           │ CHAR      │
│ SELECT_OMIT            │ CHAR      │
│ IS_INSERTABLE_INTO     │ VARCHAR   │
│ IASP_NUMBER            │ SMALLINT  │
│ ENABLED                │ VARCHAR   │
│ MAINTENANCE            │ VARCHAR   │
│ REFRESH                │ VARCHAR   │
│ REFRESH_TIME           │ TIMESTMP  │
│ MQT_DEFINITION         │ DBCLOB    │
│ ISOLATION              │ CHAR      │
│ PARTITION_TABLE        │ VARCHAR   │
│ TABLE_DEFINER          │ VARCHAR   │
│ MQT_RESTORE_DEFERRED   │ CHAR      │
│ ROUNDING_MODE          │ CHAR      │
│ CONTROL                │ CHAR      │
│ TEMPORAL_TYPE          │ CHAR      │
└────────────────────────┴───────────┘
---------------------------------------------------------------------------------------
ubuntu@apimdev2:~/_qoder/qadmcli$ ./qadmcli.sh sql query -q "SELECT COLUMN_NAME FROM QSYS2.SYSCOLUMNS WHERE TABLE_SCHEMA = 'QSYS2' AND TABLE_NAME = 'SYSPARTITIONSTAT' ORDER BY ORDINAL_POSITION"
📦 Using existing image: qadmcli
🚀 Running: qadmcli sql query -q SELECT COLUMN_NAME FROM QSYS2.SYSCOLUMNS WHERE TABLE_SCHEMA = 'QSYS2' AND TABLE_NAME = 'SYSPARTITIONSTAT' ORDER BY ORDINAL_POSITION
[05/21/26 03:30:10] INFO     Connected to AS400: 161.82.146.249                                                                              connection.py:93
      Query Results (67 rows)      
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ COLUMN_NAME                     ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ TABLE_SCHEMA                    │
│ TABLE_NAME                      │
│ TABLE_PARTITION                 │
│ PARTITION_TYPE                  │
│ PARTITION_NUMBER                │
│ NUMBER_DISTRIBUTED_PARTITIONS   │
│ NUMBER_ROWS                     │
│ NUMBER_ROW_PAGES                │
│ NUMBER_PAGES                    │
│ OVERFLOW                        │
│ CLUSTERED                       │
│ ACTIVE_BLOCKS                   │
│ AVGCOMPRESSEDROWSIZE            │
│ AVGROWCOMPRESSIONRATIO          │
│ AVGROWSIZE                      │
│ PCTROWSCOMPRESSED               │
│ PCTPAGESSAVED                   │
│ NUMBER_DELETED_ROWS             │
│ DATA_SIZE                       │
│ VARIABLE_LENGTH_SIZE            │
│ VARIABLE_LENGTH_SEGMENTS        │
│ FIXED_LENGTH_EXTENTS            │
│ VARIABLE_LENGTH_EXTENTS         │
│ COLUMN_STATS_SIZE               │
│ MAINTAINED_TEMPORARY_INDEX_SIZE │
│ NUMBER_DISTINCT_INDEXES         │
│ OPEN_OPERATIONS                 │
│ CLOSE_OPERATIONS                │
│ INSERT_OPERATIONS               │
│ BLOCKED_INSERT_OPERATIONS       │
│ BLOCKED_INSERT_ROWS             │
│ UPDATE_OPERATIONS               │
│ DELETE_OPERATIONS               │
│ CLEAR_OPERATIONS                │
│ COPY_OPERATIONS                 │
│ REORGANIZE_OPERATIONS           │
│ INDEX_BUILDS                    │
│ LOGICAL_READS                   │
│ PHYSICAL_READS                  │
│ SEQUENTIAL_READS                │
│ RANDOM_READS                    │
│ CREATE_TIMESTAMP                │
│ LAST_CHANGE_TIMESTAMP           │
│ LAST_SAVE_TIMESTAMP             │
│ LAST_RESTORE_TIMESTAMP          │
│ LAST_USED_TIMESTAMP             │
│ DAYS_USED_COUNT                 │
│ LAST_RESET_TIMESTAMP            │
│ NEXT_IDENTITY_VALUE             │
│ LOWINCLUSIVE                    │
│ LOWVALUE                        │
│ HIGHINCLUSIVE                   │
│ HIGHVALUE                       │
│ NUMBER_PARTITIONING_KEYS        │
│ PARTITIONING_KEYS               │
│ KEEP_IN_MEMORY                  │
│ MEDIA_PREFERENCE                │
│ LAST_SOURCE_UPDATE_TIMESTAMP    │
│ SOURCE_TYPE                     │
│ VOLATILE                        │
│ PARTITION_TEXT                  │
│ PARTIAL_TRANSACTION             │
│ APPLY_STARTING_RECEIVER_LIBRARY │
│ APPLY_STARTING_RECEIVER         │
│ SYSTEM_TABLE_SCHEMA             │
│ SYSTEM_TABLE_NAME               │
│ SYSTEM_TABLE_MEMBER             │
└─────────────────────────────────┘

--------------------------------------------------------------------------------------------
ubuntu@apimdev2:~/_qoder/qadmcli$ ./qadmcli.sh sql query -q "SELECT OBJECT_LIBRARY, OBJECT_NAME, OBJECT_TYPE, JOURNAL_LIBRARY, JOURNAL_NAME, JOURNAL_IMAGES FROM QSYS2.JOURNALED_OBJECTS WHERE JOURNAL_LIBRARY = 'GSTESTLIB' AND JOURNAL_NAME = 'GSTESTJNR'"
📦 Using existing image: qadmcli
🚀 Running: qadmcli sql query -q SELECT OBJECT_LIBRARY, OBJECT_NAME, OBJECT_TYPE, JOURNAL_LIBRARY, JOURNAL_NAME, JOURNAL_IMAGES FROM QSYS2.JOURNALED_OBJECTS WHERE JOURNAL_LIBRARY = 'GSTESTLIB' AND JOURNAL_NAME = 'GSTESTJNR'
[05/21/26 03:33:24] INFO     Connected to AS400: 161.82.146.249                                                                              connection.py:93
                                    Query Results (16 rows)                                     
┏━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ OBJECT_LIBRARY ┃ OBJECT_NAME ┃ OBJECT_TYPE ┃ JOURNAL_LIBRARY ┃ JOURNAL_NAME ┃ JOURNAL_IMAGES ┃
┡━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ GSTESTLIB      │ GSJNRCV001  │ *JRNRCV     │ GSTESTLIB       │ GSTESTJNR    │ NULL           │
│ GSTESTLIB      │ TB_NOPK     │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_CDC      │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_GRP_A    │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_GRP_B    │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_GRP_C    │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_PAUSE    │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_PERF     │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_RESIL    │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_RRN      │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_SCHED    │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_SCHEMA   │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ TB_SN00001  │ *FILE       │ GSTESTLIB       │ GSTESTJNR    │ *BOTH          │
│ GSTESTLIB      │ GSJNRC0001  │ *JRNRCV     │ GSTESTLIB       │ GSTESTJNR    │ NULL           │
│ GSTESTLIB      │ GSJNRC0002  │ *JRNRCV     │ GSTESTLIB       │ GSTESTJNR    │ NULL           │
│ GSTESTLIB      │ GSJNRC0003  │ *JRNRCV     │ GSTESTLIB       │ GSTESTJNR    │ NULL           │
└────────────────┴─────────────┴─────────────┴─────────────────┴──────────────┴────────────────┘
16 row(s) returned

--------------------------------------------------------------------------------------------
How to find locking processes: Locks are usually held by jobs either actively writing to the journal, reading from it (like GlueSync), or stuck in MSGW (Message Wait) from previous failed commands. 
📦 Using existing image: qadmcli
🚀 Running: qadmcli sql query -q SELECT JOB_NAME, JOB_USER, JOB_STATUS FROM TABLE(QSYS2.ACTIVE_JOB_INFO()) WHERE JOB_NAME LIKE '%QZDASOINIT%'
[05/21/26 03:37:22] INFO     Connected to AS400: 161.82.146.249                                                                              connection.py:93
              Query Results (33 rows)              
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ JOB_NAME                ┃ JOB_USER ┃ JOB_STATUS ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━┩
│ 065757/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 128287/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 129649/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 130135/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 130436/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 137976/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 137977/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 137984/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 137987/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 137989/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 137994/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 137998/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 138005/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 138007/QUSER/QZDASOINIT │ QUSER    │ DEQW       │
│ 147215/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161752/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161866/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161937/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161939/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161945/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161951/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161952/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161965/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161975/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161977/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161978/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161979/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161981/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161982/QUSER/QZDASOINIT │ QUSER    │ PSRW       │
│ 161983/QUSER/QZDASOINIT │ QUSER    │ RUN        │
│ 161984/QUSER/QZDASOINIT │ QUSER    │ TIMW       │
│ 161985/QUSER/QZDASOINIT │ QUSER    │ PSRW       │
│ 161986/QUSER/QZDASOINIT │ QUSER    │ PSRW       │
└─────────────────────────┴──────────┴────────────┘
33 row(s) returned

--------------------------------------------------------------------------------------------
Step 1 — Find the new MSGW job:

ubuntu@apimdev2:~/_qoder/qadmcli$ cd /home/ubuntu/_qoder/qadmcli && ./qadmcli.sh sql query -q "SELECT JOB_NAME, JOB_USER, JOB_STATUS FROM TABLE(QSYS2.ACTIVE_JOB_INFO()) WHERE JOB_NAME LIKE '%QZDASOINIT%' AND JOB_STATUS = 'MSGW'"
[sudo] password for ubuntu: 
📦 Using existing image: qadmcli
🚀 Running: ./qadmcli.sh sql query -q "SELECT JOB_NAME, JOB_USER, JOB_STATUS FROM TABLE(QSYS2.ACTIVE_JOB_INFO()) WHERE JOB_NAME LIKE '%QZDASOINIT%' AND JOB_STATUS = 'MSGW'"
[05/21/26 04:15:14] INFO     Connected to AS400: 161.82.146.249                                                                                             connection.py:93
              Query Results (1 rows)               
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ JOB_NAME                ┃ JOB_USER ┃ JOB_STATUS ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━┩
│ 161982/QUSER/QZDASOINIT │ QUSER    │ MSGW       │
└─────────────────────────┴──────────┴────────────┘
1 row(s) returned

Step 2 — End that MSGW job (replace XXXXXX with the job number you see):

./qadmcli.sh sql execute -q "CALL QSYS2.QCMDEXC('ENDJOB JOB(161982/QUSER/QZDASOINIT) OPTION(*IMMED)')"