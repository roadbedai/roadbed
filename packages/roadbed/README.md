# roadbed

Engine for Roadbed: scoring, paired bootstrap per slice, and the release gate. Prototype, v0.1 of the spec.

## Watch and license policy

`roadbed.watch.scan(glob, state_path, policy)` lists the author's models on the Hugging Face Hub, keeps the ids that match the glob (author literal, wildcards in the name), pins each new base to its revision sha, and reads its license from cardData and the LICENSE file at that sha. `roadbed.license_policy.decide(card, policy)` returns allow, review or deny from `policy/license.yaml`; a base with no license is always denied. A state file reports each base once (the first scan of a glob reports only the newest match). A 24 h cache lets `--offline` and Hub outages work.

```
roadbed watch --base 'Qwen/Qwen3.*-27B'            # inside an asset repo: applies policy/license.yaml
roadbed watch --base 'Qwen/Qwen3.*-27B' --offline  # cache only, at most 24 h old
```

Only `https://huggingface.co` is contacted. No token, cookie or proxy setting is sent and redirects are not followed.

Tests replay recorded cassettes (`tests/cassettes/`) and never touch the network:

```
PYTHONPATH=packages/roadbed/tests/watch uv run pytest -q -p no_network packages/roadbed/tests/watch packages/roadbed/tests/cli/test_cli_watch.py
```

Re-record on a machine with network: delete `tests/cassettes/hub_*.yaml`, then `ROADBED_VCR_RECORD=once uv run pytest packages/roadbed/tests/watch`. Recording keeps only the `accept` request header and the `content-type`, `etag`, `x-repo-commit` response headers. `ROADBED_NETWORK=1` runs one live test.
