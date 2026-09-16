#!/usr/bin/env python3
"""Local state bridge for Codex's Outlook Email connector.

The connector itself is invoked by Codex, not by this Python process.  This
module prepares and validates the exact handoff contract and records only the
milestones that Codex has independently verified through Outlook.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from config import PRIVATE_STATE_DIR, ReleaseContext, context_from_cli_args
from delivery_common import (
    DeliverySafetyError,
    checkpoint,
    collect_manifest,
    latest_workflow_gates,
    load_endpoint_state,
    receipt,
    require_live_authorization,
)
from delivery_state import set_partner_status
from email_deliveries import (
    EmailDeliveryError,
    EmailPlan,
    load_private_signature,
    prepare_email_plan,
    compose_body,
    write_connector_handoff,
)


class OutlookBridgeError(DeliverySafetyError):
    pass


def enroll_private_signature(*, input_func=input) -> Path:
    """Collect a multiline signature without putting identity data in argv."""
    print("Paste the approved plain-text Outlook signature after 'Thanks,'.")
    print("Enter a line containing only a single period (.) when finished.")
    lines: list[str] = []
    while True:
        line = input_func()
        if line == ".":
            break
        lines.append(line)
    value = "\n".join(lines).strip()
    if not value:
        raise OutlookBridgeError("Outlook signature cannot be empty")
    path = PRIVATE_STATE_DIR / "outlook_signature.txt"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def prepare_handoff(ctx: ReleaseContext, endpoint: str) -> tuple[Path, EmailPlan]:
    gate_ok, detail = latest_workflow_gates(
        ctx, ("10 Final packaging", "15 Final metadata check")
    )
    if not gate_ok:
        raise OutlookBridgeError(f"{endpoint} connector handoff is blocked: {detail}")
    plan = prepare_email_plan(ctx, endpoint, load_private_signature())
    if len(plan.attachments) != 1:
        raise OutlookBridgeError(
            "Outlook Email connector bridge requires exactly one prepared attachment"
        )
    path = write_connector_handoff(ctx, plan)
    checkpoint(
        ctx, endpoint, plan.source_manifest, "connector_handoff_prepared",
        f"exact connector contract written to {path}",
    )
    return path, plan


def load_handoff(ctx: ReleaseContext, endpoint: str) -> dict:
    path = ctx.specials_dir / "_WORKFLOW" / "connector_handoffs" / f"{endpoint}.json"
    if not path.is_file():
        raise OutlookBridgeError(f"Connector handoff is missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OutlookBridgeError(f"Invalid connector handoff {path}: {exc}") from exc
    if data.get("release_id") != ctx.release_id or data.get("endpoint") != endpoint:
        raise OutlookBridgeError("Connector handoff belongs to another release or endpoint")
    attachments = data.get("attachments") or []
    if len(attachments) != 1:
        raise OutlookBridgeError("Connector handoff must contain exactly one attachment")
    item = attachments[0]
    local = Path(str(item.get("local_path") or ""))
    if not local.is_file():
        raise OutlookBridgeError(f"Connector attachment is missing: {local}")
    from email_deliveries import _sha256
    if local.stat().st_size != int(item.get("size") or -1) or _sha256(local) != item.get("sha256"):
        raise OutlookBridgeError("Connector attachment changed after handoff preparation")
    return data


def _current_plan(ctx: ReleaseContext, endpoint: str) -> EmailPlan:
    # Rebuild and compare the immutable contract before recording any connector
    # result; this catches metadata or signature changes between turns.
    handoff = load_handoff(ctx, endpoint)
    expected_to = "libraries@qwire.com" if endpoint == "qwire" else "patrick.magee@scripps.com"
    expected_subject = (
        f"Universal Production Music - {ctx.delivery_display_folder} Metadata Delivery"
    )
    expected_body = compose_body(load_private_signature())
    if handoff.get("to") != [expected_to] or handoff.get("subject") != expected_subject:
        raise OutlookBridgeError("Current Outlook plan no longer matches its handoff")
    if handoff.get("body") != expected_body:
        raise OutlookBridgeError("Current Outlook signature/body no longer matches its handoff")
    attachment_path = Path(handoff["attachments"][0]["local_path"])
    return EmailPlan(
        endpoint=endpoint,
        to=expected_to,
        subject=expected_subject,
        body=expected_body,
        attachments=collect_manifest(
            attachment_path, allowed_suffixes=frozenset({".csv", ".zip"})
        ),
        source_manifest=collect_manifest(
            ctx.partner_metadata[endpoint], allowed_suffixes=frozenset({".csv"})
        ),
    )


def record_verified_draft(
    ctx: ReleaseContext,
    endpoint: str,
    message_id: str,
) -> Path:
    if not message_id.strip():
        raise OutlookBridgeError("Outlook draft message ID is required")
    plan = _current_plan(ctx, endpoint)
    return checkpoint(
        ctx, endpoint, plan.source_manifest, "connector_draft_verified",
        "connector draft recipients, body, and attachment metadata verified",
        remote_ids=(message_id,),
    )


def authorize_send(
    ctx: ReleaseContext,
    endpoint: str,
    message_id: str,
    confirmation: str | None,
) -> Path:
    require_live_authorization(ctx, confirmation)
    plan = _current_plan(ctx, endpoint)
    prior = load_endpoint_state(ctx, endpoint) or {}
    if prior.get("phase") != "connector_draft_verified":
        raise OutlookBridgeError("Outlook draft has not passed connector verification")
    if prior.get("remote_ids") != [message_id]:
        raise OutlookBridgeError("Authorized Outlook draft ID does not match the checkpoint")
    return checkpoint(
        ctx, endpoint, plan.source_manifest, "send_intent",
        "exact release and verified draft authorized for native Outlook send",
        remote_ids=(message_id,),
    )


def record_verified_sent_item(
    ctx: ReleaseContext,
    endpoint: str,
    draft_id: str,
    sent_message_id: str,
    confirmation: str | None,
) -> Path:
    require_live_authorization(ctx, confirmation)
    if not sent_message_id.strip():
        raise OutlookBridgeError("Sent Items message ID is required")
    plan = _current_plan(ctx, endpoint)
    prior = load_endpoint_state(ctx, endpoint) or {}
    if prior.get("phase") != "send_intent" or prior.get("remote_ids") != [draft_id]:
        raise OutlookBridgeError("No matching authorized send intent exists")
    path = receipt(ctx, endpoint, plan.source_manifest, {
        "draft_message_id": draft_id,
        "sent_message_id": sent_message_id,
        "to": plan.to,
        "subject": plan.subject,
        "attachment": plan.attachments[0].public_dict(),
        "verification": "exact message and attachment confirmed through Outlook Sent Items",
    })
    checkpoint(
        ctx, endpoint, plan.source_manifest, "sent_items_verified",
        "exact Outlook message verified in Sent Items",
        remote_ids=(sent_message_id,),
    )
    set_partner_status(ctx.specials_dir, endpoint, "delivered")
    return path


def _main() -> int:
    parser = argparse.ArgumentParser(description="Bridge Qwire/Scripps handoffs to Outlook connector state")
    parser.add_argument("endpoint", nargs="?", choices=("qwire", "scripps"))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--enroll-signature", action="store_true")
    action.add_argument("--prepare", action="store_true")
    action.add_argument("--inspect", action="store_true")
    action.add_argument("--record-draft", metavar="MESSAGE_ID")
    action.add_argument("--authorize-send", metavar="DRAFT_ID")
    action.add_argument("--record-sent", nargs=2, metavar=("DRAFT_ID", "SENT_MESSAGE_ID"))
    parser.add_argument("--confirm-live-release")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    args = parser.parse_args()
    try:
        if args.enroll_signature:
            print(enroll_private_signature())
            return 0
        if not args.endpoint:
            parser.error("endpoint is required unless --enroll-signature is used")
        ctx = context_from_cli_args(args)
        if args.prepare:
            path, _plan = prepare_handoff(ctx, args.endpoint)
            print(path)
        elif args.inspect:
            print(json.dumps(load_handoff(ctx, args.endpoint), indent=2, sort_keys=True))
        elif args.record_draft:
            print(record_verified_draft(ctx, args.endpoint, args.record_draft))
        elif args.authorize_send:
            print(authorize_send(
                ctx, args.endpoint, args.authorize_send, args.confirm_live_release
            ))
        else:
            draft_id, sent_id = args.record_sent
            print(record_verified_sent_item(
                ctx, args.endpoint, draft_id, sent_id, args.confirm_live_release
            ))
        return 0
    except (OutlookBridgeError, EmailDeliveryError) as exc:
        logging.basicConfig(level=logging.ERROR, format="%(message)s")
        logging.error("Outlook connector bridge failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
