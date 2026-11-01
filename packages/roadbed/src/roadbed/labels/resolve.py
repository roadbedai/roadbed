"""DuckDB label resolver: captured interactions + reviewer verdicts + late outcomes -> resolved rows.

    rows = resolve(interactions, verdicts, outcomes, LABEL_RULES_V1, as_of="2026-11-01T09:00:00Z")

Inputs are the request bodies of POST /v1/interactions, /v1/verdicts and /v1/outcomes (plain dicts). The
selection (latest verdict, latest outcome, lateness) runs in DuckDB; label assembly runs in Python because it
merges JSON objects. Output order is by interaction_id, so the result does not depend on input order.

A resolved row holds the seven row-hash fields plus `slices` and `row_hash` (the snapshot-row shape), and resolver
fields that never reach a snapshot part: `use` (train | eval | quarantine), `action`, `weight`, `label_source`,
`fix` (the reviewer's corrected output on a fix), `verdict_id`, `outcome_id`, `actor`, `model_version`,
`profile_id`, `hmac_key_id`, `synthetic` and `input_text` (the pseudonymised input as JCS text; its sha256 is the
input_ref, and the MinHash disjointness check reads it).

Quarantined rows keep only `interaction_id`, `use` and `reason`: they carry nothing that could leak. An
interaction id that is itself contact-shaped is replaced by `redacted:<sha256 prefix>`.

Every field that reaches an output file is scanned: the interaction id, the model version, the slices, the output,
the label, the fix, the reason code and the outcome.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import duckdb
import jsonschema
import numpy as np
from pydantic import ValidationError

from roadbed.asset.hashing import jcs, row_hash, sha256_bytes
from roadbed.contracts import InteractionIn, OutcomeIn, VerdictIn
from roadbed.labels.guard import contact_shaped, scan_input, scan_label_fields, scan_outcome
from roadbed.labels.rules import LABEL_RULES_V1, LabelRules, check_pinned

# CONTRACT-CHANGE-REQUEST: VerdictIn has no reviewer id, but correction-test-row requires created_by. The control
# plane knows the authenticated reviewer; it passes it as `actor` on the stored verdict record. The resolver strips
# `actor` before validating the rest against VerdictIn.
SERVER_FIELDS = ("actor",)
ACTOR_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

_SQL = """
with v as (
  select *, row_number() over (partition by iid order by ts desc, vid desc) as rn
  from verdicts where ($as_of is null or ts <= $as_of)
),
o as (
  select *, row_number() over (partition by iid order by observed_at desc, oid desc) as rn
  from outcomes where ($as_of is null or observed_at <= $as_of)
)
select i.iid, i.idx, v.vidx, o.oidx,
       (o.observed_at is not null and o.observed_at >= v.ts) as late
from interactions i
join v on v.iid = i.iid and v.rn = 1
left join o on o.iid = i.iid and o.rn = 1
where ($as_of is null or i.ts <= $as_of)
order by i.iid
"""


class ResolveError(ValueError):
    """The inputs are inconsistent (a retried id with other content, a body that breaks its contract)."""


def _check(model: Any, body: Mapping[str, Any], what: str, i: int) -> None:
    try:
        model.model_validate(dict(body))
    except ValidationError as e:
        first = e.errors()[0]
        raise ResolveError(f"{what} {i}: {'/'.join(str(x) for x in first['loc'])}: {first['msg']}") from None


def _dedupe(rows: Iterable[Mapping[str, Any]], key: str, what: str) -> list[dict[str, Any]]:
    """Retries with the same id are no-ops; the same id with other content is an error."""
    seen: dict[str, tuple[bytes, dict[str, Any]]] = {}
    for r in rows:
        d = dict(r)
        b = jcs(d)
        prev = seen.get(d[key])
        if prev is None:
            seen[d[key]] = (b, d)
        elif prev[0] != b:
            raise ResolveError(f"{what} {d[key]!r} was sent twice with different content")
    return [seen[k][1] for k in sorted(seen)]


def _obj(values: Sequence[Any]) -> np.ndarray:
    a = np.empty(len(values), dtype=object)
    a[:] = list(values)
    return a


def resolve(
    interactions: Iterable[Mapping[str, Any]],
    verdicts: Iterable[Mapping[str, Any]],
    outcomes: Iterable[Mapping[str, Any]],
    rules: LabelRules = LABEL_RULES_V1,
    *,
    as_of: str | None = None,
    output_schema: Mapping[str, Any] | None = None,
    erased_subjects: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Resolve one label per reviewed interaction under pinned label rules. See the module docstring.

    as_of: UTC timestamp; interactions, verdicts and outcomes after it are not seen.
    output_schema: task output schema; a label that does not validate is quarantined.
    erased_subjects: subject tokens with a tombstone; their interactions are left out.
    """
    check_pinned(rules)
    erased = set(erased_subjects)
    ints = _dedupe(interactions, "interaction_id", "interaction")
    vds_raw = _dedupe(verdicts, "verdict_id", "verdict")
    ocs = _dedupe(outcomes, "outcome_id", "outcome")
    for i, r in enumerate(ints):
        _check(InteractionIn, r, "interaction", i)
    vds = []
    for i, r in enumerate(vds_raw):
        actor = r.get("actor")
        if actor is not None and not (isinstance(actor, str) and re.match(ACTOR_PATTERN, actor)):
            raise ResolveError(f"verdict {r['verdict_id']!r}: actor must be a user id, not {actor!r}")
        _check(VerdictIn, {k: v for k, v in r.items() if k not in SERVER_FIELDS}, "verdict", i)
        vds.append(r)
    for i, r in enumerate(ocs):
        _check(OutcomeIn, r, "outcome", i)
    ints = [r for r in ints if r.get("subject_token") not in erased]
    if as_of is not None:
        _check(OutcomeIn, {"outcome_id": "x", "interaction_id": "x", "outcome": {}, "source": "csv", "observed_at": as_of},
               "as_of", 0)

    con = duckdb.connect()
    con.register("interactions", {
        "iid": _obj([r["interaction_id"] for r in ints]), "ts": _obj([r["ts"] for r in ints]),
        "idx": np.arange(len(ints), dtype=np.int64),
    })
    con.register("verdicts", {
        "vid": _obj([r["verdict_id"] for r in vds]), "iid": _obj([r["interaction_id"] for r in vds]),
        "ts": _obj([r["ts"] for r in vds]), "vidx": np.arange(len(vds), dtype=np.int64),
    })
    con.register("outcomes", {
        "oid": _obj([r["outcome_id"] for r in ocs]), "iid": _obj([r["interaction_id"] for r in ocs]),
        "observed_at": _obj([r["observed_at"] for r in ocs]), "oidx": np.arange(len(ocs), dtype=np.int64),
    })
    picked = con.execute(_SQL, {"as_of": as_of}).fetchall()
    con.close()

    out_validator = jsonschema.Draft202012Validator(dict(output_schema)) if output_schema is not None else None
    rows = []
    for _iid, idx, vidx, oidx, late in picked:
        rows.append(_assemble(ints[idx], vds[vidx], ocs[oidx] if oidx is not None else None, bool(late), rules, out_validator))
    return rows


