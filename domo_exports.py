"""
domo_exports.py — Step 1: Export Domo cards (CSV/XLSX)
==========================================
Confirmed working UI flow:
  1. Navigate to card detail (kpis/details/{card_id})
  2. Open timeframe picker (cd-timeframe-label)
  3. Open dropdown (span.db-dropdown-selector.timeframe-options__date-range)
  4. Click 'Between' — confirmed direct top-level option in this Domo instance
  5. Fill start date input (click_count=3 to select all, then type)
  6. Fill end date input (same)
  7. Close picker with page.mouse.click() on neutral card body area
     — DO NOT use Tab or Enter; both navigate away from the card detail view
  8. Wait for card to reload (networkidle)
  9. Click share icon → Send / Export → Excel  (confirmed enabled on card detail)
  10. Save .xlsx → convert to .csv → filter to Part 1/2 window

Card map:
    us_tracklist     1384111004    → …/UPM-US-{token}-Tracklist.csv
    exus_tracklist   1389146023    → …/UPM-ExUS-{token}-Tracklist.csv
    album_list       1741693601    → …/UPM-US-{token}-AlbumList.csv
    japan_metadata   242186821     → specials_dir/…/NTT Data Metadata.csv
    nbc_metadata     233748559     → specials_dir/…/NBCUniversal Metadata Export.csv
    tunesat_metadata 1826988754    → specials_dir/…/Tunesat/Metadata/UPM {month} Metadata.csv
"""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Playwright drives the Domo browser exports (Step 1).  It's imported lazily so
# that modules which only need the non-browser helpers here — e.g.
# verify_exports_exist() (pure file-existence checks), used when Step 1 is
# skipped — can import this module on a machine that doesn't have playwright
# installed.  The browser entry points call _require_playwright() and fail with
# a clear, actionable message if it's genuinely missing.
try:
    from playwright.sync_api import (
        sync_playwright,
        TimeoutError as PlaywrightTimeoutError,
    )
except ModuleNotFoundError:
    sync_playwright = None

    class PlaywrightTimeoutError(Exception):
        """Fallback so `except PlaywrightTimeoutError` clauses stay valid when
        playwright isn't installed; only the browser-driving export path can
        actually raise it."""


def _require_playwright() -> None:
    """Raise a clear error if playwright is needed but not installed."""
    if sync_playwright is None:
        raise RuntimeError(
            "Step 1 (Domo exports) needs playwright, which isn't installed on "
            "this machine.  Install it with:\n"
            "    pip3 install playwright && python3 -m playwright install chromium\n"
            "Or run with --skip-domo if the exports already exist."
        )

from config import DOMO_CARDS, DOMO_INSTANCE, DOMO_PAGE_ID, ReleaseContext, context_from_cli_args

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEMP_DOWNLOAD_DIR = Path.home() / "Downloads" / "_domo_exports_temp"
NAV_WAIT          = 4.0
PICKER_WAIT       = 2.0
DROPDOWN_WAIT     = 1.0
EXPORT_WAIT       = 2.0
LOGIN_TIMEOUT     = 120_000
NAV_TIMEOUT       = 30_000
DOWNLOAD_TIMEOUT  = 90_000

# ---------------------------------------------------------------------------
# Card configuration
# ---------------------------------------------------------------------------

