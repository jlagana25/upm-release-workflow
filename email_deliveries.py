#!/usr/bin/env python3
"""Prepare and verify Qwire/Scripps deliveries around an Outlook connector."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from config import PRIVATE_STATE_DIR, ReleaseContext, context_from_cli_args
from delivery_common import (
    DeliverySafetyError,
    ManifestFile,
    checkpoint,
    collect_manifest,
    latest_workflow_gates,
    load_endpoint_state,
    private_json,
    receipt,
    require_live_authorization,
)
from delivery_state import set_partner_status


PLUGIN_ATTACHMENT_LIMIT = 3 * 1024 * 1024
QWIRE_MAX_RECORDS = 500_000
BODY = """Hi,

Please see attached for the latest metadata from Universal Production Music.

If you have any questions, please let me know.

Thanks,"""
SIGNATURE_SEPARATOR = "\n\n\n"  # two visually blank lines


class EmailDeliveryError(DeliverySafetyError):
    pass


@dataclass(frozen=True)
class EmailPlan:
    endpoint: str
    to: str
    subject: str
    body: str
    attachments: tuple[ManifestFile, ...]
    source_manifest: tuple[ManifestFile, ...]


@dataclass(frozen=True)
class EmailSnapshot:
    message_id: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[tuple[str, int, str], ...]


class OutlookGateway(Protocol):
    def create_draft(self, plan: EmailPlan) -> str: ...
    def inspect_draft(self, draft_id: str) -> EmailSnapshot: ...
    def send_draft(self, draft_id: str) -> None: ...
    def find_sent(self, draft_id: str) -> EmailSnapshot | None: ...


def compose_body(signature: str) -> str:
    """Place two blank lines between the sign-off and private signature."""
    return BODY + (SIGNATURE_SEPARATOR + signature.strip() if signature.strip() else "")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    path.chmod(0o600)


def _zip_files(sources: tuple[Path, ...], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            archive.write(source, arcname=source.name)
    destination.chmod(0o600)
    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        if names != [source.name for source in sources] or any(
            archive.read(source.name) != source.read_bytes() for source in sources
        ):
            raise EmailDeliveryError(f"ZIP verification failed: {destination}")
    return destination


def _zip_single(source: Path, destination: Path) -> Path:
    return _zip_files((source,), destination)


def _read_csv(source: Path) -> tuple[list[str], list[list[str]]]:
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows or not rows[0]:
        raise EmailDeliveryError(f"Metadata CSV has no header: {source}")
    if not rows[1:]:
        raise EmailDeliveryError(f"Metadata CSV has no data rows: {source}")
    return rows[0], rows[1:]


def _prepare_qwire(source: Path, output: Path) -> tuple[Path, ...]:
    header, rows = _read_csv(source)
    if len(rows) <= QWIRE_MAX_RECORDS:
        if source.stat().st_size < PLUGIN_ATTACHMENT_LIMIT:
            return (source,)
        zip_path = output / f"{source.stem}.zip"
        _zip_single(source, zip_path)
        if zip_path.stat().st_size < PLUGIN_ATTACHMENT_LIMIT:
            return (zip_path,)
        zip_path.unlink()
    chunks = [
        rows[index:index + QWIRE_MAX_RECORDS]
        for index in range(0, len(rows), QWIRE_MAX_RECORDS)
    ]
    parts: list[Path] = []
    for part, chunk in enumerate(chunks, start=1):
        csv_path = output / f"{source.stem} - Part {part:03d}.csv"
        _write_csv(csv_path, header, chunk)
        parts.append(csv_path)
    reconstructed: list[list[str]] = []
    for part_path in parts:
        reconstructed.extend(_read_csv(part_path)[1])
    if reconstructed != rows:
        raise EmailDeliveryError("Qwire attachment split does not reconstruct the source CSV")
    bundle = output / f"{source.stem} - Parts.zip"
    _zip_files(tuple(parts), bundle)
    if bundle.stat().st_size >= PLUGIN_ATTACHMENT_LIMIT:
        raise EmailDeliveryError(
            "Qwire split-file ZIP exceeds the Outlook connector's 3 MiB limit"
        )
    return (bundle,)


def _prepare_scripps(source: Path, output: Path) -> tuple[Path, ...]:
    _read_csv(source)
    if source.stat().st_size < PLUGIN_ATTACHMENT_LIMIT:
        return (source,)
    destination = output / f"{source.stem}.zip"
    _zip_single(source, destination)
    if destination.stat().st_size >= PLUGIN_ATTACHMENT_LIMIT:
        raise EmailDeliveryError("Scripps ZIP still exceeds the Outlook attachment limit")
    return (destination,)


def prepare_email_plan(
    ctx: ReleaseContext,
    endpoint: str,
    signature: str,
    *,
    output_dir: Path | None = None,
) -> EmailPlan:
    if endpoint not in {"qwire", "scripps"}:
        raise EmailDeliveryError(f"Unsupported email endpoint: {endpoint}")
    source = ctx.partner_metadata[endpoint]
    source_manifest = collect_manifest(source, allowed_suffixes=frozenset({".csv"}))
    output = output_dir or (ctx.specials_dir / "_WORKFLOW" / "email_attachments" / endpoint)
    attachments = _prepare_qwire(source, output) if endpoint == "qwire" else _prepare_scripps(source, output)
    attachment_manifest = tuple(
        item for path in attachments for item in collect_manifest(
            path, allowed_suffixes=frozenset({".csv", ".zip"})
        )
    )
    if any(item.size >= PLUGIN_ATTACHMENT_LIMIT for item in attachment_manifest):
        raise EmailDeliveryError("Prepared Outlook attachment is not below 3 MiB")
    to = "libraries@qwire.com" if endpoint == "qwire" else "patrick.magee@scripps.com"
    body = compose_body(signature)
    return EmailPlan(
        endpoint, to,
        f"Universal Production Music - {ctx.delivery_display_folder} Metadata Delivery",
        body, attachment_manifest, source_manifest,
    )


def write_connector_handoff(ctx: ReleaseContext, plan: EmailPlan) -> Path:
    """Write the exact non-secret request consumed by the Outlook connector step."""
    return private_json(
        ctx.specials_dir / "_WORKFLOW" / "connector_handoffs" / f"{plan.endpoint}.json",
        {
            "schema_version": 1,
            "release_id": ctx.release_id,
            "endpoint": plan.endpoint,
            "to": [plan.to],
            "cc": [],
            "bcc": [],
            "subject": plan.subject,
            "body": plan.body,
            "attachments": [item.public_dict() | {"local_path": str(item.path)} for item in plan.attachments],
            "action": "create_draft_then_require_exact_release_authorization_before_send",
        },
    )


def _verify_snapshot(plan: EmailPlan, snapshot: EmailSnapshot) -> None:
    expected_attachments = tuple(
        (item.path.name, item.size, item.sha256) for item in plan.attachments
    )
    if snapshot.to != (plan.to,) or snapshot.cc or snapshot.bcc:
        raise EmailDeliveryError("Outlook recipients do not exactly match the plan")
    if snapshot.subject != plan.subject or snapshot.body != plan.body:
        raise EmailDeliveryError("Outlook subject or body does not exactly match the plan")
    if snapshot.attachments != expected_attachments:
        raise EmailDeliveryError("Outlook attachments do not exactly match the plan")


def deliver_email(
    ctx: ReleaseContext,
    endpoint: str,
    dry_run: bool,
    logger: logging.Logger,
    *,
    gateway: OutlookGateway | None = None,
    signature: str = "",
    live_confirmation: str | None = None,
) -> bool:
    try:
        if dry_run:
            with tempfile.TemporaryDirectory(prefix=f"upm-{endpoint}-") as temporary:
                plan = prepare_email_plan(
                    ctx, endpoint, signature, output_dir=Path(temporary)
                )
                logger.info(
                    "  [DRY RUN] %s: %d attachment(s), recipient=%s, subject=%s",
                    endpoint, len(plan.attachments), plan.to, plan.subject,
                )
            return True
        if not signature.strip():
            raise EmailDeliveryError("Approved private plain-text Outlook signature is required")
        plan = prepare_email_plan(ctx, endpoint, signature)
        require_live_authorization(ctx, live_confirmation)
        gate_ok, detail = latest_workflow_gates(
            ctx, ("10 Final packaging", "15 Final metadata check")
        )
        if not gate_ok:
            raise EmailDeliveryError(f"{endpoint} delivery is blocked: {detail}")
        if gateway is None:
            handoff = write_connector_handoff(ctx, plan)
            raise EmailDeliveryError(
                f"Outlook connector gateway is unavailable; handoff is prepared but unsent: {handoff}"
            )
        prior = load_endpoint_state(ctx, endpoint)
        prior_phase = str((prior or {}).get("phase") or "")
        prior_ids = tuple(str(value) for value in ((prior or {}).get("remote_ids") or []))
        if prior_phase in {"send_intent", "sent_pending_verification"}:
            if len(prior_ids) != 1:
                raise EmailDeliveryError("Uncertain Outlook send has no unique draft ID")
            sent = gateway.find_sent(prior_ids[0])
            if sent is None:
                raise EmailDeliveryError(
                    "Prior Outlook send is awaiting Sent Items verification; refusing a duplicate"
                )
            _verify_snapshot(plan, sent)
            draft_id = prior_ids[0]
        else:
            if prior_phase == "drafted" and len(prior_ids) == 1:
                draft_id = prior_ids[0]
            else:
                draft_id = gateway.create_draft(plan)
                checkpoint(ctx, endpoint, plan.source_manifest, "drafted", "Outlook draft created", remote_ids=(draft_id,))
            _verify_snapshot(plan, gateway.inspect_draft(draft_id))
            checkpoint(
                ctx, endpoint, plan.source_manifest, "send_intent",
                "exact draft verified; send authorized", remote_ids=(draft_id,),
            )
            gateway.send_draft(draft_id)
            checkpoint(
                ctx, endpoint, plan.source_manifest, "sent_pending_verification",
                "send action completed; awaiting Sent Items", remote_ids=(draft_id,),
            )
            sent = gateway.find_sent(draft_id)
        if sent is None:
            raise EmailDeliveryError("Matching message was not found in Sent Items")
        _verify_snapshot(plan, sent)
        path = receipt(ctx, endpoint, plan.source_manifest, {
            "message_id": sent.message_id,
            "to": plan.to,
            "subject": plan.subject,
            "attachments": [item.path.name for item in plan.attachments],
        })
        set_partner_status(ctx.specials_dir, endpoint, "delivered")
        logger.info("  ✓ %s email verified in Sent Items: %s", endpoint, path)
        return True
    except Exception as exc:
        logger.error("  ✗ %s email delivery failed: %s", endpoint, exc)
        return False


def load_private_signature() -> str:
    path = PRIVATE_STATE_DIR / "outlook_signature.txt"
    if not path.is_file():
        raise EmailDeliveryError(f"Private Outlook signature is missing: {path}")
    if path.stat().st_mode & 0o077:
        raise EmailDeliveryError("Private Outlook signature permissions must be 0600")
    return path.read_text(encoding="utf-8")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Prepare Qwire or Scripps Outlook delivery")
    parser.add_argument("endpoint", choices=("qwire", "scripps"))
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        signature = load_private_signature()
    except EmailDeliveryError:
        signature = "" if args.dry_run else None
    if signature is None:
        raise SystemExit("Private Outlook signature is required")
    ok = deliver_email(
        context_from_cli_args(args), args.endpoint, True,
        logging.getLogger("email_delivery"), signature=signature,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
