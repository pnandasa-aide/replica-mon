# GlueSync WebSocket Architecture & Findings

## Overview
This document outlines the findings from our investigation into the GlueSync WebSocket monitoring stream. The objective was to intercept, subscribe to, and decode the live telemetry data to power real-time UI updates for `replica-mon`.

---

## 1. Establishing the Connection (The Subscription)

The GlueSync CoreHub WebSocket runs over a secure `wss://` protocol at `/ui`. To start receiving live updates for specific entities, the client must first send a subscription request via the REST API.

**Endpoint:** `PUT /ui/entities-metrics-subscription`
**Authentication:** Requires a standard Bearer Token.
**Required Payload:** The backend strictly requires a `EntitiesMetricsSubscriptionDto` JSON structure consisting of the Pipeline ID and an array of Entity Names.

```json
{
  "pipelineId": "f590ab8c",
  "entities": [
    "GSLIBTST.CUSTOMERS"
  ]
}
```

*Note: Without this exact payload, the backend will return a `500 HttpMessageNotReadableException`.*

---

## 2. Message Format: Protocol Buffers (Protobuf)

Unlike typical web applications that stream JSON, GlueSync transmits live statistics using **deeply nested Protocol Buffers (Protobuf)**. 

### Why Protobuf?
- **Speed & Size:** It is a highly optimized binary format that strips out JSON keys. Strings are sent as plain text, but all numbers (like "rows processed", "latency", and "status codes") are compressed into binary chunks called "varints" (Variable-Length Integers).
- **Impact on Parsing:** Because the keys are missing (replaced by Field IDs like `Field 1`, `Field 2`), parsing the binary stream directly in a raw Python script results in a `UnicodeDecodeError`.

### Observed Message Types
By performing a raw hex-dump of the stream, we identified several message types:
1. `LicenseStatusMessage`: Contains license validity info (e.g., "trial until 2026-05-28").
2. `ConnectedExternalModulesMessaged`: Configuration mapping for external integrations (e.g., Grafana dashboards).
3. `NotificationMessage`: Push notifications for the UI.
4. `EntityStatusMessage`: Specific health and activity updates for a tracked entity.
5. `PipelineStatusMessage`: The main 600+ byte payload containing deeply nested Source Agent, Target Agent, and database configurations.

---

## 3. How to Decode the Stream

Because the messages are Protobuf, decoding them requires a specialized parser. We have created a lightweight, recursive Python parser in `gluesync_ws.py` that unpacks the binary wire-format without external dependencies.

### The Recursive Parsing Approach
Our custom `parse_protobuf` script reads the binary tags (Field ID + Wire Type):
- **Wire Type 0 (Varint):** Decodes the live metrics/integers.
- **Wire Type 2 (Length-Delimited):** Tries to decode as UTF-8 Strings. If it fails (due to control characters), it recognizes that it has hit a nested Protobuf message and *recursively* calls itself to unpack the next layer.

### Example Decoded Output
```json
{
  "Field_1_string": "PipelineStatusMessage",
  "Field_2_message": {
    "Field_1_string": "f590ab8c",
    "Field_2_message": {
      "Field_1_string": "ship-at-scale-ibm-iseries",
      "Field_2_string": "2.2.5 - build 4",
      "Field_3_string": "user001@161.82.146.249",
      "Field_10_varint": 500
    }
  }
}
```
*Notice how Field 10 is `500`. In the raw binary, this would be unreadable bytes, but it represents a live metric (e.g., rows replicated).*

### Example: Decoded `EntityStatusMessage`
```json
{
  "Field_1_string": "EntityStatusMessage",
  "Field_2_message": {
    "Field_1_message": {
      "Field_1_message": {
        "Field_1_string": "f590ab8c",      // Pipeline ID
        "Field_2_string": "4bacd683",      // Entity ID
        "Field_3_varint": 0,               // Error Flag / State
        "Field_4_varint": 1,               // Status Enum (e.g., 1 = ACTIVE)
        "Field_5_varint": 0,               // Hold Flag
        "Field_6_varint": 0
      }
    }
  }
}
```
*In the `EntityStatusMessage`, the live status is located in **Field 4** (where `1` generally represents the "Active" enum state).*

### Example: Decoded `MetricsMessage`
```json
{
  "Field_1_string": "MetricsMessage",
  "Field_2_message": {
    "Field_1_message": {
      "Field_1_message": {
        "Field_1_string": "f590ab8c",               // Pipeline ID
        "Field_2_message": {
          "Field_1_string": "f590ab8c",             // Entity/Pipeline ID
          "Field_2_varint": 1779001642299713119,    // Timestamp
          "Field_4_varint": 415,                    // Metric 1 (e.g. Inserts)
          "Field_5_varint": 58316,                  // Metric 2 (e.g. Updates)
          "Field_6_varint": 130,                    // Metric 3 (e.g. Deletes)
          "Field_7_varint": 5521654                 // Total Operations / Bytes
        }
      }
    }
  }
}
```
*The live replication numbers (like rows replicated) are cleanly extracted in fields 4, 5, 6, and 7.*

---

## 4. Architectural Client Options for `replica-mon`

Now that we understand the data, we must decide how `replica-mon` will consume these metrics. 

### Option 1: Decode in the Dashboard (Recommended)
**Approach:** Connect to the WebSocket directly from the `replica-mon` HTML/JS dashboard via the browser, completely bypassing Python.
- **Pros:** 
  - The GlueSync UI is built with Angular/React, meaning their Javascript bundles *already contain the compiled Protobuf decoders*. 
  - By reverse-engineering their Webpack bundles or using a tool like `protobuf.js`, the browser can automatically map `Field_10` back to `"rows_processed"`.
  - Zero load on the Python backend; perfectly real-time.
- **Cons:** Requires javascript development and extracting the `.proto` schemas from the vendor's frontend.

### Option 2: Decode in the Python CLI (`gluesync_ws.py`)
**Approach:** Continue using our recursive zero-dependency Python parser to extract the `Field_X_varint` metrics on the backend.
- **Pros:** Keeps all logic strictly within the terminal/container.
- **Cons:** Highly brittle. If Molo17 updates GlueSync and adds a new field, all the Field IDs shift, and your script breaks. You will have to manually figure out which varint corresponds to which metric by comparing the numbers to the dashboard.

### Option 3: Fallback to the REST API
**Approach:** Ignore the WebSocket completely and rely on the `GET /pipelines/{id}/entities` endpoint we built in `gluesync_cli_v2.py`.
- **Pros:** 100% stable, perfectly formatted JSON.
- **Cons:** It only provides static configuration health (e.g., `status: configured`), not live operational metrics (e.g., latency, CDC active, row counts).

## Conclusion
We have successfully bridged the WebSocket and cracked the binary payload. For the best integration into `replica-mon`, **Option 1** is highly recommended to leverage the vendor's existing Protobuf schemas in the browser.
