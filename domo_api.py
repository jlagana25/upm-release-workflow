"""Credential-safe public Domo Data API helpers used by workflow exports.

Step 1, BMAT, and SoundMouse use explicit DataSet projection contracts and
fail closed on API errors. Browser access is reserved for DataFlow operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from auth_manager import DOMO_API_TOKEN_URL, load_domo_api_keychain_credentials


DOMO_API_BASE = "https://api.domo.com/v1"


class DomoApiError(RuntimeError):
    """Raised without including credentials, tokens, or response bodies."""


@dataclass(frozen=True)
class DomoQueryResult:
    columns: list[str]
    rows: list[list[Any]]

    def dictionaries(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row)) for row in self.rows]


def _access_token(*, timeout: int = 30) -> str:
    import requests

    credentials = load_domo_api_keychain_credentials()
    if not credentials:
        raise DomoApiError("Domo API credentials are not enrolled")
    try:
        response = requests.get(
            DOMO_API_TOKEN_URL,
            params={"grant_type": "client_credentials", "scope": "data"},
            auth=credentials,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
    except (requests.RequestException, TypeError, ValueError) as exc:
        raise DomoApiError("Domo API authentication failed") from exc
    if not token:
        raise DomoApiError("Domo API authentication returned no access token")
    return str(token)


def query_dataset(
    dataset_id: str,
    sql: str,
    *,
    timeout: int = 300,
) -> DomoQueryResult:
    """Execute a read-only Domo DataSet query using the Keychain client pair."""
    import requests

    token = _access_token()
    try:
        response = requests.post(
            f"{DOMO_API_BASE}/datasets/query/execute/{dataset_id}",
            json={"sql": sql},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=(30, timeout),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, TypeError, ValueError) as exc:
        raise DomoApiError(f"Domo DataSet query failed for {dataset_id}") from exc
    columns = payload.get("columns") if isinstance(payload, dict) else None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise DomoApiError(f"Domo DataSet query returned an invalid result for {dataset_id}")
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        raise DomoApiError(f"Domo DataSet query returned malformed rows for {dataset_id}")
    return DomoQueryResult([str(column) for column in columns], rows)
