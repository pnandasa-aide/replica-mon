import os
import pyodbc

try:
    server = os.getenv("MSSQL_HOST", "192.168.13.62")
    database = os.getenv("MSSQL_DATABASE", "GSTargetDB")
    user = os.getenv("MSSQL_USER", "gstgdblogin")
    password = os.getenv("MSSQL_PASSWORD", "tar53t@dm1n")
    driver = "ODBC Driver 18 for SQL Server"
    
    conn_str = (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
                f"UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=yes;")
    
    print(f"Connecting to MSSQL: {server}")
    conn = pyodbc.connect(conn_str, timeout=5)
    cursor = conn.cursor()
    
    # Fix CCSID=65535 BIT DATA columns: expand to VARBINARY to handle multi-byte expansion
    # STORE_ID : CHAR(10) CCSID=65535    -> VARBINARY(400) (10 bytes x up to 4 bytes/char UTF-16,
    #                                        with EBCDIC surrogate pairs = up to 40 bytes;
    #                                        400 gives 40x safety margin for any edge-case encoding)
    # RAW_DATA : VARCHAR(50) CCSID=65535 -> VARBINARY(100) (50 bytes x ~2 bytes/char)
    queries = [
        "ALTER TABLE dbo.THAI_Test       ALTER COLUMN STORE_ID VARBINARY(400) NULL",
        "ALTER TABLE dbo.THAI_Test_vanila ALTER COLUMN STORE_ID VARBINARY(400) NULL",
        "ALTER TABLE dbo.THAI_Test       ALTER COLUMN RAW_DATA VARBINARY(100) NULL",
        "ALTER TABLE dbo.THAI_Test_vanila ALTER COLUMN RAW_DATA VARBINARY(100) NULL",
    ]
    
    for q in queries:
        print(f"Executing: {q}")
        try:
            cursor.execute(q)
            conn.commit()
            print("  SUCCESS")
        except Exception as ex:
            print(f"  FAILED: {ex}")
            
    conn.close()
except Exception as e:
    import traceback
    traceback.print_exc()
