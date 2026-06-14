#!/usr/bin/env python3
"""
Schema compatibility check: AS400 GSLIBTST.THAI_TEST  vs  MSSQL dbo.THAI_Test
Run inside the replica-mon container:
  podman exec -i replica-mon python /app/replica-mon/troubleshoot/check_schema_compatibility.py
"""
import os
import jaydebeapi
import pyodbc

SEP = "=" * 88

# ── Credentials ───────────────────────────────────────────────────────────────
AS400_HOST = os.getenv("AS400_HOST", "161.82.146.249")
AS400_USER = os.getenv("AS400_USER", "user001")
AS400_PASS = os.getenv("AS400_PASSWORD", "pwdN03xpr")
JT400_JAR  = "/opt/jt400/jt400.jar"

MSSQL_HOST = os.getenv("MSSQL_HOST", "192.168.13.62")
MSSQL_DB   = os.getenv("MSSQL_DATABASE", "GSTargetDB")
MSSQL_USER = os.getenv("MSSQL_USER", "gstgdblogin")
MSSQL_PASS = os.getenv("MSSQL_PASSWORD", "tar53t@dm1n")

# ── AS400 Source ───────────────────────────────────────────────────────────────
print(SEP)
print("SOURCE: AS400  GSLIBTST.THAI_TEST  column definitions")
print(SEP)

as400_url = (
    "jdbc:as400://" + AS400_HOST + "/GSLIBTST"
    ";naming=system;errors=full;date format=iso;time format=iso"
)
aconn = jaydebeapi.connect(
    "com.ibm.as400.access.AS400JDBCDriver",
    as400_url, [AS400_USER, AS400_PASS],
    JT400_JAR
)
acur = aconn.cursor()
acur.execute("""
    SELECT COLUMN_NAME, DATA_TYPE, LENGTH,
           NUMERIC_PRECISION, NUMERIC_SCALE,
           IS_NULLABLE, CCSID
    FROM QSYS2.SYSCOLUMNS
    WHERE TABLE_SCHEMA = 'GSLIBTST'
      AND TABLE_NAME   = 'THAI_TEST'
    ORDER BY ORDINAL_POSITION
""")
src_rows = acur.fetchall()
print(f"  {'COLUMN':<25} {'TYPE':<22} {'LEN':>5} {'PREC':>5} {'SCALE':>5} {'CCSID':>7}  NULLABLE")
print("  " + "-" * 78)
for r in src_rows:
    print(f"  {str(r[0]):<25} {str(r[1]):<22} {str(r[2]):>5} {str(r[3]):>5} {str(r[4]):>5} {str(r[6]):>7}  {r[5]}")
acur.close()
aconn.close()

# ── MSSQL Target ───────────────────────────────────────────────────────────────
print()
print(SEP)
print("TARGET: MSSQL  dbo.THAI_Test  column definitions")
print(SEP)

mconn_str = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=" + MSSQL_HOST + ";"
    "DATABASE=" + MSSQL_DB + ";"
    "UID=" + MSSQL_USER + ";"
    "PWD=" + MSSQL_PASS + ";"
    "TrustServerCertificate=yes;Encrypt=yes;"
)
mconn = pyodbc.connect(mconn_str)
mcur  = mconn.cursor()
mcur.execute("""
    SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
           NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = 'THAI_Test'
    ORDER BY ORDINAL_POSITION
""")
tgt_rows = mcur.fetchall()
print(f"  {'COLUMN':<25} {'TYPE':<22} {'LEN':>5} {'PREC':>5} {'SCALE':>5}  NULLABLE")
print("  " + "-" * 72)
for r in tgt_rows:
    print(f"  {str(r[0]):<25} {str(r[1]):<22} {str(r[2]):>5} {str(r[3]):>5} {str(r[4]):>5}  {r[5]}")
mcur.close()
mconn.close()

# ── Compatibility Matrix ───────────────────────────────────────────────────────
print()
print(SEP)
print("COMPATIBILITY CHECK  (Source -> Target)")
print(SEP)

src_map = {r[0]: r for r in src_rows}
tgt_map = {r[0]: r for r in tgt_rows}

ok_count   = 0
warn_count = 0

for col, sr in src_map.items():
    tr = tgt_map.get(col)
    if not tr:
        print(f"  [MISS ] {col:<25} -- MISSING in target")
        warn_count += 1
        continue

    s_type  = str(sr[1])
    s_len   = sr[2]
    s_ccsid = sr[6]
    t_type  = str(tr[1])
    t_len   = tr[2]

    warnings = []
    if isinstance(s_len, int) and isinstance(t_len, int) and t_len < s_len:
        warnings.append("TARGET column TOO SHORT")
    if s_ccsid == 65535:
        warnings.append("CCSID=65535 (raw BIT DATA — may expand on encoding)")
    if "binary" in s_type.lower() and "binary" not in t_type.lower() and "varbinary" not in t_type.lower():
        warnings.append("Type mismatch: source is binary but target is not")

    status = "OK   " if not warnings else "WARN "
    if not warnings:
        ok_count += 1
    else:
        warn_count += 1

    note = ("  *** " + " | ".join(warnings) + " ***") if warnings else ""
    print(
        f"  [{status}] {col:<25} "
        f"src={s_type}({s_len}) ccsid={s_ccsid}  ->  tgt={t_type}({t_len})"
        f"{note}"
    )

for col in tgt_map:
    if col not in src_map:
        print(f"  [EXTRA] {col:<25} -- exists in target only (UDF/computed column)")

print()
print(f"  Result: {ok_count} OK   {warn_count} WARNINGS")
print(SEP)
