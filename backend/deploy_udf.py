#!/usr/bin/env python3
import os
import sys
import base64
import requests
import urllib3
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# API config
CORE_HUB_URL = os.getenv('GLUESYNC_HOST', 'https://localhost:1717')
ADMIN_USER = "admin"
ADMIN_PASS = os.getenv("GLUESYNC_ADMIN_PASSWORD") or "P@ssw0rd"

PIPELINE_ID = "f590ab8c"
UDF_NAME = "UDF_7538ee07faac465497a33b1dde10d61b"

def get_token():
    print(f"Logging in to Core Hub at {CORE_HUB_URL}...")
    url = f"{CORE_HUB_URL}/authentication/login"
    resp = requests.post(url, json={"username": ADMIN_USER, "password": ADMIN_PASS}, verify=False)
    resp.raise_for_status()
    token = resp.json()["apiToken"]
    print("Login successful.")
    return token

def deploy_udf(token):
    # Read Java file
    java_file_path = Path("/app/replica-test/udfs/ThaiTestFullnameUDF.java")
    if not java_file_path.exists():
        # fallback path in case local run
        java_file_path = Path(__file__).parent.parent.parent / "replica-test/udfs/ThaiTestFullnameUDF.java"
    
    print(f"Reading code from {java_file_path}...")
    code = java_file_path.read_text(encoding="utf-8")
    
    # Replace template class name with the exact compiled UDF name
    code = code.replace("class UDF_REPLACE_WITH_COREHUB_ID", f"public class {UDF_NAME}")
    
    # Base64 encode
    b64_code = base64.b64encode(code.encode("utf-8")).decode("utf-8")
    
    print(f"Compiling and deploying UDF '{UDF_NAME}' to pipeline '{PIPELINE_ID}'...")
    url = f"{CORE_HUB_URL}/pipelines/{PIPELINE_ID}/config/entities/mapping-functions/compile-mapping-function"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "code": b64_code,
        "type": "Java",
        "udfName": UDF_NAME
    }
    
    resp = requests.post(url, json=payload, headers=headers, verify=False)
    if resp.status_code in (200, 202):
        print(f"UDF '{UDF_NAME}' deployed successfully!")
        print(resp.text)
    else:
        print(f"Deployment failed (Status: {resp.status_code}): {resp.text}")
        sys.exit(1)

def main():
    try:
        token = get_token()
        deploy_udf(token)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