def _quarantine(iid: str, reason: str) -> dict[str, Any]:
    return {"interaction_id": iid, "use": "quarantine", "reason": reason}


def _assemble(
    it: Mapping[str, Any], vd: Mapping[str, Any], oc: Mapping[str, Any] | None, late: bool, rules: LabelRules,
    out_validator: jsonschema.Draft202012Validator | None,
) -> dict[str, Any]:
    iid = it["interaction_id"]
    if contact_shaped(iid) is not None:
        return _quarantine("redacted:" + sha256_bytes(iid.encode("utf-8"))[:16], "interaction_id is contact-shaped")
    for where, obj in (("version", {"version": it["version"]}), ("slices", dict(it.get("slices") or {}))):
        if (why := scan_label_fields(obj, where)) is not None:
            return _quarantine(iid, why)
    text = jcs(it["input"]).decode("utf-8")
    if "sha256:" + sha256_bytes(text.encode("utf-8")) != it["input_ref"]:
        return _quarantine(iid, "input_ref does not match the input")
    if (why := scan_input(it["input"])) is not None:
        return _quarantine(iid, why)

    action = vd["action"]
    output = dict(it["output"])
    fix = dict(vd["verdict"]) if action == "fix" else None
    label: dict[str, Any] | None
    if action == "approve":
        label = dict(output)
    elif action == "fix":
        label = dict(vd["verdict"])
    else:
        label = None
    source = "verdict"
    outcome = dict(oc["outcome"]) if oc is not None else None
    if label is not None and outcome is not None and late and rules.late_outcome_overrides:
        for k, v in outcome.items():
            f = rules.outcome_field(k)
            if f in label and label[f] != v:
                label[f] = v
                source = "outcome"
    reason_code = vd.get("reason_code") if action != "approve" else None

    for where, val in (("output", output), ("verdict", label), ("fix", fix), ("reason_code", reason_code)):
        if val is not None and (why := scan_label_fields(val, where)) is not None:
            return _quarantine(iid, why)
    if outcome is not None and (why := scan_outcome(outcome)) is not None:
        return _quarantine(iid, why)
    if out_validator is not None:
        for where, val in (("output", output), ("verdict", label), ("fix", fix)):
            if val is not None and (err := jsonschema.exceptions.best_match(out_validator.iter_errors(val))) is not None:
                return _quarantine(iid, f"{where} does not match the output schema: {err.message}")

    row: dict[str, Any] = {
        "interaction_id": iid,
        "input_ref": it["input_ref"],
        "output": output,
        "verdict": label,
        "reason_code": reason_code,
        "outcome": outcome,
        "ts": vd["ts"],
        "slices": dict(it.get("slices") or {}),
    }
    row["row_hash"] = row_hash(row)
    row.update(
        use="eval" if action in rules.eval_only else "train",
        action=action,
        weight=float(rules.weights[action]),
        label_source=source if label is not None else None,
        fix=fix,
        verdict_id=vd["verdict_id"],
        outcome_id=oc["outcome_id"] if oc is not None else None,
        actor=vd.get("actor"),
        model_version=it["version"],
        profile_id=it["profile_id"],
        hmac_key_id=it["hmac_key_id"],
        synthetic=bool(it.get("synthetic")),
        input_text=text,
    )
    return row


def summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts by use, action and label source. Ids only, no row content."""
    by_use: dict[str, int] = {}
    by_action: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for r in rows:
        by_use[r["use"]] = by_use.get(r["use"], 0) + 1
        if r["use"] != "quarantine":
            by_action[r["action"]] = by_action.get(r["action"], 0) + 1
            if r["label_source"]:
                by_source[r["label_source"]] = by_source.get(r["label_source"], 0) + 1
    return {"rows": len(rows), "use": dict(sorted(by_use.items())), "action": dict(sorted(by_action.items())),
            "label_source": dict(sorted(by_source.items())),
            "quarantined": sorted(r["interaction_id"] for r in rows if r["use"] == "quarantine")}

