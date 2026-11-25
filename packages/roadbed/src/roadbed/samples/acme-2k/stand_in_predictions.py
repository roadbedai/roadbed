"""Write stand-in prediction files from a sealed holdout. No model runs.

Usage: python stand_in_predictions.py HOLDOUT_JSONL OUT_DIR
The candidate repeats each expected output. The incumbent repeats it too, except that `route` is rotated on every sixth row.
"""

import json
import sys
from pathlib import Path

ROUTES = ["fast_track", "standard", "complex", "siu"]

holdout, out = Path(sys.argv[1]), Path(sys.argv[2])
rows = [json.loads(line) for line in holdout.read_text(encoding="utf-8").splitlines() if line.strip()]
out.mkdir(parents=True, exist_ok=True)
for name in ("incumbent", "candidate"):
    with (out / f"predictions.{name}.jsonl").open("w", encoding="utf-8") as f:
        for i, row in enumerate(rows):
            output = dict(row["expected"])
            if name == "incumbent" and i % 6 == 0:
                output["route"] = ROUTES[(ROUTES.index(output["route"]) + 1) % len(ROUTES)]
            f.write(json.dumps({"id": row["id"], "output": output}) + "\n")
print("stand-in predictions, no model was run")