CARD_CONFIGS: list[dict] = [
    {
        "key":         "us_tracklist",
        "card_id":     DOMO_CARDS["us_tracklist"],
        "description": "US Tracklist",
        "output_fn":   lambda ctx: ctx.us_tracklist_csv,
    },
    {
        "key":         "exus_tracklist",
        "card_id":     DOMO_CARDS["exus_tracklist"],
        "description": "Ex-US Tracklist",
        "output_fn":   lambda ctx: ctx.exus_tracklist_csv,
    },
    {
        "key":         "album_list",
        "card_id":     DOMO_CARDS["album_list"],
        "description": "Album List",
        "output_fn":   lambda ctx: ctx.album_list_csv,
    },
    {
        "key":         "japan_metadata",
        "card_id":     DOMO_CARDS["japan_metadata"],
        "description": "Japan Metadata",
        "output_fn":   lambda ctx: ctx.japan_metadata_csv,
    },
    {
        "key":         "nbc_metadata",
        "card_id":     DOMO_CARDS["nbc_metadata"],
        "description": "NBC Metadata",
        "output_fn":   lambda ctx: ctx.nbc_metadata_csv,
    },
    {
        # Step 13 (non-maintrack cleanup) compares the MP3 files in the
        # Tunesat Music folder against this CSV's "File Name" column.
        # Lands directly at ctx.cleanup_metadata_csv, which is the same
        # path Step 13 reads from — so a fresh Step 1 run for this
        # month/part automatically refreshes the keepers list for Step 13.
        "key":         "tunesat_metadata",
        "card_id":     DOMO_CARDS["tunesat_metadata"],
        "description": "Tunesat Metadata",
        "output_fn":   lambda ctx: ctx.cleanup_metadata_csv,
    },

    # ---- Partner deliverable metadata exports (Step 1, after folder setup) --
    # Each lands in its partner's 3-FINAL PACKAGING Metadata folder.  The Domo
    # download is always an XLSX; "format": "csv" converts it (default),
    # "format": "xlsx" keeps the workbook as-is (JMD/TSS deliverable).
    {
        "key":         "netmix_metadata",
        "card_id":     DOMO_CARDS["netmix_metadata"],
        "description": "Netmix Metadata",
        "output_fn":   lambda ctx: ctx.partner_metadata["netmix"],
    },
    {
        "key":         "synchtank_metadata",
        "card_id":     DOMO_CARDS["synchtank_metadata"],
        "description": "SynchTank Metadata",
        "output_fn":   lambda ctx: ctx.partner_metadata["synchtank"],
    },
    {
        "key":         "scripps_metadata",
        "card_id":     DOMO_CARDS["scripps_metadata"],
        "description": "Scripps Metadata",
        "output_fn":   lambda ctx: ctx.partner_metadata["scripps"],
    },
    {
        "key":         "qwire_metadata",
        "card_id":     DOMO_CARDS["qwire_metadata"],
        "description": "Qwire Metadata",
        "output_fn":   lambda ctx: ctx.partner_metadata["qwire"],
    },
    {
        "key":         "japan_jmdtss_metadata",
        "card_id":     DOMO_CARDS["japan_jmdtss_metadata"],
        "description": "Japan JMD/TSS Metadata",
        "output_fn":   lambda ctx: ctx.partner_metadata["japan_jmdtss"],
        "format":      "xlsx",
    },
    {
        "key":         "soundexchange_mgb",
        "card_id":     DOMO_CARDS["soundexchange_mgb"],
        "description": "SoundExchange (Mgb Na Llc)",
        "output_fn":   lambda ctx: ctx.partner_metadata["soundexchange_mgb"],
        "format":      "xlsx",
    },
    {
        "key":         "soundexchange_ztunes",
        "card_id":     DOMO_CARDS["soundexchange_ztunes"],
        "description": "SoundExchange (Z Tunes, Llc)",
        "output_fn":   lambda ctx: ctx.partner_metadata["soundexchange_ztunes"],
        "format":      "xlsx",
    },
]

