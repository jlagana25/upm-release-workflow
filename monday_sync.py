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

from config import LOGS_DIR, ReleaseContext, context_from_cli_args
from delivery_state import partner_status


MONDAY_API_URL = "https://api.monday.com/v2"
MONDAY_API_VERSION = "2026-07"
MONDAY_BOARD_ID = 1242660947
MONDAY_SUBITEM_BOARD_ID = 1242669161
MONDAY_SOURCE_BOARD_NAME = "UPPM Audio Batch Releases"
MONDAY_AUDIO_BATCH_CARD_ID = "1143680792"
MONDAY_AUTOMATION_TIMEOUT_SECONDS = 5 * 60
MONDAY_AUTOMATION_POLL_SECONDS = 5

GROUP_CONTENT = "Content Updates"
GROUP_SOUNDMOUSE = "SoundMouse Updates"
GROUP_HD = "Hard Drive Updates"

MAIN_STATUS_COLUMN = "status"
SUBITEM_STATUS_COLUMN = "status"

MAIN_FINAL_STATUSES = frozenset({"Done", "Delivered"})
SUBITEM_FINAL_STATUSES = frozenset({
    "Complete", "Done", "Not Needed", "API Client - Not Needed",
})
SUBITEM_IGNORE_STATUSES = frozenset({"Not Needed", "API Client - Not Needed"})

# Labels are verified against the live schema before writes.  The numeric value
# is the stable Monday label ID (passed in the API's confusingly named `index`
# field), not its mutable visual order.
REQUIRED_MAIN_LABELS = {
    "Prepping Content": 4,
    "Ready to Close": 8,
    "Stuck": 2,
    "Delivered": 10,
}
REQUIRED_SUBITEM_LABELS = {
    "Clear to Send": 3,
    "Complete": 18,
    "Stuck": 2,
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
}

