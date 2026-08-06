"""
logging_utils.py — UPM Release Workflow Logging
================================================
Creates a timestamped log file under the configured log directory and
returns a configured logger that writes DEBUG+ to file and INFO+ to stdout.

Usage:
    from logging_utils import get_logger
    logger, log_path = get_logger(year=2026, month=5, part=1)
    logger.info("Starting workflow…")
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from config import LOGS_DIR

_DEFAULT_LOG_DIR = LOGS_DIR

_FORMATTER = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Registry so we don't add duplicate handlers if called more than once
_ACTIVE_LOGGERS: dict[str, tuple[logging.Logger, Path]] = {}


def get_logger(
    year: int,
    month: int,
    part: int,
    log_dir: Path | None = None,
    *,
    release_label: str | None = None,
) -> tuple[logging.Logger, Path]:
    """
    Create (or retrieve) the logger for one workflow run.

    Returns
    -------
    logger   : logging.Logger  — write to this throughout the run
    log_path : Path            — path to the created log file
    """
    if log_dir is None:
        log_dir = _DEFAULT_LOG_DIR

    variant = release_label or f"P{part}"
    key = f"{year}-{month:02d}-{variant}"
    if key in _ACTIVE_LOGGERS:
        return _ACTIVE_LOGGERS[key]

    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.chmod(0o700)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"UPM_Workflow_{year}-{month:02d}_{variant}_{timestamp}.log"
    log_path = log_dir / log_filename

    logger = logging.getLogger(f"upm_workflow.{year}.{month:02d}.{variant}")
    logger.setLevel(logging.DEBUG)

    # --- file handler: everything ---
    fh = logging.FileHandler(log_path, encoding="utf-8")
    log_path.chmod(0o600)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FORMATTER)

    # --- console handler: INFO and above ---
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(_FORMATTER)

    logger.addHandler(fh)
    logger.addHandler(ch)

    _ACTIVE_LOGGERS[key] = (logger, log_path)

    logger.info(f"Log file: {log_path}")
    return logger, log_path


def log_section(logger: logging.Logger, title: str) -> None:
    """Print a visible section divider to make long logs scannable."""
    bar = "=" * 60
    logger.info(bar)
    logger.info(f"  {title}")
    logger.info(bar)


# Tracks the most recently started step so the orchestrator's top-level
# exception handler can report WHERE an unexpected error occurred even if the
# step itself didn't catch it.
_CURRENT_STEP: str = "(not started)"


def current_step() -> str:
    """Return a label for the most recently started step."""
    return _CURRENT_STEP


def log_step_start(logger: logging.Logger, step: int, description: str) -> None:
    global _CURRENT_STEP
    _CURRENT_STEP = f"Step {step} — {description}"
    logger.info(f"▶  STEP {step} START — {description}")


def log_step_end(
    logger: logging.Logger,
    step: int,
    description: str,
    success: bool,
) -> None:
    status = "OK" if success else "FAILED"
    symbol = "✓" if success else "✗"
    logger.info(f"{symbol}  STEP {step} {status} — {description}")
    if not success:
        logger.error(f"   Step {step} did not complete successfully.")


def log_step_skipped(logger: logging.Logger, step: int, description: str) -> None:
    logger.info(f"—  STEP {step} SKIPPED — {description}")


def summarise_results(
    results: dict[str, str],
    log_path: Path,
    logger: logging.Logger,
) -> None:
    """Print the final human-readable summary table."""
    log_section(logger, "UPM Release Workflow — Final Summary")
    max_key = max(len(k) for k in results) if results else 20
    for key, val in results.items():
        logger.info(f"  {key:<{max_key}}  {val}")
    logger.info(f"\n  Log file: {log_path}")
