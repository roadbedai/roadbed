"""Write late outcomes from a CSV file to local files. One outcome per row. No control plane is needed.

The CSV needs the columns interaction_id and observed_at. Other columns become the outcome.
Run: ROADBED_TENANT_KEY=<16 or more characters> python reconcile_outcomes.py outcomes.csv
"""

import os
import sys

import roadbed_sdk

roadbed_sdk.init_local(
    "./roadbed-events",
    tenant_key=os.environ["ROADBED_TENANT_KEY"],
    profile_id="demo-default@v1",
)

report = roadbed_sdk.reconcile(sys.argv[1])
print(f"rows: {report.rows}, skipped: {report.skipped}, error: {report.error}")

roadbed_sdk.shutdown()
