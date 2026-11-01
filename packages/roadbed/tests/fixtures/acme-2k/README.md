# acme-2k (SYNTHETIC)

Event stream of a synthetic insurer tenant, 2,000 claims. Test fixture for `roadbed.labels` and `roadbed.evals` (W14).

Made with the W05 generator, platform branch `w05-demo-datasets` at 8349c4c1c1e3abea26065277653a3da48d16f2c5:

    uv run --project demo python -m demo.datasets.insurer generate --seed 7 --n 2000 --out acme2k
    gzip -n -9 -c acme2k/events/<name>.jsonl > <name>.jsonl.gz      # interactions, verdicts, outcomes

`slices.yaml` and `output.schema.json` are copied from the same run (`evals/`).

Every row is synthetic (`synthetic: true` on interactions, `syn-` id prefix on verdicts and outcomes). Inputs are
pseudonymised (`<PERSON_1>`, `<EMAIL_1>`, ...); a scan for email- and phone-shaped strings finds none.

| File | Rows |
|---|---|
| interactions | 2000 |
| verdicts | 2000 (approve 1738, fix 236, escalate 26) |
| outcomes | 1187 |