EXPORT_KEYS = [c["key"] for c in CARD_CONFIGS]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_domo_exports(
    ctx: ReleaseContext,
    dry_run: bool,
    logger: logging.Logger,
    only_keys: Optional[list[str]] = None,
) -> dict[str, str]:
    """Export Domo cards. Returns dict of key → 'ok'|'skipped'|'failed'.

    By default all cards are exported.  Pass `only_keys` (a list of card
    keys, e.g. ["exus_tracklist"]) to export just those — used by
    remediation to refresh a single tracklist that's still missing tracks.
    """
    cards = CARD_CONFIGS
    if only_keys:
        wanted = set(only_keys)
        cards = [c for c in CARD_CONFIGS if c["key"] in wanted]
        unknown = wanted - {c["key"] for c in CARD_CONFIGS}
        if unknown:
            logger.warning(f"  Ignoring unknown Domo card key(s): {sorted(unknown)}")
        if not cards:
            logger.warning("  No matching Domo cards to export.")
            return {}

    logger.info(
        f"  Release date range: {ctx.release_start} → {ctx.release_end}\n"
        f"  Tracklist token:    {ctx.tracklist_token}"
    )

    if dry_run:
        logger.info("  [DRY RUN] Would export the following files:")
        for card in cards:
            logger.info(f"    {card['description']:<18} → {card['output_fn'](ctx)}")
        return {card["key"]: "skipped" for card in cards}

    TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}

    _require_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            downloads_path=str(TEMP_DOWNLOAD_DIR),
        )
        ctx_pw = browser.new_context(accept_downloads=True)
        page   = ctx_pw.new_page()

        try:
            _authenticate(page, logger)

            for card in cards:
                output_path = card["output_fn"](ctx)
                logger.info(f"\n  ── {card['description']} (card {card['card_id']}) ──")
                logger.info(f"     Output: {output_path}")
                try:
                    _export_card(page, card, output_path, ctx, logger)
                    results[card["key"]] = "ok"
                    logger.info(f"     ✓ Saved: {output_path}")
                except PlaywrightTimeoutError as exc:
                    logger.error(f"     ✗ Timeout on '{card['description']}': {exc}")
                    results[card["key"]] = "failed"
                except Exception as exc:
                    logger.error(f"     ✗ Failed '{card['description']}': {exc}")
                    results[card["key"]] = "failed"
        finally:
            browser.close()

    try:
        if TEMP_DOWNLOAD_DIR.exists() and not any(TEMP_DOWNLOAD_DIR.iterdir()):
            TEMP_DOWNLOAD_DIR.rmdir()
    except Exception:
        pass

    return results


def verify_exports_exist(
    ctx: ReleaseContext,
    logger: logging.Logger,
) -> dict[str, bool]:
    """Check that all five expected CSV files already exist on disk."""
    results: dict[str, bool] = {}
    for card in CARD_CONFIGS:
        path   = card["output_fn"](ctx)
        exists = Path(path).exists()
        results[card["key"]] = exists
        logger.log(
            logging.INFO if exists else logging.WARNING,
            f"  {'✓' if exists else '✗'}  {card['key']:<18} {path}",
        )
    return results


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _authenticate(page, logger: logging.Logger) -> None:
    logger.info("  Navigating to Domo login page…")
    page.goto(
        f"https://{DOMO_INSTANCE}/auth/index?lang=en",
        wait_until="domcontentloaded",
    )
    logger.info("  >>> Complete Microsoft login in the browser window <<<")
    page.wait_for_function(
        f"() => window.location.hostname === '{DOMO_INSTANCE}'"
        f"   && !window.location.href.includes('/auth')",
        timeout=LOGIN_TIMEOUT,
        polling=1000,
    )
    logger.info("  Logged in.")
    time.sleep(NAV_WAIT)


# ---------------------------------------------------------------------------
# Per-card export
# ---------------------------------------------------------------------------

def _export_card(
    page,
    card: dict,
    output_path: Path,
    ctx: ReleaseContext,
    logger: logging.Logger,
) -> None:
    # 0. Reject placeholder card IDs (e.g. TUNESAT_CARD_ID_TBD) so a
    #    missing config doesn't navigate to a garbage URL and time out
    #    later with a confusing error.
    if not card["card_id"].isdigit():
        raise RuntimeError(
            f"Card '{card['key']}' has a non-numeric placeholder card ID "
            f"({card['card_id']!r}).\n"
            f"  Fill in the real Domo card ID in config.py DOMO_CARDS "
            f"before running this export."
        )

    # 1. Navigate to card detail
    _navigate_to_card(
        page,
        card["card_id"],
        logger,
        page_id=card.get("page_id", DOMO_PAGE_ID),
    )

    # 2. Set the date range.  In previous-month mode use Domo's built-in
    #    "Previous Month" preset; otherwise set the explicit Between range.
    if getattr(ctx, "previous_month", False):
        _apply_previous_month_preset(page, logger)
    else:
        _apply_between_date_range(page, ctx.release_start, ctx.release_end, logger)

    # 3. Confirm we're still on the card (picker close should not navigate)
    if "kpis/details" not in page.url:
        raise RuntimeError(
            f"Navigated away from card detail after setting date range.\n"
            f"  Current URL: {page.url}"
        )

    # 4. Download (always an XLSX from Domo) → save as XLSX or convert to CSV
    xlsx_temp = _trigger_excel_download(page, card["description"], logger)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if card.get("format", "csv") == "xlsx":
        # Keep the workbook as-is (cross-filesystem move: temp is in Downloads,
        # output on the Pegasus volume).
        if output_path.exists():
            output_path.unlink()
        shutil.move(str(xlsx_temp), str(output_path))
        logger.info(f"     Saved XLSX → {output_path.name}")
    else:
        _xlsx_to_csv(xlsx_temp, output_path, logger)


