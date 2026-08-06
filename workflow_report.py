"""Structured JSON report for every orchestrator run."""

from __future__ import annotations

import json
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from config import LOGS_DIR, ReleaseContext


def _count_files(path: Path, suffixes: tuple[str, ...]) -> int:
    if not path.exists():
        return 0
    wanted = {suffix.lower() for suffix in suffixes}
    return sum(
        1 for item in path.rglob("*")
        if item.is_file() and item.suffix.lower() in wanted
    )


def _diagnostics_from_log(log_path: Path) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    try:
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "  WARNING " in line or "  ERROR " in line:
                level = "error" if "  ERROR " in line else "warning"
                diagnostics.append({"level": level, "message": line})
    except OSError:
        pass
    return diagnostics


def write_workflow_report(
    ctx: ReleaseContext,
    args: Any,
    results: Any,
    log_path: Path,
    started_at: datetime,
) -> Path:
    finished_at = datetime.now().astimezone()
    report_dir = LOGS_DIR / "reports" / ctx.release_id
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"run-{finished_at.strftime('%Y%m%d-%H%M%S')}.json"
    steps = {
        key: {"status": status, "detail": detail}
        for key, (status, detail) in results.items()
    }
    payload = {
        "schema_version": 1,
        "release": {
            "id": ctx.release_id,
            "year": ctx.year,
            "month": ctx.month,
            "part": ctx.part,
            "full_month": ctx.is_full_month,
            "start": ctx.release_start,
            "end": ctx.release_end,
        },
        "run": {
            "host": socket.gethostname().split(".")[0],
            "command": sys.argv,
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": finished_at.isoformat(timespec="seconds"),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
            "dry_run": bool(getattr(args, "dry_run", False)),
            "overall": "failed" if results.any_failed() else "completed",
        },
        "steps": steps,
        "artifacts": {
            "log": str(log_path),
            "missing_report": str(ctx.missing_report_csv),
            "soundmouse_missing_report": str(ctx.soundmouse_validation_report),
        },
        "output_counts": {
            "sourceaudio_us_aiff": _count_files(ctx.partner_dirs["sourceaudio_music"], (".aif", ".aiff")),
            "sourceaudio_exus_aiff": _count_files(ctx.partner_dirs["sourceaudio_exus_music"], (".aif", ".aiff")),
            "nbc_wav": _count_files(ctx.partner_dirs["nbc_wav_music"], (".wav",)),
            "soundmouse_wav": _count_files(ctx.soundmouse_release_dir / "MEDIA", (".wav",)),
        },
        "diagnostics": _diagnostics_from_log(log_path),
    }
    temporary = report_path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(report_path)
    return report_path
