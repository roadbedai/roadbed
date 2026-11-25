"""Send late outcomes from a CSV file. One outcome per row.

The CSV needs the columns interaction_id and observed_at. Other columns become the outcome.
Run: ROADBED_URL=... ROADBED_TOKEN=... ROADBED_TENANT_KEY=... python reconcile_outcomes.py outcomes.csv
"""

import os
import sys

import roadbed_sdk

roadbed_sdk.init(
    os.environ["ROADBED_URL"],
    os.environ["ROADBED_TOKEN"],
    tenant_key=os.environ["ROADBED_TENANT_KEY"],
    profile_id="demo-default@v1",
)

report = roadbed_sdk.reconcile(sys.argv[1])
print(f"rows: {report.rows}, skipped: {report.skipped}, error: {report.error}")

roadbed_sdk.shutdown()