def _navigate_to_card(
    page,
    card_id: str,
    logger: logging.Logger,
    *,
    page_id: str = DOMO_PAGE_ID,
) -> None:
    url = f"https://{DOMO_INSTANCE}/page/{page_id}/kpis/details/{card_id}"
    logger.info(f"     Navigating to card {card_id}…")
    page.evaluate(f"window.location.replace('{url}')")
    try:
        page.wait_for_function(
            "() => window.location.href.includes('kpis/details')",
            timeout=NAV_TIMEOUT, polling=500,
        )
    except PlaywrightTimeoutError:
        pass
    time.sleep(NAV_WAIT)


# ---------------------------------------------------------------------------
# Timeframe picker — Between date range
# ---------------------------------------------------------------------------

def _dump_picker_debug(page, logger: logging.Logger, tag: str) -> None:
    """
    On a picker-interaction failure, save a screenshot and the dropdown's
    visible option text so the exact menu structure can be inspected.  Best
    effort — never raises.
    """
    try:
        _REPO_ROOT = Path(__file__).resolve().parent.parent
        dbg = _REPO_ROOT / "_logs" / "domo_failures"
        dbg.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot = dbg / f"domo_{tag}_{ts}.png"
        page.screenshot(path=str(shot))
        logger.error(f"     ⓘ Saved picker screenshot: {shot}")
    except Exception as exc:
        logger.warning(f"     (could not save screenshot: {exc})")

    # Log the text of every timeframe option/group currently in the DOM so we
    # can see the actual labels ("Previous", "Previous Month", etc.).
    try:
        opts = page.locator(
            "[class*='timeframe-options'] li, [class*='timeframe-options'] span"
        )
        n = min(opts.count(), 40)
        labels = []
        for i in range(n):
            t = (opts.nth(i).inner_text(timeout=500) or "").strip()
            if t:
                labels.append(t)
        if labels:
            logger.error(
                "     ⓘ Timeframe options seen in DOM: "
                + " | ".join(dict.fromkeys(labels))   # dedupe, keep order
            )
    except Exception as exc:
        logger.warning(f"     (could not read option labels: {exc})")


def _apply_previous_month_preset(page, logger: logging.Logger) -> None:
    """
    Select Domo's built-in "Previous Month" timeframe preset.

    Same picker as the Between flow, but instead of choosing Between and
    filling two dates, we pick the "Previous Month" preset.  In this Domo
    instance the presets are TWO-LEVEL: "Previous Month" lives inside the
    "Previous" group and is hidden (Angular ``ng-hide`` /
    ``ng-show="$ctrl.selectedGroup === group"``) until that group is selected.
    So we must click **"Previous"** first to reveal the group, then click
    **"Previous Month"** within it.  Domo then scopes the card to the whole
    prior calendar month, so we don't compute or type any dates.
    """
    logger.info("     Setting date range: Previous Month (Domo preset)")

    # Step 1: open the timeframe picker
    page.locator("cd-timeframe-label").click(timeout=10_000)
    time.sleep(PICKER_WAIT)

    # Step 2: open the dropdown
    page.locator(
        "span.db-dropdown-selector.timeframe-options__date-range"
    ).click(timeout=10_000)
    time.sleep(DROPDOWN_WAIT)

    # Step 3: select the "Previous" GROUP first.  This sets
    # $ctrl.selectedGroup and un-hides the group's ranges (incl. "Previous
    # Month").  Match the group header by exact text to avoid colliding with
    # the "Previous Month"/"Previous Week" range items themselves.
    #
    # Use a regex for an EXACT "Previous" match so we don't match
    # "Previous Month", "Previous Week", etc.
    import re as _re
    try:
        group = page.get_by_text(_re.compile(r"^\s*Previous\s*$"))
        try:
            group.first.click(timeout=10_000)
            logger.info("     Selected 'Previous' group.")
        except Exception:
            # Fallback: some builds render the group as a list item / button.
            page.locator(
                "li.timeframe-options__group:has-text('Previous'), "
                "[class*='timeframe-options__group']:has-text('Previous')"
            ).first.click(timeout=10_000)
            logger.info("     Selected 'Previous' group (fallback selector).")
        time.sleep(DROPDOWN_WAIT)

        # Step 4: now click the "Previous Month" RANGE within the revealed
        # group.  Scroll into view and click; match exact text so it isn't
        # confused with other ranges.
        pm = page.get_by_text(_re.compile(r"^\s*Previous Month\s*$"))
        try:
            pm.first.scroll_into_view_if_needed(timeout=5_000)
        except Exception:
            pass
        pm.first.click(timeout=10_000)
        logger.info("     Clicked 'Previous Month'.")
    except Exception:
        # Capture exactly what the picker looked like so the selectors can be
        # corrected if Domo's markup differs from what we expect.
        _dump_picker_debug(page, logger, "previous_month")
        raise
    time.sleep(DROPDOWN_WAIT)

    # Step 5: close the picker the same neutral way the Between flow does
    # (clicking the filter-bar row to the right, never the card body).
    vp = page.viewport_size or {"width": 1280, "height": 720}
    page.mouse.click(vp["width"] - 150, 130)
    logger.info(f"     Picker closed.  URL: {page.url}")

    # Step 6: wait for the card to reload with the new timeframe
    time.sleep(4)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    time.sleep(2)


