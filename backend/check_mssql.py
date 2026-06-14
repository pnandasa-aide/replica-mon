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
    
    # Apply Option A to THAI_Test and THAI_Test_vanila
    queries = [
        "ALTER TABLE dbo.THAI_Test ALTER COLUMN STORE_ID VARBINARY(50) NULL",
        "ALTER TABLE dbo.THAI_Test_vanila ALTER COLUMN STORE_ID VARBINARY(50) NULL"
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