CONTENT_GATES = {
    "sourceaudio": ("11 SourceAudio", "15 Final metadata check"),
    "sourceaudio_exus": ("11 SourceAudio", "15 Final metadata check"),
    "soundexchange": ("10 SoundExchange forms", "15 Final metadata check"),
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
    def validate_source_schema(self) -> SourceBoardSchema: ...
    def fetch_source_batch(
        self, schema: SourceBoardSchema, batch: str
    ) -> list[SourceItem]: ...
    def create_source_group(self, schema: SourceBoardSchema, title: str) -> str: ...
    def create_source_item(
        self, schema: SourceBoardSchema, group_id: str, row: SourceRow
    ) -> int: ...
    def set_source_item_values(
        self, schema: SourceBoardSchema, item_id: int, row: SourceRow, *, include_master: bool
    ) -> None: ...


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
            columns(ids: ["status"]) { id title type settings_str }
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

        self._validate_status_labels(parent_columns["status"], REQUIRED_MAIN_LABELS)
        self._validate_status_labels(child_columns["status"], REQUIRED_SUBITEM_LABELS)

    def validate_source_schema(self) -> SourceBoardSchema:
        """Discover and validate the source board by its exact name."""
        matches: list[Mapping[str, Any]] = []
        # This account can see several thousand boards. Page through the full
        # accessible inventory instead of assuming the source board appears in
        # the first 1,000 results.
        for page in range(1, 21):
            data = self._request(
                """
                query SourceBoards($page: Int!) {
                  boards(limit: 500, page: $page, state: active) {
                    id name
                  }
                }
                """,
                {"page": page},
            )
            boards = data.get("boards") or []
            matches.extend(
                board for board in boards
                if str(board.get("name") or "") == MONDAY_SOURCE_BOARD_NAME
            )
            if len(boards) < 500:
                break
        if len(matches) != 1:
            raise MondayError(
                f"Expected exactly one active Monday board named "
                f"{MONDAY_SOURCE_BOARD_NAME!r}; found {len(matches)}"
            )
        board_id = int(matches[0]["id"])
        data = self._request(
            """
            query SourceBoardSchema($board: [ID!]!) {
              boards(ids: $board) { id name columns { id title type } }
            }
            """,
            {"board": [board_id]},
        )
        boards = data.get("boards") or []
        if len(boards) != 1 or str(boards[0].get("name") or "") != MONDAY_SOURCE_BOARD_NAME:
            raise MondayError("Monday source board disappeared during schema validation")
        board = boards[0]
        by_title: dict[str, tuple[str, str]] = {}
        duplicates: set[str] = set()
        for column in board.get("columns") or []:
            title = str(column.get("title") or "").strip()
            if title in by_title:
                duplicates.add(title)
            by_title[title] = (
                str(column.get("id") or ""), str(column.get("type") or "")
            )
        required = {
            "Batch", "Catalog", "Release Date", "LabelId", "Album Code",
            "Album Title", "Digital Fulfillment", "Batch Master",
        }
        missing = sorted(required - set(by_title))
        duplicate_required = sorted(required & duplicates)
        if missing or duplicate_required:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if duplicate_required:
                details.append("duplicate " + ", ".join(duplicate_required))
            raise MondayError("Monday source-board schema mismatch: " + "; ".join(details))
        return SourceBoardSchema(board_id, by_title)

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
        self, schema: SourceBoardSchema, group_id: str, row: SourceRow
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
                "values": self._source_values(schema, row, include_master=False),
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
                column_values(ids: ["status"]) { id text }
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
    state_key = PARTNER_STATE_KEYS.get(partner)
    state = partner_status(ctx.specials_dir, state_key) if state_key else "pending"
    if state == "delivered":
        return "Complete"
    if gate == "failed":
        return "Stuck"
    if state == "uploaded" or gate == "ready":
        return "Clear to Send"
    return None


def _change_if_allowed(
    changes: list[StatusChange],
    item: BoardSubitem,
    desired: str | None,
) -> None:
    if not desired or item.status == desired or item.status in SUBITEM_FINAL_STATUSES:
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


def _main_status(item: BoardItem, changes: list[StatusChange]) -> str | None:
    if item.status in MAIN_FINAL_STATUSES:
        return None
    statuses = [
        _planned_status(child, changes)
        for child in item.subitems
        if _planned_status(child, changes) not in SUBITEM_IGNORE_STATUSES
    ]
    if any(status == "Stuck" for status in statuses):
        return "Stuck"
    if statuses and all(status in {"Complete", "Done"} for status in statuses):
        return "Delivered"
    if statuses and all(
        status in {"Clear to Send", "Complete", "Done"} for status in statuses
    ):
        return "Ready to Close"
    if changes:
        return "Prepping Content"
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
    missing = sorted(set(CONTENT_PARTNERS) - set(children))
    if missing:
        raise MondayError("Content Updates item is missing subitems: " + ", ".join(missing))
    changes: list[StatusChange] = []
    for name, partner in CONTENT_PARTNERS.items():
        gate = _gate_state(results, CONTENT_GATES.get(partner, DEFAULT_CONTENT_GATE))
        _change_if_allowed(
            changes,
            children[name],
            _desired_package_status(ctx, partner, gate),
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
        "Stuck" if gate == "failed" else "Clear to Send" if gate == "ready" else None
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
            _change_if_allowed(changes, children[name], "Stuck")
    if gate == "failed":
        _change_if_allowed(changes, children["Upload to SoundMouse"], "Stuck")
        return changes
    if gate == "ready":
        for name in (
            "Download Media from UniSync", "Export Metadata", "Export Album Covers",
        ):
            _change_if_allowed(changes, children[name], "Complete")
        _change_if_allowed(changes, children["Upload to SoundMouse"], "Clear to Send")
    if state == "uploaded":
        _change_if_allowed(changes, children["Upload to SoundMouse"], "Complete")
        _change_if_allowed(
            changes, children["Process Metadata in SoundMouse"], "Clear to Send"
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


def _required_destination_shape(items: list[BoardItem], batch: str) -> None:
    content = _require_single(items, GROUP_CONTENT, batch)
    hd = _require_single(items, GROUP_HD, batch)
    soundmouse = _require_single(items, GROUP_SOUNDMOUSE, batch)
    required = {
        content.id: set(CONTENT_PARTNERS),
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
        if triggered and set(existing) != set(expected):
            raise MondayError(
                "Monday source batch was already triggered but is incomplete; "
                "refusing to trigger a second automation run"
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
        item_ids: dict[str, int] = {}
        for row in expected_rows:
            current = existing.get(row.work_grouping_id)
            if current:
                item_id, actual = current.item_id, current.row
                if not _same_source_row(actual, row):
                    gateway.set_source_item_values(
                        schema, item_id, row, include_master=False
                    )
            else:
                item_id = gateway.create_source_item(schema, group_id, row)
            item_ids[row.work_grouping_id] = item_id

        master = next(row for row in expected_rows if row.batch_master)
        if not triggered:
            gateway.set_source_item_values(
                schema, item_ids[master.work_grouping_id], master,
                include_master=True,
            )
            logger.info(
                f"  ✓ Loaded {len(expected_rows)} source row(s); Batch Master "
                "was written last."
            )
        else:
            logger.info("  ✓ Monday source batch was already loaded and triggered.")

        deadline = time.monotonic() + timeout_seconds
        while True:
            destination = gateway.fetch_batch(batch)
            try:
                _required_destination_shape(destination, batch)
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
        requested_batches = list(dict.fromkeys([month, sm_batch]))
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
        if compact_batch:
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
        if not changes:
            logger.info("  ✓ Monday statuses already match the workflow state.")
            return True
        for change in changes:
            prefix = "subitem" if change.is_subitem else "main item"
            logger.info(
                f"  {'[DRY RUN] ' if dry_run else ''}{prefix} {change.item_name}: "
                f"{change.old_status or '(blank)'} → {change.new_status}"
            )
        if dry_run:
            return True
        for change in changes:
            gateway.set_status(change)
        confirmed = _status_index([
            item
            for batch in requested_batches
            for item in gateway.fetch_batch(batch)
        ])
        unconfirmed = [
            change for change in changes
            if confirmed.get(change.item_id) != change.new_status
        ]
        if unconfirmed:
            names = ", ".join(change.item_name for change in unconfirmed)
            raise MondayError(
                "Monday post-write verification did not confirm: " + names
            )
        logger.info(f"  ✓ Updated {len(changes)} Monday status value(s).")
        return True
    except MondayError as exc:
        logger.error(f"  ✗ Monday synchronization failed closed: {exc}")
        return False


def _main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize workflow state to Monday")
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=(1, 2))
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--full-month-content", action="store_true")
    parser.add_argument(
        "--monday-batch",
        help="Override derived batch (legacy YYYYMM or compact UPMYYYYMMDD)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    ctx = context_from_cli_args(args)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("monday_sync")
    # Standalone mode intentionally uses delivery_state only; prep gates remain
    # unchanged unless a structured orchestrator result is supplied in-process.
    ok = run_monday_sync(
        ctx, {}, dry_run=args.dry_run, logger=logger,
        batch_override=args.monday_batch,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