def _apply_between_date_range(
    page,
    start_iso: str,
    end_iso: str,
    logger: logging.Logger,
) -> None:
    """
    Open the timeframe picker, select Between, fill the two date inputs,
    then close the picker by clicking a neutral spot on the page body.

    CRITICAL: Do not use Tab or Enter after filling the end-date input —
    both cause the browser to navigate away from the card detail view in
    this Domo instance.
    """
    start_mdy = datetime.strptime(start_iso, "%Y-%m-%d").strftime("%m/%d/%Y")
    end_mdy   = datetime.strptime(end_iso,   "%Y-%m-%d").strftime("%m/%d/%Y")
    logger.info(f"     Setting date range: {start_mdy} → {end_mdy}")

    # Step 1: open the timeframe picker
    page.locator("cd-timeframe-label").click(timeout=10_000)
    time.sleep(PICKER_WAIT)

    # Step 2: open the dropdown
    page.locator(
        "span.db-dropdown-selector.timeframe-options__date-range"
    ).click(timeout=10_000)
    time.sleep(DROPDOWN_WAIT)

    # Step 3: click Between (confirmed direct top-level option)
    page.locator(
        "span.ng-binding.ng-scope:has-text('Between'), "
        "li:has-text('Between')"
    ).first.click(timeout=10_000)
    logger.info("     Clicked 'Between'.")
    time.sleep(DROPDOWN_WAIT)

    # Step 4: fill start date
    _fill_picker_date(page, 0, start_mdy, "start", logger)
    time.sleep(0.4)

    # Step 5: fill end date
    _fill_picker_date(page, 1, end_mdy, "end", logger)
    time.sleep(0.4)

    # Step 6: close the picker by clicking the empty area to the RIGHT of the
    # date inputs in the filter bar row (outside the card body entirely).
    # This avoids accidentally drilling into the card data.
    # The area is at approximately y=130 (filter bar row), far-right x.
    vp = page.viewport_size or {"width": 1280, "height": 720}
    page.mouse.click(vp["width"] - 150, 130)
    logger.info(f"     Picker closed.  URL: {page.url}")

    # Step 7: wait for the card to reload with the new date range
    time.sleep(4)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    time.sleep(2)


