"""Preserve keyword-only refresh candidates; never mutate remote delivery state.

The prior local export is evidence of a change, not proof of remote metadata.
Immutable revision files survive repeated exports and require remote comparison.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path

from sourceaudio_delta import _track_id
from tracklist_columns import POSSIBLE_EXTERNAL_ID_COLS, _find_column


def outstanding_keyword_revisions(audit_directory):
    """Return pending revisions, including on refreshes with no new changes.

    A receipt is accepted only when it binds to the exact revision bytes and
    verifies every changed identity. An unverified initial baseline requires
    comparison of all its identities; it is not treated as a stale correction.
    """
    outstanding = []
    for revision in sorted(Path(audit_directory).glob("*.json")):
        if revision.name.endswith(".receipt.json"):
            continue
        evidence = json.loads(revision.read_text(encoding="utf-8"))
        if evidence.get("status") not in {"requires_remote_comparison", "baseline_unverified"}:
            continue
        receipt_path = revision.with_suffix(".receipt.json")
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            ids = (
                set(evidence["current_local_keywords"])
                if evidence["status"] == "baseline_unverified"
                else {change["external_id"] for change in evidence["changes"]}
            )
            if (
                receipt.get("status") == "remote_verified"
                and receipt.get("revision_sha256") == hashlib.sha256(revision.read_bytes()).hexdigest()
                and set(receipt.get("verified_external_ids", [])) == ids
            ):
                continue
        outstanding.append(revision)
    return outstanding


def _keywords(headers, rows):
    id_column = _find_column(headers, POSSIBLE_EXTERNAL_ID_COLS)
    keyword_column = _find_column(headers, ("Keywords",))
    if not id_column or not keyword_column:
        raise ValueError("Keyword audit requires External Id and Keywords")
    result = {}
    for row in rows:
        track_id = _track_id(row.get(id_column, ""))
        if not track_id:
            raise ValueError("Keyword audit refuses blank External Id")
        value = str(row.get(keyword_column) or "")
        if track_id in result and result[track_id] != value:
            raise ValueError("Keyword audit refuses conflicting duplicate External Id")
        result[track_id] = value
    return result


def audit_keyword_refresh(metadata_path, headers, rows, audit_directory):
    """Audit BEFORE replacing an export. Return immutable evidence path or None.

    Missing prior exports are explicitly recorded as unverified baselines.
    Only shared IDs are compared; audio reconciliation handles additions/removals.
    Blank new values are flagged for review, never authorized as remote clears.
    """
    metadata_path = Path(metadata_path)
    current = _keywords(headers, [dict(zip(headers, row)) for row in rows])
    prior = None
    if metadata_path.is_file():
        with metadata_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            prior = _keywords(list(reader.fieldnames or []), list(reader))
    changes = [] if prior is None else [
        {"external_id": key, "old_keywords": prior[key],
         "new_keywords": current[key], "requires_clear_review": not current[key]}
        for key in sorted(prior.keys() & current.keys())
        if prior[key] != current[key]
    ]
    if prior is not None and not changes:
        return None
    evidence = {
        "schema_version": 1, "metadata_path": str(metadata_path),
        "status": "requires_remote_comparison" if prior is not None else "baseline_unverified",
        "field": "Keywords", "prior_local_keywords": prior,
        "current_local_keywords": current, "changes": changes,
    }
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    destination = Path(audit_directory) / (digest + ".json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != encoded:
            raise ValueError("Keyword revision evidence is inconsistent")
        return destination
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".keyword-audit-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination
