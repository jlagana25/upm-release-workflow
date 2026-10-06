#!/usr/bin/env python3
"""Synchronize release preparation state to the UPPM Monday board.

The local workflow uses a per-user Monday personal API token stored in macOS
Keychain.  Tokens never enter argv, logs, reports, URLs, or repository files.
All writes are planned and validated before the first mutation is sent.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

import requests

from config import (
    LOGS_DIR,
    ReleaseContext,
    context_from_cli_args,
    monthly_metadata_delivery_ready,
    rolling_monthly_delivery_date,
)
from delivery_state import partner_status


MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_API_VERSION = "2026-07"
MONDAY_BOARD_ID = 1242660947
MONDAY_SUBITEM_BOARD_ID = 1242669161
MONDAY_SOURCE_BOARD_ID = 5981022568
MONDAY_SOURCE_BOARD_NAME = "UPPM Audio Batch Releases"
MONDAY_AUDIO_BATCH_CARD_ID = "1143680792"
MONDAY_AUTOMATION_TIMEOUT_SECONDS = 5 * 60
MONDAY_AUTOMATION_POLL_SECONDS = 5

SOURCE_COLUMN_SPECS = {
    # Logical field: (stable Monday column ID, expected title, expected type)
    "Batch": ("text", "Batch", "text"),
    "Catalog": ("text_mm6sxbha", "Catalog", "text"),
    "Release Date": ("date_mm6sxypv", "Release Date", "date"),
    "LabelId": ("text4", "LabelID", "text"),
    "Album Code": ("text7", "Album Code", "text"),
    "Album Title": ("text6", "Album Title", "text"),
    "Digital Fulfillment": ("status0", "Digital Fulfillment", "status"),
    "Batch Master": ("status1", "Batch Master", "status"),
}

GROUP_CONTENT = "Content Updates"
GROUP_SOUNDMOUSE = "SoundMouse Updates"
GROUP_HD = "Hard Drive Updates"

MAIN_STATUS_COLUMN = "status"
SUBITEM_STATUS_COLUMN = "status"
SUBITEM_NEXT_ACTION_COLUMN = "status3"

MAIN_FINAL_STATUSES = frozenset({"Done", "Delivered"})
SUBITEM_FINAL_STATUSES = frozenset({
    "Complete", "Done", "Not Required", "Managed by API",
})
SUBITEM_IGNORE_STATUSES = frozenset({"Not Required", "Managed by API"})

# Labels are verified against the live schema before writes.  The numeric value
# is the stable Monday label ID (passed in the API's confusingly named `index`
# field), not its mutable visual order.
REQUIRED_MAIN_LABELS = {
    "Preparing Content": 4,
    "Ready to Deliver": 8,
    "Blocked": 2,
    "Delivered": 10,
}
REQUIRED_SUBITEM_LABELS = {
    "In Progress": 0,
    "Scheduled Monthly": 101,
    "Ready to Deliver": 3,
    "Not Required": 13,
    "Complete": 18,
    "Blocked": 2,
}
REQUIRED_NEXT_ACTION_LABELS = {
    "Automation Running": 0,
    "Resolve Blocker": 2,
    "Revise Package": 3,
    "Prepare Package": 4,
    "Ship / Dispatch": 6,
    "Wait for Schedule": 7,
    "Run Automated Delivery": 8,
    "Upload Audio Manually": 9,
    "No Action Needed": 10,
    "Send / Notify Client": 11,
    "Manual Delivery Required": 12,
    "Process Metadata": 13,
    "Awaiting External Processing": 14,
}

CONTENT_PARTNERS = {
    "UPM Japan - TSS & JMD Metadata (Album Date Format YYYY/MM/DD)": "japan_jmdtss",
    "UPM Japan - NTT DATA": "japan_ntt",
    "SourceAudio": "sourceaudio",
    "SourceAudio (Ex-US)": "sourceaudio_exus",
    "Netmix": "netmix",
    "TuneSat (+Bruton & Kosinus)": "tunesat",
    "ESPN": "espn",
    "SynchTank": "synchtank",
    "Discovery": "discovery",
    "Scripps (Metadata only)": "scripps",
    "QWire (Metadata only)": "qwire",
    "SoundExchange (Metadata only)": "soundexchange",
    "BMAT": "bmat",
}

CONTENT_GATES = {
    "sourceaudio": ("11 SourceAudio", "15 Final metadata check"),
    "sourceaudio_exus": ("11 SourceAudio", "15 Final metadata check"),
    "soundexchange": ("10 SoundExchange forms", "15 Final metadata check"),
    "bmat": ("17 BMAT",),
}
DEFAULT_CONTENT_GATE = ("10 Final packaging", "15 Final metadata check")
HD_GATE = ("9 Verification", "10 Final packaging")

GATE_RESULT_KEYS = frozenset({
    *DEFAULT_CONTENT_GATE,
    *HD_GATE,
    *(key for keys in CONTENT_GATES.values() for key in keys),
    "16 SoundMouse",
    "16 SoundMouse media",
    "16 SoundMouse metadata",
    "16 SoundMouse covers",
    "17 BMAT",
})

SOUNDMOUSE_PROGRESS_GATES = {
    "Download Media from UniSync": "16 SoundMouse media",
    "Export Metadata": "16 SoundMouse metadata",
    "Export Album Covers": "16 SoundMouse covers",
}

PARTNER_STATE_KEYS = {
    "japan_jmdtss": "japan_jmdtss",
    "japan_ntt": "japan_ntt",
    "sourceaudio": "sourceaudio",
    "sourceaudio_exus": "sourceaudio_exus",
    "netmix": "netmix",
    "tunesat": "tunesat",
    "espn": "espn",
    "synchtank": "synchtank",
    "discovery": "discovery",
    "scripps": "scripps",
    "qwire": "qwire",
    "soundexchange": "soundexchange",
}

MONTHLY_METADATA_PARTNERS = frozenset({
    "japan_ntt",
    "japan_jmdtss",
    "qwire",
    "scripps",
})
MONTHLY_PENDING_STATUS = "Scheduled Monthly"
RETIRED_CONTENT_SUBITEMS = frozenset({
    "NBC",
    "MTV/Viacom (Metadata only)",
})


class MondayError(RuntimeError):
    """A fail-closed Monday validation or API error."""


class MondayAuthorizationError(MondayError):
    """The supplied token was rejected by Monday."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class BoardSubitem:
    id: int
    name: str
    status: str
    next_action: str = ""


@dataclass(frozen=True)
class BoardItem:
    id: int
    name: str
    group: str
    batch: str
    delivery_type: str
    status: str
    subitems: tuple[BoardSubitem, ...]


@dataclass(frozen=True)
class StatusChange:
    item_id: int
    item_name: str
    board_id: int
    old_status: str
    new_status: str
    is_subitem: bool


@dataclass(frozen=True)
class NextActionChange:
    item_id: int
    item_name: str
    old_action: str
    new_action: str


@dataclass(frozen=True)
class SourceRow:
    work_grouping_id: str
    batch: str
    catalog: str
    release_date: str
    label_id: str
    album_code: str
    album_title: str
    digital_fulfillment: str
    batch_master: str


@dataclass(frozen=True)
class SourceBoardSchema:
    board_id: int
    columns: Mapping[str, tuple[str, str]]


@dataclass(frozen=True)
class SourceItem:
    item_id: int
    group_id: str
    row: SourceRow


class MondayGateway(Protocol):
    def validate_schema(self) -> None: ...
    def fetch_batch(self, batch: str) -> list[BoardItem]: ...
    def set_status(self, change: StatusChange) -> None: ...
    def set_next_action(self, change: NextActionChange) -> None: ...
    def validate_source_schema(self) -> SourceBoardSchema: ...
    def fetch_source_batch(
        self, schema: SourceBoardSchema, batch: str
    ) -> list[SourceItem]: ...
    def create_source_group(self, schema: SourceBoardSchema, title: str) -> str: ...
    def create_source_item(
        self, schema: SourceBoardSchema, group_id: str, row: SourceRow, *,
        include_master: bool = False,
    ) -> int: ...
    def set_source_item_values(
        self, schema: SourceBoardSchema, item_id: int, row: SourceRow, *, include_master: bool
    ) -> None: ...
    def repair_soundmouse_destination(
        self, batch: str, items: list[BoardItem]
    ) -> bool: ...
    def remove_rolling_monthly_subitems(
        self, batch: str, items: list[BoardItem], *, keep_monthly: bool = False
    ) -> int: ...
    def ensure_rolling_bmat_subitem(
        self, batch: str, items: list[BoardItem]
    ) -> bool: ...


def _column_text(values: Iterable[Mapping[str, Any]], column_id: str) -> str:
    for value in values:
        if value.get("id") == column_id:
            return str(value.get("text") or "").strip()
    return ""


class MondayClient:
    """Small GraphQL client with no credential persistence of its own."""

    def __init__(self, token: str, *, session=None, timeout: int = 30) -> None:
        normalized = token.strip()
        if normalized.casefold().startswith("bearer "):
            normalized = normalized[7:].strip()
        if not normalized:
            raise MondayError("Monday API token is empty")
        self._token = normalized
        self._authorization = normalized
        self._session = session or requests.Session()
        self._timeout = timeout

    def _request(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        include_api_version: bool = True,
    ) -> dict[str, Any]:
        try:
            response = None
            # monday's GraphQL examples use the raw personal token, while its
            # hosted-token documentation uses Bearer. Prefer GraphQL's native
            # form, but safely retry a rejected request with Bearer; a 401 means
            # the first request was not executed.
            candidates = [self._authorization]
            bearer = f"Bearer {self._token}"
            if self._authorization != bearer:
                candidates.append(bearer)
            for authorization in candidates:
                headers = {
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                    "User-Agent": "UPM-Release-Workflow/1.0",
                }
                if include_api_version:
                    headers["API-Version"] = MONDAY_API_VERSION
                response = self._session.post(
                    MONDAY_API_URL,
                    headers=headers,
                    json={"query": query, "variables": dict(variables)},
                    timeout=self._timeout,
                )
                if response.status_code != 401:
                    self._authorization = authorization
                    break
            assert response is not None
            if response.status_code in (401, 403):
                raise MondayAuthorizationError(
                    f"Monday returned HTTP {response.status_code} for the API token",
                    status_code=response.status_code,
                )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise MondayError(f"Monday API request failed: {exc}") from exc
        if payload.get("errors"):
            messages = "; ".join(
                str(error.get("message", "unknown GraphQL error"))
                for error in payload["errors"]
            )
            raise MondayError(f"Monday GraphQL error: {messages}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MondayError("Monday API returned no data")
        return data

    def validate_auth(self) -> None:
        """Confirm that the token identifies a Monday user without logging it."""
        # Match monday's authentication documentation exactly: the basic `me`
        # probe has no version-specific fields, so do not add API-Version here.
        data = self._request(
            "query CurrentUser { me { id } }", {}, include_api_version=False
        )
        if not (data.get("me") or {}).get("id"):
            raise MondayAuthorizationError("Monday did not identify the API token")

    def validate_schema(self) -> None:
        query = """
        query BoardSchema($board: [ID!]!, $subitems: [ID!]!) {
          parent: boards(ids: $board) {
            id name
            groups { id title }
            columns(ids: ["status", "batch", "status_1"]) { id title type settings_str }
          }
          children: boards(ids: $subitems) {
            id
            columns(ids: ["status", "status3"]) { id title type settings_str }
          }
        }
        """
        data = self._request(
            query,
            {"board": [MONDAY_BOARD_ID], "subitems": [MONDAY_SUBITEM_BOARD_ID]},
        )
        parents = data.get("parent") or []
        children = data.get("children") or []
        if len(parents) != 1 or len(children) != 1:
            raise MondayError("Expected parent and subitem boards were not found")
        group_titles = {str(group.get("title")) for group in parents[0].get("groups", [])}
        required_groups = {GROUP_CONTENT, GROUP_SOUNDMOUSE, GROUP_HD}
        missing_groups = sorted(required_groups - group_titles)
        if missing_groups:
            raise MondayError("Monday board is missing groups: " + ", ".join(missing_groups))

        parent_columns = {column["id"]: column for column in parents[0].get("columns", [])}
        for column_id in ("status", "batch", "status_1"):
            if column_id not in parent_columns:
                raise MondayError(f"Monday board is missing column {column_id!r}")
        child_columns = {column["id"]: column for column in children[0].get("columns", [])}
        if "status" not in child_columns:
            raise MondayError("Monday subitem board is missing its Status column")
        if "status3" not in child_columns:
            raise MondayError("Monday subitem board is missing its Next Action column")
        if str(child_columns["status3"].get("title") or "") != "Next Action":
            raise MondayError("Monday subitem column status3 must be named 'Next Action'")

        self._validate_status_labels(parent_columns["status"], REQUIRED_MAIN_LABELS)
        self._validate_status_labels(child_columns["status"], REQUIRED_SUBITEM_LABELS)
        self._validate_status_labels(
            child_columns["status3"], REQUIRED_NEXT_ACTION_LABELS
        )

    def validate_source_schema(self) -> SourceBoardSchema:
        """Validate the exact source board and its stable column IDs."""
        data = self._request(
            """
            query SourceBoardSchema($board: [ID!]!) {
              boards(ids: $board) { id name columns { id title type } }
            }
            """,
            {"board": [MONDAY_SOURCE_BOARD_ID]},
        )
        boards = data.get("boards") or []
        if len(boards) != 1 or str(boards[0].get("name") or "") != MONDAY_SOURCE_BOARD_NAME:
            raise MondayError("Monday source board ID/name validation failed")
        board = boards[0]
        by_id = {str(column.get("id") or ""): column for column in board.get("columns") or []}
        resolved: dict[str, tuple[str, str]] = {}
        mismatches: list[str] = []
        for logical, (column_id, title, column_type) in SOURCE_COLUMN_SPECS.items():
            actual = by_id.get(column_id)
            if not actual:
                mismatches.append(f"missing {column_id} ({logical})")
                continue
            actual_title = str(actual.get("title") or "").strip()
            actual_type = str(actual.get("type") or "")
            if actual_title != title or actual_type != column_type:
                mismatches.append(
                    f"{column_id}: expected {title}/{column_type}, "
                    f"found {actual_title}/{actual_type}"
                )
                continue
            resolved[logical] = (column_id, column_type)
        if mismatches:
            raise MondayError("Monday source-board schema mismatch: " + "; ".join(mismatches))
        return SourceBoardSchema(MONDAY_SOURCE_BOARD_ID, resolved)

    @staticmethod
    def _source_values(
        schema: SourceBoardSchema,
        row: SourceRow,
        *,
        include_master: bool,
    ) -> str:
        values = {
            "Batch": row.batch,
            "Catalog": row.catalog,
            "Release Date": row.release_date,
            "LabelId": row.label_id,
            "Album Code": row.album_code,
            "Album Title": row.album_title,
            "Digital Fulfillment": row.digital_fulfillment,
        }
        if include_master:
            values["Batch Master"] = row.batch_master
        encoded: dict[str, Any] = {}
        for title, value in values.items():
            if not value:
                continue
            column_id, column_type = schema.columns[title]
            if column_type == "date":
                encoded[column_id] = {"date": value[:10]}
            elif column_type == "status":
                encoded[column_id] = {"label": value}
            else:
                encoded[column_id] = value
        return json.dumps(encoded)

    def fetch_source_batch(
        self, schema: SourceBoardSchema, batch: str
    ) -> list[SourceItem]:
        batch_column = schema.columns["Batch"][0]
        data = self._request(
            """
            query SourceBatch($board: ID!, $column: String!, $batch: String!) {
              items_page_by_column_values(
                board_id: $board,
                limit: 500,
                columns: [{column_id: $column, column_values: [$batch]}]
              ) {
                items { id name group { id } column_values { id text } }
              }
            }
            """,
            {"board": schema.board_id, "column": batch_column, "batch": batch},
        )
        by_id = {
            column_id: title
            for title, (column_id, _column_type) in schema.columns.items()
        }
        result: list[SourceItem] = []
        page = data.get("items_page_by_column_values") or {}
        for item in page.get("items") or []:
            text = {
                by_id.get(str(value.get("id") or ""), ""): str(value.get("text") or "").strip()
                for value in item.get("column_values") or []
            }
            result.append(SourceItem(
                item_id=int(item["id"]),
                group_id=str((item.get("group") or {}).get("id") or ""),
                row=SourceRow(
                work_grouping_id=str(item.get("name") or "").strip(),
                batch=text.get("Batch", ""),
                catalog=text.get("Catalog", ""),
                release_date=text.get("Release Date", "")[:10],
                label_id=text.get("LabelId", ""),
                album_code=text.get("Album Code", ""),
                album_title=text.get("Album Title", ""),
                digital_fulfillment=text.get("Digital Fulfillment", ""),
                batch_master=text.get("Batch Master", ""),
            )))
        return result

    def create_source_group(self, schema: SourceBoardSchema, title: str) -> str:
        data = self._request(
            """
            mutation CreateSourceGroup($board: ID!, $name: String!) {
              create_group(board_id: $board, group_name: $name) { id title }
            }
            """,
            {"board": schema.board_id, "name": title},
        )
        group = data.get("create_group") or {}
        if str(group.get("title") or "") != title or not group.get("id"):
            raise MondayError("Monday did not confirm creation of the source-board group")
        return str(group["id"])

    def create_source_item(
        self, schema: SourceBoardSchema, group_id: str, row: SourceRow, *,
        include_master: bool = False,
    ) -> int:
        data = self._request(
            """
            mutation CreateSourceItem(
              $board: ID!, $group: String!, $name: String!, $values: JSON!
            ) {
              create_item(
                board_id: $board, group_id: $group, item_name: $name,
                column_values: $values
              ) { id }
            }
            """,
            {
                "board": schema.board_id,
                "group": group_id,
                "name": row.work_grouping_id,
                "values": self._source_values(
                    schema, row, include_master=include_master
                ),
            },
        )
        item = data.get("create_item") or {}
        if not item.get("id"):
            raise MondayError(f"Monday did not create source row {row.work_grouping_id}")
        return int(item["id"])

    def set_source_item_values(
        self,
        schema: SourceBoardSchema,
        item_id: int,
        row: SourceRow,
        *,
        include_master: bool,
    ) -> None:
        data = self._request(
            """
            mutation SetSourceValues($board: ID!, $item: ID!, $values: JSON!) {
              change_multiple_column_values(
                board_id: $board, item_id: $item, column_values: $values
              ) { id }
            }
            """,
            {
                "board": schema.board_id,
                "item": item_id,
                "values": self._source_values(
                    schema, row, include_master=include_master
                ),
            },
        )
        changed = data.get("change_multiple_column_values") or {}
        if str(changed.get("id")) != str(item_id):
            raise MondayError(f"Monday did not confirm source row {row.work_grouping_id}")

    @staticmethod
    def _validate_status_labels(column: Mapping[str, Any], expected: Mapping[str, int]) -> None:
        try:
            settings = json.loads(str(column.get("settings_str") or "{}"))
        except json.JSONDecodeError as exc:
            raise MondayError("Monday Status settings are invalid JSON") from exc
        labels = settings.get("labels", {})
        if isinstance(labels, list):
            actual = {str(entry.get("label")): int(entry.get("id")) for entry in labels}
        else:
            actual = {str(label): int(label_id) for label_id, label in labels.items()}
        for label, label_id in expected.items():
            if actual.get(label) != label_id:
                raise MondayError(
                    f"Monday Status label changed: expected {label!r} to have ID {label_id}"
                )

    def fetch_batch(self, batch: str) -> list[BoardItem]:
        query = """
        query BatchItems($board: ID!, $batch: String!) {
          items_page_by_column_values(
            board_id: $board,
            limit: 50,
            columns: [{column_id: "batch", column_values: [$batch]}]
          ) {
            items {
              id name
              group { title }
              column_values(ids: ["status", "batch", "status_1"]) { id text }
              subitems {
                id name
                column_values(ids: ["status", "status3"]) { id text }
              }
            }
          }
        }
        """
        data = self._request(query, {"board": MONDAY_BOARD_ID, "batch": batch})
        page = data.get("items_page_by_column_values") or {}
        result: list[BoardItem] = []
        for item in page.get("items") or []:
            columns = item.get("column_values") or []
            subitems = tuple(
                BoardSubitem(
                    id=int(child["id"]),
                    name=str(child.get("name") or ""),
                    status=_column_text(child.get("column_values") or [], "status"),
                    next_action=_column_text(
                        child.get("column_values") or [], "status3"
                    ),
                )
                for child in item.get("subitems") or []
            )
            result.append(BoardItem(
                id=int(item["id"]),
                name=str(item.get("name") or ""),
                group=str((item.get("group") or {}).get("title") or ""),
                batch=_column_text(columns, "batch"),
                delivery_type=_column_text(columns, "status_1"),
                status=_column_text(columns, "status"),
                subitems=subitems,
            ))
        return result

    def set_status(self, change: StatusChange) -> None:
        label_map = REQUIRED_SUBITEM_LABELS if change.is_subitem else REQUIRED_MAIN_LABELS
        if change.new_status not in label_map:
            raise MondayError(f"Refusing unknown Monday status {change.new_status!r}")
        query = """
        mutation SetStatus($board: ID!, $item: ID!, $values: JSON!) {
          change_multiple_column_values(
            board_id: $board,
            item_id: $item,
            column_values: $values
          ) { id }
        }
        """
        values = json.dumps({"status": {"index": label_map[change.new_status]}})
        data = self._request(
            query,
            {"board": change.board_id, "item": change.item_id, "values": values},
        )
        changed = data.get("change_multiple_column_values") or {}
        if str(changed.get("id")) != str(change.item_id):
            raise MondayError(f"Monday did not confirm status update for {change.item_name}")

    def set_next_action(self, change: NextActionChange) -> None:
        if change.new_action not in REQUIRED_NEXT_ACTION_LABELS:
            raise MondayError(
                f"Refusing unknown Monday next action {change.new_action!r}"
            )
        query = """
        mutation SetNextAction($board: ID!, $item: ID!, $values: JSON!) {
          change_multiple_column_values(
            board_id: $board,
            item_id: $item,
            column_values: $values
          ) { id }
        }
        """
        values = json.dumps({
            SUBITEM_NEXT_ACTION_COLUMN: {
                "index": REQUIRED_NEXT_ACTION_LABELS[change.new_action]
            }
        })
        data = self._request(
            query,
            {
                "board": MONDAY_SUBITEM_BOARD_ID,
                "item": change.item_id,
                "values": values,
            },
        )
        changed = data.get("change_multiple_column_values") or {}
        if str(changed.get("id")) != str(change.item_id):
            raise MondayError(
                f"Monday did not confirm next action update for {change.item_name}"
            )

    def repair_soundmouse_destination(
        self, batch: str, items: list[BoardItem]
    ) -> bool:
        """Repair only the automation's uniquely identifiable misplaced item.

        The source-board recipe currently creates the SoundMouse-typed parent
        in Reports.  Monday's public API cannot edit the recipe itself, so move
        that exact generated item and ensure its fixed five-child shape.  Never
        invent a missing parent or choose among ambiguous candidates.
        """
        expected_children = {
            "Download Media from UniSync", "Export Metadata",
            "Export Album Covers", "Upload to SoundMouse",
            "Process Metadata in SoundMouse",
        }
        in_group = [item for item in items if item.group == GROUP_SOUNDMOUSE]
        candidates = [
            item for item in items
            if item.delivery_type == "SoundMouse (SM)"
        ]
        changed = False
        if not in_group:
            if len(candidates) != 1:
                return False
            candidate = candidates[0]
            data = self._request(
                """
                query DestinationGroups($board: [ID!]!) {
                  boards(ids: $board) { groups { id title } }
                }
                """,
                {"board": [MONDAY_BOARD_ID]},
            )
            groups = [
                group
                for group in (data.get("boards") or [{}])[0].get("groups", [])
                if str(group.get("title") or "") == GROUP_SOUNDMOUSE
            ]
            if len(groups) != 1:
                raise MondayError(
                    f"Expected exactly one Monday group named {GROUP_SOUNDMOUSE!r}"
                )
            moved = self._request(
                """
                mutation MoveSoundMouse($item: ID!, $group: String!) {
                  move_item_to_group(item_id: $item, group_id: $group) { id }
                }
                """,
                {"item": candidate.id, "group": str(groups[0]["id"])},
            )
            if str((moved.get("move_item_to_group") or {}).get("id")) != str(candidate.id):
                raise MondayError("Monday did not confirm the SoundMouse group repair")
            changed = True
            items = self.fetch_batch(batch)
            in_group = [item for item in items if item.group == GROUP_SOUNDMOUSE]

        if len(in_group) != 1:
            return changed
        parent = in_group[0]
        if parent.delivery_type != "SoundMouse (SM)":
            raise MondayError("SoundMouse group item has the wrong delivery type")
        existing = {child.name for child in parent.subitems}
        for name in sorted(expected_children - existing):
            created = self._request(
                """
                mutation CreateSoundMouseSubitem(
                  $parent: ID!, $name: String!, $values: JSON!
                ) {
                  create_subitem(
                    parent_item_id: $parent, item_name: $name,
                    column_values: $values
                  ) { id }
                }
                """,
                {
                    "parent": parent.id,
                    "name": name,
                    "values": json.dumps({
                        SUBITEM_STATUS_COLUMN: {
                            "index": REQUIRED_SUBITEM_LABELS["In Progress"]
                        }
                    }),
                },
            )
            if not (created.get("create_subitem") or {}).get("id"):
                raise MondayError(f"Monday did not create SoundMouse subitem {name!r}")
            changed = True
        return changed

    def remove_rolling_monthly_subitems(
        self, batch: str, items: list[BoardItem], *, keep_monthly: bool = False
    ) -> int:
        """Remove monthly-only partners from an automation-created rolling item."""
        content = _require_single(items, GROUP_CONTENT, batch)
        monthly_names = {
            name
            for name, partner in CONTENT_PARTNERS.items()
            if partner in MONTHLY_METADATA_PARTNERS
        }
        matches = [
            child for child in content.subitems if child.name in monthly_names
        ]
        duplicate_names = sorted({
            child.name for child in matches
            if sum(candidate.name == child.name for candidate in matches) > 1
        })
        if duplicate_names:
            raise MondayError(
                "Rolling Content Updates item has duplicate monthly subitems: "
                + ", ".join(duplicate_names)
            )
        if keep_monthly:
            missing = sorted(monthly_names - {child.name for child in matches})
            if missing:
                raise MondayError(
                    "Month-owning rolling item is missing monthly subitems: "
                    + ", ".join(missing)
                )
            return 0
        for child in matches:
            data = self._request(
                """
                mutation DeleteMonthlySubitem($item: ID!) {
                  delete_item(item_id: $item) { id }
                }
                """,
                {"item": child.id},
            )
            deleted = data.get("delete_item") or {}
            if str(deleted.get("id")) != str(child.id):
                raise MondayError(
                    f"Monday did not confirm removal of monthly subitem {child.name!r}"
                )
        if matches:
            confirmed = _require_single(
                self.fetch_batch(batch), GROUP_CONTENT, batch
            )
            remaining = sorted(
                child.name
                for child in confirmed.subitems
                if child.name in monthly_names
            )
            if remaining:
                raise MondayError(
                    "Monday still contains rolling monthly subitems: "
                    + ", ".join(remaining)
                )
        return len(matches)

    def ensure_rolling_bmat_subitem(
        self, batch: str, items: list[BoardItem]
    ) -> bool:
        """Add BMAT to a rolling Content item when the source recipe omits it."""
        content = _require_single(items, GROUP_CONTENT, batch)
        matches = [child for child in content.subitems if child.name == "BMAT"]
        if len(matches) > 1:
            raise MondayError("Rolling Content Updates item has duplicate BMAT subitems")
        if matches:
            return False
        created = self._request(
            """
            mutation CreateBmatSubitem($parent: ID!, $values: JSON!) {
              create_subitem(
                parent_item_id: $parent,
                item_name: "BMAT",
                column_values: $values
              ) { id }
            }
            """,
            {
                "parent": content.id,
                "values": json.dumps({
                    SUBITEM_STATUS_COLUMN: {
                        "index": REQUIRED_SUBITEM_LABELS["In Progress"]
                    },
                    SUBITEM_NEXT_ACTION_COLUMN: {
                        "index": REQUIRED_NEXT_ACTION_LABELS["Automation Running"]
                    },
                }),
            },
        )
        if not (created.get("create_subitem") or {}).get("id"):
            raise MondayError("Monday did not confirm BMAT subitem creation")
        return True

    def ensure_monthly_batch(self, ctx: ReleaseContext) -> BoardItem:
        """Create or resume the standalone monthly Content Updates item."""
        if not ctx.is_monthly_delivery:
            raise MondayError("Refusing to create a monthly batch for a release context")
        batch = monday_batch_key(ctx)
        existing = self.fetch_batch(batch)
        content = [item for item in existing if item.group == GROUP_CONTENT]
        if len(content) > 1:
            raise MondayError(
                f"Expected at most one monthly Content Updates item for {batch}; "
                f"found {len(content)}"
            )
        if content:
            item = content[0]
        else:
            data = self._request(
                """
                query MonthlyGroup($board: [ID!]!) {
                  boards(ids: $board) { groups { id title } }
                }
                """,
                {"board": [MONDAY_BOARD_ID]},
            )
            groups = [
                group
                for group in (data.get("boards") or [{}])[0].get("groups", [])
                if str(group.get("title") or "") == GROUP_CONTENT
            ]
            if len(groups) != 1:
                raise MondayError(
                    f"Expected exactly one Monday group named {GROUP_CONTENT!r}"
                )
            values = json.dumps({
                "batch": batch,
                MAIN_STATUS_COLUMN: {"index": REQUIRED_MAIN_LABELS["Preparing Content"]},
            })
            created = self._request(
                """
                mutation CreateMonthlyItem(
                  $board: ID!, $group: String!, $name: String!, $values: JSON!
                ) {
                  create_item(
                    board_id: $board, group_id: $group,
                    item_name: $name, column_values: $values
                  ) { id }
                }
                """,
                {
                    "board": MONDAY_BOARD_ID,
                    "group": str(groups[0]["id"]),
                    "name": f"{batch} - Monthly Delivery",
                    "values": values,
                },
            )
            item_id = int((created.get("create_item") or {}).get("id") or 0)
            if not item_id:
                raise MondayError("Monday did not confirm monthly item creation")
            item = BoardItem(
                item_id,
                f"{batch} - Monthly Delivery",
                GROUP_CONTENT,
                batch,
                "",
                "Preparing Content",
                (),
            )

        required = {
            name
            for name, partner in CONTENT_PARTNERS.items()
            if partner in MONTHLY_METADATA_PARTNERS
        }
        children = {child.name: child for child in item.subitems}
        duplicate_names = [
            name for name in required
            if sum(child.name == name for child in item.subitems) > 1
        ]
        if duplicate_names:
            raise MondayError(
                "Monthly item has duplicate subitems: " + ", ".join(duplicate_names)
            )
        for name in sorted(required - set(children)):
            created = self._request(
                """
                mutation CreateMonthlySubitem(
                  $parent: ID!, $name: String!, $values: JSON!
                ) {
                  create_subitem(
                    parent_item_id: $parent,
                    item_name: $name,
                    column_values: $values
                  ) { id }
                }
                """,
                {
                    "parent": item.id,
                    "name": name,
                    "values": json.dumps({
                        SUBITEM_STATUS_COLUMN: {
                            "index": REQUIRED_SUBITEM_LABELS["In Progress"]
                        }
                    }),
                },
            )
            if not (created.get("create_subitem") or {}).get("id"):
                raise MondayError(f"Monday did not confirm monthly subitem {name!r}")

        confirmed = [
            candidate
            for candidate in self.fetch_batch(batch)
            if candidate.group == GROUP_CONTENT
        ]
        if len(confirmed) != 1:
            raise MondayError("Monday did not confirm the monthly Content Updates item")
        confirmed_names = {child.name for child in confirmed[0].subitems}
        if not required.issubset(confirmed_names):
            raise MondayError("Monday did not confirm every monthly delivery subitem")
        return confirmed[0]


def monday_batch_key(ctx: ReleaseContext, override: str | None = None) -> str:
    """Return the exact destination-board batch key for this workflow run.

    Any context with a compact ``UPMYYYYMMDD`` release ID uses that exact value
    in all three destination groups.  This includes the August 2026 Part 2
    bridge, whose client-facing name remains unchanged.  Older month/part runs
    retain the source automation's ``YYYYMM`` convention.
    """
    if override:
        if not re.fullmatch(r"(?:\d{6}|UPM\d{8})", override):
            raise MondayError(
                "--monday-batch must be YYYYMM (legacy) or UPMYYYYMMDD"
            )
        return override
    if ctx.is_monthly_delivery:
        return ctx.monthly_monday_batch
    if re.fullmatch(r"UPM\d{8}", ctx.release_id):
        return ctx.release_id
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+(\d{4})\b",
        ctx.client_delivery_label,
    )
    if not match:
        raise MondayError(
            f"Could not derive Monday batch from {ctx.client_delivery_label!r}; "
            "pass --monday-batch YYYYMM"
        )
    month_number = datetime.strptime(match.group(1), "%B").month
    return f"{match.group(2)}{month_number:02d}"


def monday_batch_month(ctx: ReleaseContext, override: str | None = None) -> str:
    """Backward-compatible alias for callers using the former helper name."""
    return monday_batch_key(ctx, override)


def monday_release_part(ctx: ReleaseContext) -> int:
    """Return the source-board Release Part that drives Monday automations."""
    if ctx.full_month_content:
        return ctx.part
    elif ctx.is_full_month:
        return 1
    elif ctx.is_date_range:
        return 1 if int(ctx.release_start[-2:]) <= 14 else 2
    return ctx.part


def soundmouse_batch(ctx: ReleaseContext, month_batch: str) -> str:
    if re.fullmatch(r"UPM\d{8}", month_batch):
        return month_batch
    return f"{month_batch} -{monday_release_part(ctx)}"


def _result_status(results: Any, key: str) -> str:
    if hasattr(results, "status"):
        return str(results.status(key) or "")
    value = results.get(key, "") if isinstance(results, Mapping) else ""
    if isinstance(value, tuple):
        value = value[0]
    return str(value).split(" — ", 1)[0]


def _gate_state(results: Any, keys: Iterable[str]) -> str:
    states = [_result_status(results, key) for key in keys]
    if any(state == "failed" or state.startswith("blocked") for state in states):
        return "failed"
    if states and all(state == "completed" for state in states):
        return "ready"
    return "unchanged"


def _results_with_history(ctx: ReleaseContext, current: Any) -> Mapping[str, str]:
    """Fill skipped/missing recovery results from recent real-run reports.

    A normal full run supplies current in-memory results.  A later ``--only 18``
    recovery has only skipped entries, so scan newest reports until each gate's
    most recent non-skipped result is found.  A recent failure therefore wins
    over an older success; dry-run reports are never accepted as readiness
    evidence.
    """
    merged: dict[str, str] = {}
    for key in GATE_RESULT_KEYS:
        status = _result_status(current, key)
        if status and status != "skipped":
            merged[key] = status
    unresolved = set(GATE_RESULT_KEYS) - set(merged)
    report_dir = Path(LOGS_DIR) / "reports" / ctx.release_id
    if not unresolved or not report_dir.is_dir():
        return merged
    for path in sorted(report_dir.glob("run-*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if bool((payload.get("run") or {}).get("dry_run")):
            continue
        report_steps = payload.get("steps") or {}
        for key in tuple(unresolved):
            entry = report_steps.get(key) or {}
            status = str(entry.get("status") or "")
            if status and status != "skipped":
                merged[key] = status
                unresolved.remove(key)
        if not unresolved:
            break
    return merged


def _desired_package_status(
    ctx: ReleaseContext,
    partner: str,
    gate: str,
) -> str | None:
    if partner == "bmat":
        if gate == "failed":
            return "Blocked"
        if gate == "ready":
            # Step 17 owns BMAT packaging and SFTP submission end to end;
            # completed includes the valid no-new-releases no-op.
            return "Complete"
        return "In Progress"
    state_key = PARTNER_STATE_KEYS.get(partner)
    state = partner_status(ctx.specials_dir, state_key) if state_key else "pending"
    if state in {"uploaded", "delivered"}:
        return "Complete"
    if partner in MONTHLY_METADATA_PARTNERS:
        if ctx.is_monthly_delivery:
            if gate == "failed":
                return "Blocked"
            if gate == "ready":
                return "Ready to Deliver"
            return "In Progress"
        if not getattr(ctx, "monthly_metadata_due", True):
            return MONTHLY_PENDING_STATUS
        if (
            hasattr(ctx, "monthly_metadata_delivery_date")
            and not monthly_metadata_delivery_ready(ctx)
        ):
            if gate == "failed":
                return "Blocked"
            if gate == "ready":
                return MONTHLY_PENDING_STATUS
            return "In Progress"
    if gate == "failed":
        return "Blocked"
    if gate == "ready":
        return "Ready to Deliver"
    return None


def _change_if_allowed(
    changes: list[StatusChange],
    item: BoardSubitem,
    desired: str | None,
    *,
    reopen_monthly_not_needed: bool = False,
) -> None:
    if not desired or item.status == desired:
        return
    if item.status in SUBITEM_FINAL_STATUSES and not (
        reopen_monthly_not_needed and item.status == "Not Required"
    ):
        return
    changes.append(StatusChange(
        item_id=item.id,
        item_name=item.name,
        board_id=MONDAY_SUBITEM_BOARD_ID,
        old_status=item.status,
        new_status=desired,
        is_subitem=True,
    ))


def _planned_status(item: BoardSubitem, changes: Iterable[StatusChange]) -> str:
    for change in changes:
        if change.item_id == item.id:
            return change.new_status
    return item.status


def _next_action_for(item_name: str, status: str) -> str:
    """Translate lifecycle state into the next concrete operator action."""
    if status in SUBITEM_FINAL_STATUSES:
        return "No Action Needed"
    if status == "Blocked":
        return "Resolve Blocker"
    if status == "Needs Revision":
        return "Revise Package"
    if status == "Scheduled Monthly":
        return "Wait for Schedule"
    if status in {"In Progress", "Delivering"}:
        return (
            "Automation Running"
            if status == "In Progress"
            else "Awaiting External Processing"
        )
    if status != "Ready to Deliver":
        return "Prepare Package"

    if item_name.startswith("SourceAudio"):
        return "Upload Audio Manually"
    if item_name == "Discovery":
        return "Wait for Schedule"
    if item_name in {"MP3", "WAV"}:
        return "Ship / Dispatch"
    if item_name == "Process Metadata in SoundMouse":
        return "Process Metadata"
    if item_name in {
        "UPM Japan - TSS & JMD Metadata (Album Date Format YYYY/MM/DD)",
        "UPM Japan - NTT DATA",
        "Scripps (Metadata only)",
        "QWire (Metadata only)",
    }:
        return "Send / Notify Client"
    return "Run Automated Delivery"


def build_next_action_plan(
    items_by_batch: Mapping[str, list[BoardItem]],
    status_changes: Iterable[StatusChange],
    *,
    eligible_subitem_ids: set[int] | None = None,
) -> list[NextActionChange]:
    """Plan the companion Next Action value for every fetched subitem."""
    planned = {
        change.item_id: change.new_status
        for change in status_changes
        if change.is_subitem
    }
    changes: list[NextActionChange] = []
    seen: set[int] = set()
    for items in items_by_batch.values():
        for parent in items:
            for child in parent.subitems:
                if (
                    eligible_subitem_ids is not None
                    and child.id not in eligible_subitem_ids
                ):
                    continue
                if child.id in seen:
                    continue
                seen.add(child.id)
                desired = _next_action_for(
                    child.name, planned.get(child.id, child.status)
                )
                if desired != child.next_action:
                    changes.append(NextActionChange(
                        item_id=child.id,
                        item_name=child.name,
                        old_action=child.next_action,
                        new_action=desired,
                    ))
    return changes


def _main_status(item: BoardItem, changes: list[StatusChange]) -> str | None:
    if item.status in MAIN_FINAL_STATUSES:
        return None
    statuses = [
        _planned_status(child, changes)
        for child in item.subitems
        if _planned_status(child, changes) not in SUBITEM_IGNORE_STATUSES
    ]
    if any(status == "Blocked" for status in statuses):
        return "Blocked"
    if statuses and all(status in {"Complete", "Done"} for status in statuses):
        return "Delivered"
    if statuses and all(
        status in {"Ready to Deliver", "Complete", "Done"} for status in statuses
    ):
        return "Ready to Deliver"
    if changes:
        return "Preparing Content"
    return None


def _require_single(items: list[BoardItem], group: str, batch: str) -> BoardItem:
    matches = [item for item in items if item.group == group and item.batch == batch]
    if len(matches) != 1:
        raise MondayError(
            f"Expected exactly one {group!r} item with batch {batch!r}; found {len(matches)}"
        )
    return matches[0]


def _content_changes(ctx: ReleaseContext, item: BoardItem, results: Any) -> list[StatusChange]:
    children = {child.name: child for child in item.subitems}
    managed_partners = {
        name: partner
        for name, partner in CONTENT_PARTNERS.items()
        if (
            partner in MONTHLY_METADATA_PARTNERS
            if ctx.is_monthly_delivery
            else partner not in MONTHLY_METADATA_PARTNERS
        )
    }
    missing = sorted(set(managed_partners) - set(children))
    if missing:
        raise MondayError("Content Updates item is missing subitems: " + ", ".join(missing))
    changes: list[StatusChange] = []
    if not ctx.is_monthly_delivery:
        for name in RETIRED_CONTENT_SUBITEMS:
            retired = children.get(name)
            if retired is not None:
                _change_if_allowed(changes, retired, "Not Required")
    for name, partner in managed_partners.items():
        gate = _gate_state(results, CONTENT_GATES.get(partner, DEFAULT_CONTENT_GATE))
        desired = _desired_package_status(ctx, partner, gate)
        _change_if_allowed(
            changes,
            children[name],
            desired,
            reopen_monthly_not_needed=(
                partner in MONTHLY_METADATA_PARTNERS
                and desired == MONTHLY_PENDING_STATUS
            ),
        )
    return changes


def _hd_changes(ctx: ReleaseContext, item: BoardItem, results: Any) -> list[StatusChange]:
    children = {child.name: child for child in item.subitems}
    missing = sorted({"MP3", "WAV"} - set(children))
    if missing:
        raise MondayError("Hard Drive Updates item is missing subitems: " + ", ".join(missing))
    state = partner_status(ctx.specials_dir, "hd_updates")
    gate = _gate_state(results, HD_GATE)
    desired = "Complete" if state == "delivered" else (
        "Blocked" if gate == "failed" else "Ready to Deliver" if gate == "ready" else None
    )
    changes: list[StatusChange] = []
    for name in ("MP3", "WAV"):
        _change_if_allowed(changes, children[name], desired)
    return changes


def _soundmouse_changes(
    ctx: ReleaseContext,
    item: BoardItem,
    results: Any,
) -> list[StatusChange]:
    required = {
        "Download Media from UniSync", "Export Metadata", "Export Album Covers",
        "Upload to SoundMouse", "Process Metadata in SoundMouse",
    }
    children = {child.name: child for child in item.subitems}
    missing = sorted(required - set(children))
    if missing:
        raise MondayError("SoundMouse item is missing subitems: " + ", ".join(missing))
    state = partner_status(ctx.specials_dir, "soundmouse")
    gate = _gate_state(results, ("16 SoundMouse",))
    changes: list[StatusChange] = []
    if state == "delivered":
        for name in required:
            _change_if_allowed(changes, children[name], "Complete")
        return changes
    for name, result_key in SOUNDMOUSE_PROGRESS_GATES.items():
        phase = _result_status(results, result_key)
        if phase == "completed":
            _change_if_allowed(changes, children[name], "Complete")
        elif phase == "failed":
            _change_if_allowed(changes, children[name], "Blocked")
    if gate == "failed":
        _change_if_allowed(changes, children["Upload to SoundMouse"], "Blocked")
        return changes
    if gate == "ready":
        for name in (
            "Download Media from UniSync", "Export Metadata", "Export Album Covers",
        ):
            _change_if_allowed(changes, children[name], "Complete")
        _change_if_allowed(changes, children["Upload to SoundMouse"], "Ready to Deliver")
    if state == "uploaded":
        _change_if_allowed(changes, children["Upload to SoundMouse"], "Complete")
        _change_if_allowed(
            changes, children["Process Metadata in SoundMouse"], "Ready to Deliver"
        )
    return changes


def build_status_plan(
    ctx: ReleaseContext,
    results: Any,
    items_by_batch: Mapping[str, list[BoardItem]],
    *,
    batch_override: str | None = None,
) -> list[StatusChange]:
    month = monday_batch_key(ctx, batch_override)
    if ctx.is_monthly_delivery:
        content = _require_single(items_by_batch.get(month, []), GROUP_CONTENT, month)
        item_changes = _content_changes(ctx, content, results)
        changes = list(item_changes)
        desired_main = _main_status(content, item_changes)
        if desired_main and desired_main != content.status:
            changes.append(StatusChange(
                item_id=content.id,
                item_name=content.name,
                board_id=MONDAY_BOARD_ID,
                old_status=content.status,
                new_status=desired_main,
                is_subitem=False,
            ))
        return changes
    sm_batch = soundmouse_batch(ctx, month)
    monthly_items = items_by_batch.get(month, [])
    soundmouse_items = items_by_batch.get(sm_batch, [])
    soundmouse = _require_single(soundmouse_items, GROUP_SOUNDMOUSE, sm_batch)

    changes: list[StatusChange] = []
    managed_items = [(soundmouse, _soundmouse_changes)]
    # Legacy source automation creates Content, Hard Drive, and SoundMouse for
    # Part 1, but only SoundMouse for Part 2.  The replacement automation uses
    # all three groups whenever the workflow has a compact UPMYYYYMMDD key,
    # including the August 2026 Part 2 bridge.
    if re.fullmatch(r"UPM\d{8}", month) or monday_release_part(ctx) == 1:
        content = _require_single(monthly_items, GROUP_CONTENT, month)
        hd = _require_single(monthly_items, GROUP_HD, month)
        managed_items[0:0] = [
            (content, _content_changes),
            (hd, _hd_changes),
        ]
    for item, builder in managed_items:
        item_changes = builder(ctx, item, results)
        changes.extend(item_changes)
        desired_main = _main_status(item, item_changes)
        if desired_main and desired_main != item.status:
            changes.append(StatusChange(
                item_id=item.id,
                item_name=item.name,
                board_id=MONDAY_BOARD_ID,
                old_status=item.status,
                new_status=desired_main,
                is_subitem=False,
            ))
    return changes


def _status_index(items: Iterable[BoardItem]) -> dict[int, str]:
    statuses: dict[int, str] = {}
    for item in items:
        statuses[item.id] = item.status
        statuses.update({child.id: child.status for child in item.subitems})
    return statuses


_SOURCE_HEADERS = (
    "WorkGroupingId", "Batch", "Catalog", "Release Date", "LabelId",
    "Album Code", "Album Title", "Digital Fulfillment", "Batch Master",
)


def load_source_rows(path: Path, batch: str) -> tuple[SourceRow, ...]:
    """Load and normalize the Domo Audio Batch export for one workflow."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = tuple(reader.fieldnames or ())
            missing = sorted(set(_SOURCE_HEADERS) - set(headers))
            if missing:
                raise MondayError(
                    "Domo Audio Batch export is missing columns: " + ", ".join(missing)
                )
            raw_rows = list(reader)
    except OSError as exc:
        raise MondayError(f"Could not read Domo Audio Batch export: {exc}") from exc
    if not raw_rows:
        raise MondayError("Domo Audio Batch export contains no release rows")

    by_id: dict[str, SourceRow] = {}
    masters = 0
    for number, raw in enumerate(raw_rows, start=2):
        work_id = str(raw.get("WorkGroupingId") or "").strip()
        if not work_id:
            raise MondayError(f"Domo Audio Batch row {number} has no WorkGroupingId")
        exported_batch = str(raw.get("Batch") or "").strip()
        if exported_batch and exported_batch != batch:
            raise MondayError(
                f"Domo Audio Batch row {number} belongs to {exported_batch}, not {batch}"
            )
        master = str(raw.get("Batch Master") or "").strip()
        if master:
            masters += 1
        row = SourceRow(
            work_grouping_id=work_id,
            batch=batch,
            catalog=str(raw.get("Catalog") or "").strip(),
            release_date=str(raw.get("Release Date") or "").strip()[:10],
            label_id=str(raw.get("LabelId") or "").strip(),
            album_code=str(raw.get("Album Code") or "").strip(),
            album_title=str(raw.get("Album Title") or "").strip(),
            digital_fulfillment=str(raw.get("Digital Fulfillment") or "").strip(),
            batch_master=master,
        )
        required_values = {
            "Catalog": row.catalog,
            "Release Date": row.release_date,
            "LabelId": row.label_id,
            "Album Code": row.album_code,
            "Album Title": row.album_title,
            "Digital Fulfillment": row.digital_fulfillment,
        }
        blank = sorted(name for name, value in required_values.items() if not value)
        if blank:
            raise MondayError(
                f"Domo Audio Batch row {number} has blank fields: {', '.join(blank)}"
            )
        previous = by_id.get(work_id)
        if previous and previous != row:
            raise MondayError(f"Conflicting Domo Audio Batch rows for {work_id}")
        by_id[work_id] = row
    if masters > 1:
        raise MondayError("Domo Audio Batch export has more than one Batch Master row")
    rows = list(by_id.values())
    if masters == 0:
        rows[0] = SourceRow(**{
            **rows[0].__dict__, "batch_master": "Batch Master",
        })
    return tuple(rows)


def _required_destination_shape(
    items: list[BoardItem], batch: str, *, include_monthly: bool = False
) -> None:
    content = _require_single(items, GROUP_CONTENT, batch)
    hd = _require_single(items, GROUP_HD, batch)
    soundmouse = _require_single(items, GROUP_SOUNDMOUSE, batch)
    required = {
        content.id: {
            name
            for name, partner in CONTENT_PARTNERS.items()
            if include_monthly or partner not in MONTHLY_METADATA_PARTNERS
        },
        hd.id: {"MP3", "WAV"},
        soundmouse.id: {
            "Download Media from UniSync", "Export Metadata", "Export Album Covers",
            "Upload to SoundMouse", "Process Metadata in SoundMouse",
        },
    }
    by_id = {item.id: item for item in (content, hd, soundmouse)}
    for item_id, names in required.items():
        missing = sorted(names - {child.name for child in by_id[item_id].subitems})
        if missing:
            raise MondayError(
                f"{by_id[item_id].group} item is missing subitems: " + ", ".join(missing)
            )


def _same_source_row(actual: SourceRow, expected: SourceRow) -> bool:
    return (
        actual.work_grouping_id == expected.work_grouping_id
        and actual.batch == expected.batch
        and actual.catalog == expected.catalog
        and actual.release_date[:10] == expected.release_date[:10]
        and actual.label_id == expected.label_id
        and actual.album_code == expected.album_code
        and actual.album_title == expected.album_title
        and actual.digital_fulfillment == expected.digital_fulfillment
    )


def run_monday_source_preflight(
    ctx: ReleaseContext,
    *,
    dry_run: bool,
    logger: logging.Logger,
    gateway: MondayGateway | None = None,
    timeout_seconds: int = MONDAY_AUTOMATION_TIMEOUT_SECONDS,
    poll_seconds: float = MONDAY_AUTOMATION_POLL_SECONDS,
) -> bool:
    """Export/load the source batch, then prove its automation finished."""
    batch = monday_batch_key(ctx)
    if not re.fullmatch(r"UPM\d{8}", batch):
        logger.info("  Monday source-board preflight is not required for legacy batches.")
        return True
    try:
        if gateway is None:
            from auth_manager import load_monday_keychain_token
            token = load_monday_keychain_token()
            if not token:
                raise MondayError(
                    "Monday API token is not enrolled. Run: python3 auth_manager.py "
                    "--enroll-monday-keychain"
                )
            gateway = MondayClient(token)
        gateway.validate_schema()
        schema = gateway.validate_source_schema()
        if dry_run:
            logger.info(
                "  [DRY RUN] Would refresh/export Domo Audio Batch card "
                f"{MONDAY_AUDIO_BATCH_CARD_ID}, load {batch} into "
                f"{MONDAY_SOURCE_BOARD_NAME}, trigger Batch Master last, and "
                "verify all three destination items through the Monday API."
            )
            return True

        from domo_exports import run_domo_exports
        card = {
            "key": "monday_audio_batch",
            "card_id": MONDAY_AUDIO_BATCH_CARD_ID,
            "description": "Domo Audio Batch",
            "output_fn": lambda release: release.monday_audio_batch_csv,
        }
        exported = run_domo_exports(
            ctx, False, logger, card_configs=[card]
        )
        if exported.get("monday_audio_batch") != "ok":
            raise MondayError("Domo Audio Batch export did not complete")
        expected_rows = load_source_rows(ctx.monday_audio_batch_csv, batch)
        expected = {row.work_grouping_id: row for row in expected_rows}
        existing_items = gateway.fetch_source_batch(schema, batch)
        existing = {item.row.work_grouping_id: item for item in existing_items}
        if len(existing) != len(existing_items):
            raise MondayError("Monday source board has duplicate WorkGroupingId rows")
        unexpected = sorted(set(existing) - set(expected))
        if unexpected:
            raise MondayError(
                "Monday source batch contains unexpected rows: " + ", ".join(unexpected)
            )
        triggered = any(item.row.batch_master for item in existing_items)
        missing_source_ids = set(expected) - set(existing)
        if triggered and missing_source_ids:
            logger.warning(
                "  ↻ Monday source batch was triggered before the refreshed "
                f"Domo export was complete; backfilling {len(missing_source_ids)} "
                "non-master row(s) without retriggering the automation."
            )

        if not existing:
            suffix = datetime.fromisoformat(ctx.release_start).strftime("%y%m%d")
            group_id = gateway.create_source_group(
                schema, f"UPPM Audio Batch {suffix}"
            )
        else:
            group_ids = {item.group_id for item in existing_items}
            if len(group_ids) != 1 or not next(iter(group_ids)):
                raise MondayError(
                    "Existing Monday source rows do not belong to one source group"
                )
            group_id = next(iter(group_ids))
        master = next(row for row in expected_rows if row.batch_master)
        # Monday's active recipe is creation-triggered: the one Batch Master
        # item must be created last with its trigger status already present.
        # Setting that status in a follow-up mutation does not run the recipe.
        ordered_rows = tuple(
            row for row in expected_rows if row.work_grouping_id != master.work_grouping_id
        ) + (master,)
        item_ids: dict[str, int] = {}
        for row in ordered_rows:
            current = existing.get(row.work_grouping_id)
            if current:
                item_id, actual = current.item_id, current.row
                if not _same_source_row(actual, row):
                    gateway.set_source_item_values(
                        schema, item_id, row, include_master=False
                    )
            else:
                item_id = gateway.create_source_item(
                    schema,
                    group_id,
                    row,
                    include_master=(
                        not triggered
                        and row.work_grouping_id == master.work_grouping_id
                    ),
                )
            item_ids[row.work_grouping_id] = item_id

        if not triggered:
            logger.info(
                f"  ✓ Loaded {len(expected_rows)} source row(s); Batch Master "
                "was created last with the trigger value."
            )
        elif missing_source_ids:
            logger.info(
                f"  ✓ Backfilled {len(missing_source_ids)} source row(s); "
                "the existing Batch Master was left unchanged."
            )
        else:
            logger.info("  ✓ Monday source batch was already loaded and triggered.")

        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                destination = gateway.fetch_batch(batch)
                if gateway.repair_soundmouse_destination(batch, destination):
                    logger.info(
                        "  ✓ Repaired the automation-created SoundMouse item "
                        "through the Monday API."
                    )
                    destination = gateway.fetch_batch(batch)
                include_monthly = rolling_monthly_delivery_date(ctx) is not None
                removed = gateway.remove_rolling_monthly_subitems(
                    batch, destination, keep_monthly=include_monthly
                )
                if removed:
                    logger.info(
                        "  ✓ Removed %d monthly-only subitem(s) from the rolling "
                        "Content Updates item through the Monday API.",
                        removed,
                    )
                    destination = gateway.fetch_batch(batch)
                if gateway.ensure_rolling_bmat_subitem(batch, destination):
                    logger.info(
                        "  ✓ Added the BMAT delivery subitem through the Monday API."
                    )
                    destination = gateway.fetch_batch(batch)
                _required_destination_shape(
                    destination, batch, include_monthly=include_monthly
                )
                logger.info(
                    "  ✓ Monday source automation created Content Updates, Hard "
                    "Drive Updates, and SoundMouse Updates with required subitems."
                )
                return True
            except MondayError:
                if time.monotonic() >= deadline:
                    raise MondayError(
                        "Monday source automation did not create the complete "
                        f"destination batch {batch} within {timeout_seconds} seconds"
                    )
                time.sleep(max(0.0, poll_seconds))
    except MondayError as exc:
        logger.error(f"  ✗ Monday source-board preflight failed closed: {exc}")
        return False


def run_monday_sync(
    ctx: ReleaseContext,
    results: Any,
    *,
    dry_run: bool,
    logger: logging.Logger,
    batch_override: str | None = None,
    gateway: MondayGateway | None = None,
    include_history: bool = True,
) -> bool:
    try:
        month = monday_batch_key(ctx, batch_override)
        sm_batch = soundmouse_batch(ctx, month)
        if gateway is None:
            from auth_manager import load_monday_keychain_token
            token = load_monday_keychain_token()
            if not token:
                raise MondayError(
                    "Monday API token is not enrolled. Run: python3 auth_manager.py "
                    "--enroll-monday-keychain"
                )
            gateway = MondayClient(token)
        gateway.validate_schema()
        part = monday_release_part(ctx)
        compact_batch = bool(re.fullmatch(r"UPM\d{8}", month))
        requested_batches = (
            [month]
            if ctx.is_monthly_delivery
            else list(dict.fromkeys([month, sm_batch]))
        )
        if not compact_batch and part == 2:
            requested_batches = [sm_batch]
        items_by_batch = {
            batch: gateway.fetch_batch(batch) for batch in requested_batches
        }
        # Mid-run checkpoints must use only this process's observed results;
        # pulling an older completed report could advance unfinished packages.
        # The final/recovery sync keeps history enabled so ``--only 18`` works.
        effective_results = (
            _results_with_history(ctx, results) if include_history else results
        )
        changes = build_status_plan(
            ctx, effective_results, items_by_batch, batch_override=batch_override
        )
        eligible_action_ids = None
        if ctx.is_monthly_delivery:
            monthly_names = {
                name
                for name, partner in CONTENT_PARTNERS.items()
                if partner in MONTHLY_METADATA_PARTNERS
            }
            eligible_action_ids = {
                child.id
                for item in items_by_batch.get(month, [])
                for child in item.subitems
                if child.name in monthly_names
            }
        next_actions = build_next_action_plan(
            items_by_batch,
            changes,
            eligible_subitem_ids=eligible_action_ids,
        )
        if ctx.is_monthly_delivery:
            logger.info(f"  Monday monthly batch mapping: Content={month}")
        elif compact_batch:
            logger.info(
                "  Monday batch mapping: "
                f"Content/HD/SoundMouse={month} (compact workflow ID)"
            )
        elif part == 1:
            logger.info(
                f"  Monday batch mapping: Content/HD={month}; SoundMouse={sm_batch}"
            )
        else:
            logger.info(
                "  Monday batch mapping: Content/HD=not created by source "
                f"automation for Part 2; SoundMouse={sm_batch}"
            )
        if not changes and not next_actions:
            logger.info(
                "  ✓ Monday statuses and next actions already match the workflow state."
            )
            return True
        for change in changes:
            prefix = "subitem" if change.is_subitem else "main item"
            logger.info(
                f"  {'[DRY RUN] ' if dry_run else ''}{prefix} {change.item_name}: "
                f"{change.old_status or '(blank)'} → {change.new_status}"
            )
        for change in next_actions:
            logger.info(
                f"  {'[DRY RUN] ' if dry_run else ''}next action "
                f"{change.item_name}: "
                f"{change.old_action or '(blank)'} → {change.new_action}"
            )
        if dry_run:
            return True
        for change in changes:
            gateway.set_status(change)
        for change in next_actions:
            gateway.set_next_action(change)
        confirmed_items = [
            item
            for batch in requested_batches
            for item in gateway.fetch_batch(batch)
        ]
        confirmed = _status_index(confirmed_items)
        unconfirmed = [
            change for change in changes
            if confirmed.get(change.item_id) != change.new_status
        ]
        if unconfirmed:
            names = ", ".join(change.item_name for change in unconfirmed)
            raise MondayError(
                "Monday post-write verification did not confirm: " + names
            )
        confirmed_actions = {
            child.id: child.next_action
            for item in confirmed_items
            for child in item.subitems
        }
        unconfirmed_actions = [
            change for change in next_actions
            if confirmed_actions.get(change.item_id) != change.new_action
        ]
        if unconfirmed_actions:
            names = ", ".join(change.item_name for change in unconfirmed_actions)
            raise MondayError(
                "Monday next-action verification did not confirm: " + names
            )
        logger.info(
            "  ✓ Updated "
            f"{len(changes)} Monday status value(s) and "
            f"{len(next_actions)} next-action value(s)."
        )
        return True
    except MondayError as exc:
        logger.error(f"  ✗ Monday synchronization failed closed: {exc}")
        return False


def ensure_monday_monthly_batch(
    ctx: ReleaseContext,
    *,
    dry_run: bool,
    logger: logging.Logger,
    gateway: MondayClient | None = None,
) -> bool:
    """Ensure the independent monthly Content Updates item exists."""
    if not ctx.is_monthly_delivery:
        logger.error("  ✗ Refusing monthly Monday setup for a release context")
        return False
    batch = monday_batch_key(ctx)
    if dry_run:
        logger.info(
            "  [DRY RUN] Would ensure monthly Monday batch %s with NTT DATA, "
            "JMD/TSS, Qwire, and Scripps subitems",
            batch,
        )
        return True
    try:
        if gateway is None:
            from auth_manager import load_monday_keychain_token
            token = load_monday_keychain_token()
            if not token:
                raise MondayError(
                    "Monday API token is not enrolled. Run: python3 "
                    "auth_manager.py --enroll-monday-keychain"
                )
            gateway = MondayClient(token)
        gateway.validate_schema()
        item = gateway.ensure_monthly_batch(ctx)
        logger.info("  ✓ Monthly Monday batch ready: %s", item.batch)
        return True
    except MondayError as exc:
        logger.error("  ✗ Monthly Monday batch setup failed closed: %s", exc)
        return False


def adopt_completed_monthly_statuses(
    ctx: ReleaseContext,
    source_batch: str,
    *,
    dry_run: bool,
    logger: logging.Logger,
    gateway: MondayClient | None = None,
) -> bool:
    """Copy four completed monthly subitems into their rolling owner.

    The historical item is intentionally retained as audit history. This only
    updates the rolling batch's subitems and never marks its parent Delivered.
    """
    if not ctx.is_monthly_delivery or not ctx.monthly_rolls_into_batch:
        logger.error("  ✗ Monthly status adoption requires a rolling-owned context")
        return False
    target_batch = monday_batch_key(ctx)
    if source_batch == target_batch:
        logger.error("  ✗ Monthly source and target batches are identical")
        return False
    required = {
        name
        for name, partner in CONTENT_PARTNERS.items()
        if partner in MONTHLY_METADATA_PARTNERS
    }
    if dry_run:
        logger.info(
            "  [DRY RUN] Would copy four completed monthly statuses from %s to %s",
            source_batch,
            target_batch,
        )
        return True
    try:
        if gateway is None:
            from auth_manager import load_monday_keychain_token
            token = load_monday_keychain_token()
            if not token:
                raise MondayError("Monday API token is not enrolled")
            gateway = MondayClient(token)
        gateway.validate_schema()
        source = _require_single(gateway.fetch_batch(source_batch), GROUP_CONTENT, source_batch)
        target = _require_single(gateway.fetch_batch(target_batch), GROUP_CONTENT, target_batch)
        source_children = {child.name: child for child in source.subitems if child.name in required}
        target_children = {child.name: child for child in target.subitems if child.name in required}
        if set(source_children) != required or set(target_children) != required:
            raise MondayError("Monthly source or rolling target is missing required subitems")
        incomplete = sorted(
            name for name, child in source_children.items()
            if child.status not in SUBITEM_FINAL_STATUSES
        )
        if incomplete:
            raise MondayError(
                "Historical monthly subitems are not complete: " + ", ".join(incomplete)
            )
        for name in sorted(required):
            child = target_children[name]
            if child.status not in SUBITEM_FINAL_STATUSES:
                gateway.set_status(StatusChange(
                    child.id, child.name, MONDAY_SUBITEM_BOARD_ID,
                    child.status, "Complete", True,
                ))
            if child.next_action != "No Action Needed":
                gateway.set_next_action(NextActionChange(
                    child.id, child.name, child.next_action, "No Action Needed"
                ))
        confirmed = _require_single(
            gateway.fetch_batch(target_batch), GROUP_CONTENT, target_batch
        )
        confirmed_children = {
            child.name: child for child in confirmed.subitems if child.name in required
        }
        failed = sorted(
            name for name in required
            if name not in confirmed_children
            or confirmed_children[name].status not in SUBITEM_FINAL_STATUSES
            or confirmed_children[name].next_action != "No Action Needed"
        )
        if failed:
            raise MondayError(
                "Monday did not confirm adopted monthly statuses: " + ", ".join(failed)
            )
        logger.info(
            "  ✓ Adopted four completed monthly statuses into %s; historical %s retained",
            target_batch,
            source_batch,
        )
        return True
    except MondayError as exc:
        logger.error("  ✗ Monthly status adoption failed closed: %s", exc)
        return False


def _main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize workflow state to Monday")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--delivery-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument(
        "--monday-batch",
        help="Override derived batch (legacy YYYYMM or compact UPMYYYYMMDD)",
    )
    parser.add_argument(
        "--source-preflight",
        action="store_true",
        help="Load/repair the exact rolling source batch before status sync",
    )
    parser.add_argument(
        "--adopt-completed-monthly-from",
        metavar="BATCH",
        help="copy completed monthly subitems from a historical batch",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    ctx = context_from_cli_args(args)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("monday_sync")
    if args.source_preflight:
        ok = run_monday_source_preflight(
            ctx, dry_run=args.dry_run, logger=logger
        )
        return 0 if ok else 1
    if args.adopt_completed_monthly_from:
        ok = adopt_completed_monthly_statuses(
            ctx,
            args.adopt_completed_monthly_from,
            dry_run=args.dry_run,
            logger=logger,
        )
        return 0 if ok else 1
    # Standalone status mode intentionally uses delivery_state only; prep gates remain
    # unchanged unless a structured orchestrator result is supplied in-process.
    ok = run_monday_sync(
        ctx, {}, dry_run=args.dry_run, logger=logger,
        batch_override=args.monday_batch,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