def _fill_picker_date(
    page,
    index: int,
    value_mdy: str,
    label: str,
    logger: logging.Logger,
) -> None:
    """
    Fill the nth date input inside the open timeframe picker.
    The picker exposes two inputs after Between is clicked.

    Selector order: most specific first (placeholder → ng-model → generic).
    click(click_count=3) selects all existing text before typing.
    """
    selectors = [
        "input[placeholder*='MM/DD/YYYY'], input[placeholder*='mm/dd']",
        "input[ng-model*='startDate'], input[ng-model*='endDate'], "
        "input[ng-model*='start'], input[ng-model*='end'], "
        "input[ng-model*='date' i]",
        ".timeframe-options input, .timeframe-between input, "
        "cd-timeframe-dropdown input",
    ]

    for selector in selectors:
        inputs = page.locator(selector)
        if inputs.count() > index:
            loc = inputs.nth(index)
            loc.click()
            time.sleep(0.15)
            loc.click(click_count=3)
            time.sleep(0.1)
            loc.type(value_mdy, delay=40)
            logger.debug(f"       {label} set to {value_mdy} via: {selector!r}")
            return

    # Fallback: log what inputs exist inside the picker and raise
    picker_inputs = page.evaluate("""
        () => Array.from(document.querySelectorAll('input')).map(i => ({
            type: i.type, placeholder: i.placeholder,
            ngModel: i.getAttribute('ng-model'), visible: !!(i.offsetWidth||i.offsetHeight)
        }))
    """)
    raise PlaywrightTimeoutError(
        f"Could not find date picker input [{index}] ({label}).\n"
        f"  All inputs on page: {picker_inputs}\n"
        f"  Inspect the picker DOM with --debug to find the correct selector."
    )


# ---------------------------------------------------------------------------
# Export trigger
# ---------------------------------------------------------------------------

def _wait_for_enabled(
    page, selector: str, logger: logging.Logger,
    timeout: int = 15, poll: float = 0.5,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page.locator(selector).first.is_enabled():
                return
        except Exception:
            pass
        time.sleep(poll)
    logger.warning(f"     '{selector}' not enabled after {timeout}s — clicking anyway.")


def _trigger_excel_download(
    page,
    description: str,
    logger: logging.Logger,
) -> Path:
    """
    Share icon → Send / Export → Excel.
    'Send / Export' is confirmed enabled when on the card detail page.
    """
    logger.info("     Opening share menu…")

    try:
        page.locator(
            "[aria-label*='share' i],[aria-label*='export' i],"
            "[title*='share' i],[title*='export' i]"
        ).first.click(timeout=10_000)
    except Exception:
        page.locator(
            ".detail-header button, .kpi-card-header button"
        ).nth(3).click(timeout=10_000)
    time.sleep(EXPORT_WAIT)

    _wait_for_enabled(page, "text=Send / Export", logger, timeout=15)
    page.locator("text=Send / Export").click(timeout=10_000)
    time.sleep(EXPORT_WAIT)

    logger.info("     Downloading Excel…")
    with page.expect_download(timeout=DOWNLOAD_TIMEOUT) as dl_info:
        page.locator("text=Excel").click(timeout=10_000)

    download  = dl_info.value
    temp_path = TEMP_DOWNLOAD_DIR / f"_temp_{description.replace(' ', '_')}.xlsx"
    download.save_as(str(temp_path))
    logger.debug(f"       Temp: {temp_path}")
    return temp_path


# ---------------------------------------------------------------------------
# Excel → CSV
# ---------------------------------------------------------------------------

def _xlsx_to_csv(xlsx_path: Path, csv_path: Path, logger: logging.Logger) -> None:
    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError("pandas not installed.  Run: pip install pandas openpyxl")
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "openpyxl not installed.  Run:\n"
            "    pip install openpyxl"
        )
    df = pd.read_excel(xlsx_path, dtype=str).fillna("")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    logger.info(f"     CSV rows: {len(df)}  columns: {len(df.columns)}")
    xlsx_path.unlink()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

def _select_cards(
    args, logger: logging.Logger
) -> list[dict] | None:
    """
    Resolve --only into a concrete list of CARD_CONFIGS entries.

    --only accepts a COMMA-SEPARATED list of tokens; each token is matched
    case-insensitively as a substring against EITHER the card key OR its
    description.  So all of these work::

        --only tunesat
        --only netmix_metadata,qwire_metadata
        --only "Tunesat Metadata, SoundExchange"

    Results are returned in CARD_CONFIGS order (not token order) and
    de-duplicated.  Returns None if ANY token matches nothing, so the caller
    can exit non-zero.  No --only ⇒ every card (matches the orchestrator).
    """
    if not args.only:
        return list(CARD_CONFIGS)

    tokens = [t.strip().lower() for t in args.only.split(",") if t.strip()]
    if not tokens:
        return list(CARD_CONFIGS)

    def _matches(card: dict, tok: str) -> bool:
        return tok in card["key"].lower() or tok in card["description"].lower()

    selected = [c for c in CARD_CONFIGS if any(_matches(c, t) for t in tokens)]
    unmatched = [t for t in tokens if not any(_matches(c, t) for c in CARD_CONFIGS)]

    if unmatched:
        valid = "\n".join(
            f"    {c['key']:22s}  {c['description']}" for c in CARD_CONFIGS
        )
        logger.error(
            f"No card matched --only token(s): {', '.join(unmatched)}.\n"
            f"Available cards:\n{valid}"
        )
        return None
    return selected


def _run_test(args) -> None:
    import sys
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("domo_test")

    ctx = context_from_cli_args(args)
    logger.info(f"Release context: {ctx}")
    logger.info(f"Date range:      {ctx.release_start} → {ctx.release_end}")

    cards = _select_cards(args, logger)
    if cards is None:
        sys.exit(1)

    if args.dry_run:
        # Dry-run always previews ALL cards via the orchestrator path so
        # users can see the full pipeline plan; --only is ignored here.
        run_domo_exports(ctx, dry_run=True, logger=logger)
        return

    logger.info(
        f"Running {len(cards)} card(s): "
        f"{', '.join(c['key'] for c in cards)}"
    )

    TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    _require_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, downloads_path=str(TEMP_DOWNLOAD_DIR),
        )
        ctx_pw = browser.new_context(accept_downloads=True)
        page   = ctx_pw.new_page()

        keep_browser_open = False
        results: dict[str, str] = {}
        try:
            _authenticate(page, logger)

            for card in cards:
                output_path = card["output_fn"](ctx)
                logger.info(
                    f"\n  ── {card['description']} (card {card['card_id']}) ──"
                )
                logger.info(f"     Output: {output_path}")
                try:
                    _export_card(page, card, output_path, ctx, logger)
                    results[card["key"]] = "ok"
                    logger.info(f"     ✓ Saved: {output_path}")
                except PlaywrightTimeoutError as exc:
                    results[card["key"]] = "failed"
                    logger.error(f"     ✗ Timeout: {exc}")
                    if args.debug:
                        keep_browser_open = True
                        break
                except Exception as exc:
                    results[card["key"]] = "failed"
                    logger.error(f"     ✗ Failed: {exc}")
                    if args.debug:
                        keep_browser_open = True
                        break

            # Per-card summary
            logger.info("\n  ─── Summary ───")
            for key, status in results.items():
                mark = "✓" if status == "ok" else "✗"
                logger.info(f"    {mark}  {key}: {status}")

            if keep_browser_open:
                logger.info(
                    "Browser left open (--debug).  Press Ctrl+C to exit."
                )
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
        finally:
            browser.close()

    # Tidy the temp download dir if it ended up empty
    try:
        if TEMP_DOWNLOAD_DIR.exists() and not any(TEMP_DOWNLOAD_DIR.iterdir()):
            TEMP_DOWNLOAD_DIR.rmdir()
    except Exception:
        pass


if __name__ == "__main__":
    import argparse, sys

    p = argparse.ArgumentParser(
        description=(
            "Export Domo cards for a UPM release.  By default runs all six "
            "cards; pass --only SUBSTR to run a subset."
        )
    )
    p.add_argument("--test",    action="store_true", required=True)
    p.add_argument("--year",    type=int)
    p.add_argument("--month",   type=int)
    p.add_argument("--part",    type=int, choices=[1, 2])
    p.add_argument(
        "--previous-month", action="store_true",
        help="Full-month run for the previous month "
             "(no Part split). Relative to today, or to "
             "--year/--month if given.")
    p.add_argument(
        "--only", default=None, metavar="TOKENS",
        help=(
            "Run only the card(s) matching TOKENS — a comma-separated list, "
            "each matched case-insensitively against a card key or description. "
            "Examples: --only tunesat ; "
            "--only netmix_metadata,qwire_metadata ; "
            "--only \"SoundExchange, Japan JMD\".  Omit to run all cards in a "
            "single browser session."
        ),
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Preview all six destination paths without exporting.")
    p.add_argument("--debug",   action="store_true",
                   help="Keep browser open on the first failure for inspection.")
    args = p.parse_args()
    _run_test(args)
