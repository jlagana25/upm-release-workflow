"""
config.py — UPM Release Workflow Configuration
================================================
All paths, naming tokens, and constants are derived here from
(year, month, part). No hardcoded dates anywhere else.

Usage:
    from config import ReleaseContext
    ctx = ReleaseContext(year=2026, month=5, part=1)
    print(ctx.release_id)           # UPM-2026-05-P1
    print(ctx.specials_root)        # UPM-2026-05-P1
    print(ctx.hd_folder)            # UPM-2026-05-P1
    print(ctx.us_tracklist_csv)     # Path(…/UPM-US-2026-05-P1-Tracklist.csv)
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Fixed volume roots — checked by preflight before any work begins
# ---------------------------------------------------------------------------

VOLUMES: dict[str, Path] = {
    "R8_1": Path("/Volumes/Pegasus32 R8 - 1"),
    "R8_2": Path("/Volumes/Pegasus32 R8 - 2"),
}

# ---------------------------------------------------------------------------
# Fixed baseline source paths
# ---------------------------------------------------------------------------

BASELINE_SPECIALS = Path(
    "/Volumes/Pegasus32 R8 - 1/_Specials/_UPM Specials Baseline"
)
BASELINE_HD_STAGING = Path(
    "/Volumes/Pegasus32 R8 - 2/Hard Drive Updates/2-STAGING"
    "/_BaselineFolder-Use this folder to create a new monthly folder"
)
BASELINE_HD_FINAL = Path(
    "/Volumes/Pegasus32 R8 - 2/Hard Drive Updates/1-ORIGINAL"
    "/BaselineFolder-UPMNewReleases"
)

# ---------------------------------------------------------------------------
# Fixed output base paths
# ---------------------------------------------------------------------------

SPECIALS_BASE = Path("/Volumes/Pegasus32 R8 - 1/_Specials/UPM")
HD_STAGING_BASE = Path("/Volumes/Pegasus32 R8 - 2/Hard Drive Updates/2-STAGING")
HD_FINAL_BASE = Path(
    "/Volumes/Pegasus32 R8 - 2/Hard Drive Updates/3-FINAL PACKAGING/UPM-US"
)
MASTERS_COVERS_DIR = Path("/Volumes/Pegasus32 R8 - 1/UPM-US-Masters/Covers")
UPM_CACHE_MP3 = Path("/Volumes/Pegasus32 R8 - 2/UPM-US-Cache/MP3")
UPM_CACHE_WAV = Path("/Volumes/Pegasus32 R8 - 2/UPM-US-Cache/WAV")
SOUNDMOUSE_BASE = Path("/Volumes/Pegasus32 R8 - 2/SoundMouse")

# Retired partner folders can remain in the shared Specials baseline for
# historical releases. New release trees must not inherit them. Matching is
# punctuation/case-insensitive so both "MTV-Viacom" and layout variants are
# excluded without depending on a particular dated folder prefix.
RETIRED_PARTNER_TOKENS: frozenset[str] = frozenset({"mtvviacom"})


def is_retired_partner_name(name: str) -> bool:
    normalized = "".join(ch.lower() for ch in name if ch.isalnum())
    return any(token in normalized for token in RETIRED_PARTNER_TOKENS)

# HDF1-local coordination area used by the login-session Soundminer agent.
# HDF2 transports JSON requests/status over SSH; SSH never launches or drives
# the GUI. Keeping the queue local avoids macOS background-process stalls when
# writing to the removable/shared Pegasus volume.
SOUNDMINER_AGENT_ROOT = (
    Path.home() / "Library" / "Application Support" / "UPM Soundminer Agent" / "queue"
)
SOUNDMINER_AGENT_ENABLED = True
SOUNDMINER_AGENT_POLL_SECONDS = 5
SOUNDMINER_AGENT_HEARTBEAT_TIMEOUT = 180
SOUNDMINER_AGENT_JOB_TIMEOUT = 12 * 60 * 60

# ---------------------------------------------------------------------------
# Fixed user-side paths
# ---------------------------------------------------------------------------

USER_HOME = Path.home()
TRACKLISTS_DIR = Path(os.environ.get(
    "UPM_TRACKLISTS_DIR",
    USER_HOME / "Documents" / "UPM Tracklists" / "Release Lists",
))
EXPORTS_DIR = Path(os.environ.get(
    "UPM_EXPORTS_DIR",
    USER_HOME / "Documents" / "Scripts" / "Python" / "_Exports" / "_New Releases",
))
LOGS_DIR = Path(os.environ.get(
    "UPM_LOGS_DIR",
    USER_HOME / "Documents" / "Scripts" / "Python" / "_Logs" / "UPM Release Workflow",
))
PRIVATE_STATE_DIR = USER_HOME / ".upm_release_workflow"
DOMO_PROFILE_DIR = PRIVATE_STATE_DIR / "domo_browser_profile"
UNISYNC_PREFS_DIR = USER_HOME / "Library" / "SMUniSync"
UNISYNC_XML_PATH = UNISYNC_PREFS_DIR / "UniSync.xml"
MISSING_COVER_REPORT = Path("/Volumes/UPM Builds/Missing_CDCover_Downloads.csv")

# ---------------------------------------------------------------------------
# Domo configuration
# ---------------------------------------------------------------------------

DOMO_INSTANCE = "umusic-publishing.domo.com"
DOMO_PAGE_ID = "117808327"
SOUNDMOUSE_DOMO_PAGE_ID = "1225470481"

DOMO_CARDS: dict[str, str] = {
    "us_tracklist":     "1384111004",
    "exus_tracklist":   "1389146023",
    "album_list":       "1741693601",
    "japan_metadata":   "242186821",
    "nbc_metadata":     "233748559",
    # Tunesat Metadata — Step 13 (non-maintrack cleanup) reads this CSV's
    # "File Name" column to decide what stays in the Tunesat Music folder.
    # Same date-range filter as the other cards (Part 1: days 01–14,
    # Part 2: days 15–end of month).  Replace the placeholder with the
    # real card ID before running Step 1.
    "tunesat_metadata": "1826988754",

    # Partner deliverable metadata exports (Step 1, after folder setup) → each
    # lands in its partner's 3-FINAL PACKAGING Metadata folder.  Tunesat reuses
    # the card above (it already lands in the Tunesat deliverable folder).
    "netmix_metadata":       "704249829",
    "synchtank_metadata":    "835577782",
    "scripps_metadata":      "871733994",
    "qwire_metadata":        "1299912690",
    "sourceaudio_metadata":  "816828701",
    "sourceaudio_exus_metadata": "1909039415",
    "japan_jmdtss_metadata": "272055256",   # exported as .xlsx (not CSV)
    "soundexchange_mgb":     "700921386",
    "soundexchange_ztunes":  "583310488",
}

# SoundMouse is a self-contained Step 16.  Its bucket card determines which
# of the ten territory-specific metadata workbooks are required for a run.
SOUNDMOUSE_DOMO_CARDS: dict[str, str] = {
    "tracklist": "1928643877",
    "bucket": "471892560",
    "01": "491141478",
    "02": "2122953228",
    "03": "1047392342",
    "04": "1779722120",
    "05": "674513648",
    "06": "248457036",
    "07": "108032482",
    "08": "430369583",
    "09": "91106427",
    "10": "1228428287",
}

# ---------------------------------------------------------------------------
# Remote Soundminer machine
# ---------------------------------------------------------------------------
#
# Soundminer v5Pro runs on a SEPARATE Mac on the same network, not on the
# box that runs this pipeline.  Step 12 is therefore triggered over SSH and
# executed *on that remote Mac*, where the GUI, Soundminer, and the
# reference screenshots all live.  Both Pegasus volumes are mounted at the
# SAME paths on both machines, so every path the pipeline computes is valid
# on either side — no path translation needed.
#
# Two macOS realities shape this config (see remote_runner.py for detail):
#   1. An SSH-launched process can't reach the GUI ("Aqua") session by
#      default.  GUI_SESSION_WRAPPER re-injects the command into the
#      logged-in user's session via `launchctl asuser <uid>`.
#   2. The remote must have granted Accessibility + Screen Recording to the
#      process that ends up driving the UI (one-time, in System Settings).
#
# Fill these in for your environment.  REMOTE_SOUNDMINER_ENABLED gates
# whether the orchestrator runs Step 12 remotely (True) or locally (False).

# SSH-triggered remote execution is DISABLED for this deployment.
#
# Empirically, the Soundminer Mac (USMPSMDHDF1) cannot be driven over SSH:
# it's a managed machine (CyberArk EPM, sudoers locked with `(ALL) PASSWD:
# ALL` and timestamp_timeout=0), and — decisively — macOS refuses screen
# capture to SSH-spawned processes ("could not create image from display"),
# because the responsible process (sshd) lacks Screen Recording permission
# and can't be granted it under policy.  pyautogui's whole approach needs a
# capturable display, so SSH-triggering is a non-starter here regardless of
# sudo/launchctl/TCC tuning.
#
# The supported unattended path is therefore NOT SSH. ``soundminer_agent.py``
# is installed once as an HDF1 per-user LaunchAgent with Aqua session type.
# HDF2 writes requests/status JSON through the shared Pegasus volume; the HDF1
# agent drives Soundminer locally and reports heartbeats/results. The legacy
# manual handoff remains available with ``--no-soundminer-agent``.
#
# If this ever moves to a non-managed Mac where SSH GUI automation is
# permitted, set REMOTE_SOUNDMINER_ENABLED = True and the smoke test in
# remote_runner.py will validate the path.
REMOTE_SOUNDMINER_ENABLED = False

# ---------------------------------------------------------------------------
# Machine detection
# ---------------------------------------------------------------------------
# The workflow can be launched from EITHER machine.  Step 12 (Soundminer) can
# only be driven on the Soundminer Mac itself (its GUI/console session owns the
# capturable display).  So the orchestrator auto-detects which machine it is
# running on by hostname and chooses how to run Step 12:
#
#   • On the Soundminer machine  → run Step 12 INLINE (drive the GUI locally).
#   • On the pipeline machine    → submit to HDF1 login-session agent. Legacy
#                                   manual handoff is recovery-only.
#
# Every other step rides on the shared Pegasus volumes (identical paths on both
# machines), so they run the same from either host.

import socket as _socket

# Short hostname of the Soundminer Mac (the box that runs Soundminer's GUI).
SOUNDMINER_HOSTNAME = "USMPSMDHDF1"
# Short hostname of the pipeline Mac (where Steps 1–11/12.7/13/14 normally run).
PIPELINE_HOSTNAME   = "USMPSMDHDF2"


def current_hostname() -> str:
    """Short, upper-cased hostname of the machine we're running on."""
    return _socket.gethostname().split(".")[0].strip().upper()


def is_soundminer_machine() -> bool:
    """True if this process is running on the Soundminer Mac."""
    return current_hostname() == SOUNDMINER_HOSTNAME.upper()


def is_pipeline_machine() -> bool:
    """True if this process is running on the designated pipeline Mac."""
    return current_hostname() == PIPELINE_HOSTNAME.upper()


def machine_role() -> str:
    """Human-readable role label for logging/banners."""
    host = current_hostname()
    if host == SOUNDMINER_HOSTNAME.upper():
        return f"{host} (Soundminer machine)"
    if host == PIPELINE_HOSTNAME.upper():
        return f"{host} (pipeline machine)"
    return f"{host} (unrecognised — treating as pipeline machine)"


REMOTE_SOUNDMINER: dict[str, str] = {
    # SSH target.  `host` may be a hostname, a Bonjour name (e.g.
    # "soundminer-mac.local"), or an IP.  `user` is the macOS account that
    # is logged into the GUI / Screen Sharing session on the remote Mac.
    "host":        os.environ.get("UPM_SOUNDMINER_HOST", "USMPSMDHDF1.local"),
    "user":        os.environ.get("UPM_SOUNDMINER_USER", "hdfuser"),
    # Numeric UID of that account on the remote Mac.  Get it by running
    # `id -u` while logged into the remote Mac (first account is usually
    # 501).  Used by `launchctl asuser <uid>` to reach the GUI session.
    "uid":         "504",                       # confirmed via `id -u`
    # Absolute path to this project's `files/` dir ON THE REMOTE MAC, AS SEEN
    # OVER SSH.  Verified reachable over SSH after reboot; the earlier
    # /Volumes/Documents mount was a transient auto-mount that didn't survive
    # a restart, so we use the stable home-directory path (identical in both
    # SSH and console contexts on this machine).
    "repo_path":   os.environ.get(
        "UPM_SOUNDMINER_REPO",
        "/Users/hdfuser/Documents/Scripts/Python/UPM Release WorkFlow Automation/files",
    ),
    # Same files, but the path AS SEEN IN THE REMOTE MAC'S OWN CONSOLE /
    # Screen Sharing Terminal session, where the volume mounts under
    # /Users/hdfuser/….  This is what the Step 12 hand-off banner tells the
    # operator to `cd` into, because Path C runs soundminer.py there.
    "console_repo_path": os.environ.get(
        "UPM_SOUNDMINER_REPO",
        "/Users/hdfuser/Documents/Scripts/Python/UPM Release WorkFlow Automation/files",
    ),
    # Python interpreter on the remote Mac (must have pyautogui + Pillow).
    "python":      "python3",
    # Command template that re-injects a GUI command into the logged-in
    # Aqua session.  "{uid}", "{user}", and "{cmd}" are substituted by
    # remote_runner (via str.replace, so braces in {cmd} are safe).  The
    # default uses `launchctl asuser`, which requires the SSH user to have
    # (passwordless) sudo for that command.  remote_runner appends the
    # actual `/bin/bash -lc '…'` invocation as {cmd}.  If your setup can
    # reach the GUI from SSH without this (rare), set it to just "{cmd}".
    "gui_wrapper": "sudo launchctl asuser {uid} sudo -u {user} {cmd}",
}


# Placeholder string used throughout the baseline folder/template files; it is
# replaced with the release's display string (e.g. "May 2026 Part 1") during
# folder setup (Step 2/3) and album-list generation (Step 4).
PLACEHOLDER = "MMMM YYYY"


# File extensions safe to read as UTF-8/latin-1 for placeholder replacement
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    [".txt", ".csv", ".html", ".htm", ".xml", ".json", ".md", ".rtf"]
)

# Ex-US labels that are eligible for Tunesat delivery
TUNESAT_EXUS_LABELS: frozenset[str] = frozenset(
    [
        "Bruton",
        "Bruton Classical Series",
        "Bruton Vaults",
        "Bruton Vaults Anthologies",
        "BTV",
        "Kosinus",
        "Kosinus Archives",
        "Kosinus Arts",
        "Kosinus Classical",
        "Kosinus Magazine",
        "Kosinus Trailers",
        "Kosinus World",
    ]
)

# Required macOS applications — checked during preflight
REQUIRED_APPS: dict[str, str] = {
    "UniSync":          "/Applications/UniSync.app",
    "Soundminer v5Pro": "/Applications/Soundminer v5Pro.app",
    "ffmpeg":           "ffmpeg",  # checked via shutil.which
}

# Possible DOCX-to-PDF converters (tried in order)
DOCX_TO_PDF_METHODS: list[str] = ["libreoffice", "soffice"]


# ---------------------------------------------------------------------------
# ReleaseContext — the single source of truth for one run
# ---------------------------------------------------------------------------

class ReleaseContext:
    """
    Derives every naming token, date range, and file/folder path for one UPM
    release run. Supports legacy month/part runs and exact rolling ranges.
    """

    def __init__(
        self,
        year: int,
        month: int,
        part: int,
        previous_month: bool = False,
        full_month_content: bool = False,
        range_start: date | None = None,
        range_end: date | None = None,
    ) -> None:
        # In previous-month mode there is no Part split — the run covers the
        # full calendar month — so `part` is normalised to 1 and ignored for
        # naming/date purposes.  We keep the attribute so downstream code that
        # reads ctx.part still works.
        if previous_month:
            part = 1
        if previous_month and (full_month_content or range_start or range_end):
            raise ValueError("previous-month cannot be combined with another date mode")
        if (range_start is None) != (range_end is None):
            raise ValueError("range_start and range_end must be supplied together")
        if range_start is not None and (range_end - range_start).days != 13:
            raise ValueError("date-range deliveries must cover exactly 14 inclusive days")
        if part not in (1, 2):
            raise ValueError(f"part must be 1 or 2, got {part!r}")
        if full_month_content and part != 2:
            raise ValueError("full_month_content requires Part 2")
        if not (1 <= month <= 12):
            raise ValueError(f"month must be 1-12, got {month!r}")

        self.year = year
        self.month = month
        self.part = part
        self.previous_month = previous_month
        self.full_month_content = full_month_content
        self.is_date_range = range_start is not None

        run_date = datetime(year, month, 1)

        # ---- Basic name tokens -----------------------------------------------
        self.month_name = run_date.strftime("%B")   # May
        self.year_str   = run_date.strftime("%Y")   # 2026
        self.month_num  = run_date.strftime("%m")   # 05

        # Previous-month mode is always a single full-month run, never Part 2.
        # Keep it explicitly named so it cannot collide with the normal Part 1
        # release for the same calendar month.
        self.is_full_month = previous_month
        if self.is_date_range:
            assert range_start is not None and range_end is not None
            self.release_variant = "RANGE"
            self.release_id = f"UPM-{range_start.isoformat()}_to_{range_end.isoformat()}"
        else:
            self.release_variant = "FULL" if self.is_full_month else f"P{self.part}"
            self.release_id = f"UPM-{self.year_str}-{self.month_num}-{self.release_variant}"
        # e.g. 2026-05-P1, 2026-05-P2, or 2026-05-FULL
        self.tracklist_token = self.release_id.removeprefix("UPM-")

        # ---- Display strings -------------------------------------------------
        # May 2026
        self.month_display = f"{self.month_name} {self.year_str}"

        # May 2026 Part 1, May 2026 Part 2, or May 2026 Full.
        if self.is_date_range:
            assert range_start is not None and range_end is not None
            if range_start.year == range_end.year and range_start.month == range_end.month:
                self.month_display_folder = (
                    f"{range_start.strftime('%B')} {range_start.day}\u2013{range_end.day} "
                    f"{range_start.year}"
                )
            elif range_start.year == range_end.year:
                self.month_display_folder = (
                    f"{range_start.strftime('%B')} {range_start.day}\u2013"
                    f"{range_end.strftime('%B')} {range_end.day} {range_start.year}"
                )
            else:
                self.month_display_folder = (
                    f"{range_start.strftime('%B')} {range_start.day} {range_start.year}\u2013"
                    f"{range_end.strftime('%B')} {range_end.day} {range_end.year}"
                )
        else:
            self.month_display_folder = (
                f"{self.month_name} {self.year_str} Full"
                if self.is_full_month
                else f"{self.month_name} {self.year_str} Part {self.part}"
            )

        # Exact client-facing delivery label. Part deliveries intentionally do
        # not add "Release"; rolling date ranges use the requested plural.
        self.client_delivery_label = (
            f"{self.month_display_folder} Releases"
            if self.is_date_range
            else (
                f"{self.month_display_folder} Release"
                if self.is_full_month
                else self.month_display_folder
            )
        )

        # May 2026 (Part 1), May 2026 (Part 2), or May 2026 (Full).
        self.month_display_text = (
            self.month_display_folder
            if self.is_date_range
            else (
                f"{self.month_name} {self.year_str} (Full)"
                if self.is_full_month
                else f"{self.month_name} {self.year_str} (Part {self.part})"
            )
        )

        # ---- Folder names ----------------------------------------------------
        # Both internal roots use the canonical release ID.
        self.specials_root = self.release_id
        self.hd_folder = self.release_id

        # ---- Release date range ---------------------------------------------
        last_day = monthrange(year, month)[1]
        if self.is_date_range:
            assert range_start is not None and range_end is not None
            self.release_start = range_start.isoformat()
            self.release_end = range_end.isoformat()
        elif previous_month or full_month_content:
            # Full calendar month: 1st → last day.
            self.release_start = f"{self.year_str}-{self.month_num}-01"
            self.release_end   = f"{self.year_str}-{self.month_num}-{last_day:02d}"
        elif part == 1:
            self.release_start = f"{self.year_str}-{self.month_num}-01"
            self.release_end   = f"{self.year_str}-{self.month_num}-14"
        else:
            self.release_start = f"{self.year_str}-{self.month_num}-15"
            self.release_end   = f"{self.year_str}-{self.month_num}-{last_day:02d}"

        # ---- Top-level directories ------------------------------------------
        self.specials_dir   = SPECIALS_BASE / self.specials_root
        self.hd_staging_dir = HD_STAGING_BASE / self.hd_folder
        self.hd_final_dir   = HD_FINAL_BASE / self.hd_folder

        # ---- Tracklist / metadata CSVs --------------------------------------
        self.us_tracklist_csv = (
            TRACKLISTS_DIR / f"UPM-US-{self.tracklist_token}-Tracklist.csv"
        )
        self.exus_tracklist_csv = (
            TRACKLISTS_DIR
            / "Ex-US"
            / f"UPM-ExUS-{self.tracklist_token}-Tracklist.csv"
        )
        self.album_list_csv = (
            TRACKLISTS_DIR
            / "Album Lists"
            / f"UPM-US-{self.tracklist_token}-AlbumList.csv"
        )

        # ---- Step 16: SoundMouse -------------------------------------------
        # SoundMouse's human-facing tracklist name uses an exclusive upper
        # bound (e.g. a June full-month export is 06-01-26 to 07-01-26), while
        # Domo and ActivationRange use the normal inclusive release_end.
        from datetime import date as _sm_date, timedelta as _sm_timedelta
        _sm_end_exclusive = (
            _sm_date.fromisoformat(self.release_end) + _sm_timedelta(days=1)
        )
        _sm_name_start = _sm_date.fromisoformat(self.release_start).strftime(
            "%m-%d-%y"
        )
        _sm_name_end = _sm_end_exclusive.strftime("%m-%d-%y")
        self.soundmouse_tracklist_csv = (
            TRACKLISTS_DIR / "SoundMouse"
            / f"Soundmouse {_sm_name_start} to {_sm_name_end}.csv"
        )
        self.soundmouse_bucket_csv = (
            TRACKLISTS_DIR / "SoundMouse"
            / f"Soundmouse Bucket {_sm_name_start} to {_sm_name_end}.csv"
        )
        self.soundmouse_activation_range = (
            f"{self.release_start}_to_{self.release_end}"
        )
        self.soundmouse_release_dir = (
            SOUNDMOUSE_BASE / self.soundmouse_activation_range
        )
        self.soundmouse_validation_report = (
            EXPORTS_DIR
            / f"SoundMouse {self.soundmouse_activation_range}_Missing.csv"
        )

        _japan_folder = self.partner_folder_name("Japan NTT DATA")
        self.japan_metadata_csv = (
            self.specials_dir
            / "3-FINAL PACKAGING"
            / _japan_folder
            / f"{self.month_display_folder} NTT Data Metadata.csv"
        )
        self.nbc_metadata_csv = (
            self.specials_dir
            / "1-ORIGINAL"
            / "Metadata"
            / f"UPM-US NBCUniversal Metadata Export-{self.release_variant}.csv"
        )

        # ---- Album list document paths --------------------------------------
        _doc_stem = (
            f"Universal Production Music - "
            f"{self.month_display_folder} Album List"
        )
        self.album_list_docx = self.hd_staging_dir / f"{_doc_stem}.docx"
        self.album_list_pdf  = self.hd_staging_dir / f"{_doc_stem}.pdf"

        # ---- Missing-files report -------------------------------------------
        from datetime import date as _date
        _today = _date.today().strftime("%m-%d-%Y")
        self.missing_report_csv = (
            EXPORTS_DIR
            / f"UPM {self.month_display_folder}_Missing_{_today}.csv"
        )

        # ---- Step 13: Non-MainTrack cleanup paths ---------------------------
        # The Tunesat delivery folder is the primary cleanup target.
        # CSV:    …/{client delivery label} - Tunesat/Metadata/UPM {mdf} Metadata.csv
        # Target: …/{client delivery label} - Tunesat/Music
        _tunesat_root = (
            self.specials_dir
            / "3-FINAL PACKAGING"
            / self.partner_folder_name("Tunesat")
        )
        self.cleanup_metadata_csv = (
            _tunesat_root / "Metadata" / f"UPM {self.month_display_folder} Metadata.csv"
        )
        self.cleanup_target_folder = _tunesat_root / "Music"

        # ---- Partner delivery directories (populated by helper) -------------
        self.partner_dirs = self._build_partner_dirs()

        # ---- SoundExchange: raw exports + ingest template live in STAGING,
        #      generated ISRC ingest workbooks ship from FINAL PACKAGING -------
        self.soundexchange_staging_dir = (
            self.specials_dir / "2-STAGING" / "SoundExchange"
        )
        self.soundexchange_final_dir = (
            self.specials_dir / "3-FINAL PACKAGING"
            / self.partner_folder_name("SoundExchange")
        )

        # ---- Partner deliverable metadata CSV/XLSX destinations -------------
        self.partner_metadata = self._build_partner_metadata()

        # ---- UniSync job definitions ----------------------------------------
        self.unisync_jobs = self._build_unisync_jobs()

    # -------------------------------------------------------------------------
    # Alternate constructors
    # -------------------------------------------------------------------------

    @classmethod
    def for_previous_month(
        cls,
        year: int | None = None,
        month: int | None = None,
    ) -> "ReleaseContext":
        """
        Build a context for the "Previous Month" full-month run.

        - If `year` and `month` are given, "previous month" is computed
          relative to THAT month (e.g. for 2026-06 → 2026-05).
        - If they are omitted, it is computed relative to TODAY's real date.

        Either way, the result is a single full-month context (no Part split):
        date range = 1st → last day, and names use the explicit
        "Month YYYY Full" form.

        Handles the January → December year rollover.
        """
        from datetime import date as _date

        if year is not None and month is not None:
            ref_year, ref_month = year, month
        elif year is None and month is None:
            today = _date.today()
            ref_year, ref_month = today.year, today.month
        else:
            raise ValueError(
                "for_previous_month: pass BOTH year and month, or NEITHER "
                "(to use today's date)."
            )

        if not (1 <= ref_month <= 12):
            raise ValueError(f"month must be 1-12, got {ref_month!r}")

        # Step back one month, rolling the year over at January.
        if ref_month == 1:
            prev_year, prev_month = ref_year - 1, 12
        else:
            prev_year, prev_month = ref_year, ref_month - 1

        return cls(year=prev_year, month=prev_month, part=1, previous_month=True)

    @classmethod
    def for_date_range(cls, start: str | date, end: str | date) -> "ReleaseContext":
        """Build an exact 14-day delivery context, including cross-month ranges."""
        start_date = date.fromisoformat(start) if isinstance(start, str) else start
        end_date = date.fromisoformat(end) if isinstance(end, str) else end
        return cls(
            year=start_date.year,
            month=start_date.month,
            part=1,
            range_start=start_date,
            range_end=end_date,
        )

    def partner_folder_name(self, partner: str) -> str:
        return f"Universal Production Music {self.client_delivery_label} - {partner}"

    def pinned_cli_args(self) -> list[str]:
        """Return CLI arguments that recreate this exact release context.

        ``--previous-month --year/--month`` accepts a reference month, so a
        resolved full-month context must pass the following month when handed
        to another machine. This prevents a Full run from reopening Part 1.
        """
        if self.is_date_range:
            return ["--start-date", self.release_start, "--end-date", self.release_end]
        if self.is_full_month:
            if self.month == 12:
                ref_year, ref_month = self.year + 1, 1
            else:
                ref_year, ref_month = self.year, self.month + 1
            return [
                "--previous-month",
                "--year", str(ref_year),
                "--month", str(ref_month),
            ]
        args = [
            "--year", str(self.year),
            "--month", str(self.month),
            "--part", str(self.part),
        ]
        if self.full_month_content:
            args.append("--full-month-content")
        return args

    # -------------------------------------------------------------------------
    # Internal builders
    # -------------------------------------------------------------------------

    def _build_partner_metadata(self) -> dict[str, Path]:
        """Destinations for the partner deliverable metadata exports (Step 1).

        Each partner's metadata lands in its 3-FINAL PACKAGING Metadata folder.
        Tunesat is NOT here — it reuses self.cleanup_metadata_csv (same path,
        and Step 13 reads it there).  NBC and NTT Data are NOT here either —
        they keep their existing destinations (nbc_metadata_csv is read by the
        Soundminer NBC step from 1-ORIGINAL; japan_metadata_csv already lands in
        the NTT DATA deliverable folder).
        """
        mdf = self.month_display_folder
        fp  = self.specials_dir / "3-FINAL PACKAGING"

        def _r(name: str) -> Path:
            return fp / self.partner_folder_name(name)

        return {
            "netmix":    _r("Netmix")    / "Metadata" / f"UPM {mdf} Metadata.csv",
            "synchtank": _r("SynchTank") / "Metadata" / f"UPM {mdf} Metadata.csv",
            "scripps":   _r("Scripps")   / "Metadata" / f"UPM {mdf} Metadata.csv",
            "qwire":     _r("Qwire")     / "Metadata"
                         / f"Qwire Library Submission Template \u2013 {mdf}.csv",
            "sourceaudio": _r("SourceAudio") / "Metadata"
                           / f"UPM {mdf} Metadata.csv",
            "sourceaudio_exus": _r("SourceAudio Ex-US") / "Metadata"
                                / f"UPM Ex-US {mdf} Metadata.csv",
            "soundexchange_mgb":    self.soundexchange_staging_dir / "Metadata"
                                    / "SoundExchange Universal Music - Mgb Na Llc.xlsx",
            "soundexchange_ztunes": self.soundexchange_staging_dir / "Metadata"
                                    / "SoundExchange Universal Music - Z Tunes, Llc.xlsx",
            # JMD/TSS is delivered as an Excel workbook, not CSV.
            "japan_jmdtss": fp / self.partner_folder_name("Japan JMD and TSS")
                            / f"{mdf} UPM Japan JMD TSS Metadata.xlsx",
        }

    def _build_partner_dirs(self) -> dict[str, Path]:
        mdf = self.month_display_folder
        fp  = self.specials_dir / "3-FINAL PACKAGING"
        hdf = self.hd_final_dir
        st  = self.specials_dir / "2-STAGING"

        def _r(name: str) -> Path:
            return fp / self.partner_folder_name(name)

        return {
            # MP3 destinations (Step 10)
            "tunesat_mp3":      _r("Tunesat")   / "Music",
            "discovery_mp3":    _r("Discovery") / "Music" / "MP3",
            "hd_mp3_media":     hdf / "MP3 (UDrive 2.0)" / f"Universal Production Music {self.client_delivery_label} (SM)" / "MEDIA",

            # WAV destinations (Step 10)
            "espn_wav":         _r("ESPN")      / "Music",
            "synchtank_wav":    _r("SynchTank") / "Music",
            "synchtank_covers": _r("SynchTank") / "Covers",
            "discovery_wav":    _r("Discovery") / "Music" / "WAV",
            "hd_wav_media":     hdf / "WAV (UDrive 2.0)" / f"Universal Production Music {self.client_delivery_label} (SW)" / "MEDIA",

            # WAV w COVERS destinations (Step 10)
            "nbc_staging_media": st / "SME WAV 48K NBC" / "MEDIA",
            "netmix_music":     _r("Netmix")    / "Music",

            # Ex-US destinations (Step 10)
            "exus_staging_media": st / "SME WAV ExUS" / "MEDIA",

            # Japan (Step 10)
            "japan_final_media": fp / self.partner_folder_name("Japan NTT DATA") / "MEDIA",

            # NBC music (Steps 12.6, 12.7)
            "nbc_wav_music":    _r("NBC") / "Music" / "WAV",
            "nbc_mp3_music":    _r("NBC") / "Music" / "MP3",
            "nbc_music_root":   _r("NBC") / "Music",

            # SourceAudio (Step 11 — Soundminer scan → AIFF mirror)
            "sourceaudio_music":      _r("SourceAudio") / "Music",
            "sourceaudio_exus_music": _r("SourceAudio Ex-US") / "Music",
        }

    def _build_unisync_jobs(self) -> list[dict]:
        music = self.specials_dir / "1-ORIGINAL" / "Music"

        return [
            {
                "name":        "US MP3",
                "territory":   "United States (MP3)",
                "cache_path":  str(UPM_CACHE_MP3),
                "client_path": str(music / "MP3"),
                "csv":         str(self.us_tracklist_csv),
            },
            {
                "name":        "US WAV",
                "territory":   "United States",
                "cache_path":  str(UPM_CACHE_WAV),
                "client_path": str(music / "WAV"),
                "csv":         str(self.us_tracklist_csv),
            },
            # NOTE: "US WAV w COVERS" is intentionally NOT a UniSync job.
            # WAV w COVERS is identical to the WAV download plus an album
            # cover dropped into each album folder, so re-downloading the
            # same ~5k WAVs through UniSync a second time is wasted time.
            # Instead the orchestrator builds WAV w COVERS by COPYING the
            # WAV tree (covers.build_wav_with_covers_from_wav) right after
            # the UniSync jobs, and Step 8 adds the cover images on top.
            {
                "name":        "Ex-US MP3",
                "territory":   "Rest of World (MP3)",
                "cache_path":  str(UPM_CACHE_MP3),
                "client_path": str(music / "Ex-US (MP3)"),
                "csv":         str(self.exus_tracklist_csv),
            },
            {
                "name":        "Ex-US WAV",
                "territory":   "Rest of World",
                "cache_path":  str(UPM_CACHE_WAV),
                "client_path": str(music / "Ex-US (WAV)"),
                "csv":         str(self.exus_tracklist_csv),
            },
            {
                "name":        "Japan WAV",
                "territory":   "Japan",
                "cache_path":  str(UPM_CACHE_WAV),
                "client_path": str(music / "Japan"),
                "csv":         str(self.japan_metadata_csv),
            },
        ]

    # -------------------------------------------------------------------------
    def get_cleanup_job(self, partner: str) -> dict:
        """
        Return paths for the non-maintrack cleanup step for any named partner.

        Pattern (confirmed against Tunesat example):
          Partner root: {specials_dir}/3-FINAL PACKAGING/
                        {ctx.partner_folder_name(partner)}
          Metadata CSV: {partner_root}/Metadata/UPM {mdf} Metadata.csv
          Music folder: {partner_root}/Music

        Usage:
            job = ctx.get_cleanup_job("Tunesat")
            # job["metadata_csv"]  → Path(…/Tunesat/Metadata/UPM May 2026 Metadata.csv)
            # job["music_folder"]  → Path(…/Tunesat/Music)
        """
        mdf = self.month_display_folder
        partner_root = (
            self.specials_dir
            / "3-FINAL PACKAGING"
            / self.partner_folder_name(partner)
        )
        return {
            "partner":      partner,
            "partner_root": partner_root,
            "metadata_csv": partner_root / "Metadata" / f"UPM {mdf} Metadata.csv",
            "music_folder": partner_root / "Music",
        }

    # -------------------------------------------------------------------------
    def summary(self) -> str:
        lines = [
            f"  Release ID:       {self.release_id}",
            f"  Year:             {self.year}",
            f"  Month:            {self.month_name} ({self.month_num})",
            f"  Release type:     {'Full' if self.is_full_month else f'Part {self.part}'}",
            f"  Release range:    {self.release_start} → {self.release_end}",
            f"  Tracklist token:  {self.tracklist_token}",
            f"  Specials root:    {self.specials_root}",
            f"  HD folder:        {self.hd_folder}",
            f"  Specials dir:     {self.specials_dir}",
            f"  HD staging dir:   {self.hd_staging_dir}",
            f"  HD final dir:     {self.hd_final_dir}",
            f"  US tracklist:     {self.us_tracklist_csv}",
            f"  Ex-US tracklist:  {self.exus_tracklist_csv}",
            f"  Album list CSV:   {self.album_list_csv}",
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"ReleaseContext(year={self.year}, month={self.month}, "
            f"part={self.part}, release_id={self.release_id!r})"
        )


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def context_from_cli_args(args) -> "ReleaseContext":
    """
    Build a ReleaseContext from parsed CLI args, supporting both normal
    (Part 1 / Part 2) runs and --previous-month full-month runs.

    Expects `args` to have: year, month, part, and (optionally)
    previous_month.  Centralises the validation so every module's standalone
    CLI behaves identically:

      • --previous-month with no year/month  → previous month of TODAY
      • --previous-month --year Y --month M   → previous month of Y-M
      • normal                                → requires year, month, part

    Raises ValueError with a clear message on an invalid combination.
    """
    previous = getattr(args, "previous_month", False)
    year  = getattr(args, "year", None)
    month = getattr(args, "month", None)
    part  = getattr(args, "part", None)
    start = getattr(args, "start_date", None)
    end = getattr(args, "end_date", None)
    full_month_content = getattr(args, "full_month_content", False)

    if start or end:
        if previous or year is not None or month is not None or part is not None:
            raise ValueError("--start-date/--end-date cannot be combined with month/part modes")
        if not start or not end:
            raise ValueError("--start-date and --end-date must be supplied together")
        if full_month_content:
            raise ValueError("--full-month-content is not valid with an exact date range")
        return ReleaseContext.for_date_range(start, end)

    if previous:
        if (year is None) ^ (month is None):
            raise ValueError(
                "--previous-month: pass BOTH --year and --month, or NEITHER "
                "(to use today's date)."
            )
        return ReleaseContext.for_previous_month(year=year, month=month)

    missing = [
        flag for flag, val in
        (("--year", year), ("--month", month), ("--part", part))
        if val is None
    ]
    if missing:
        raise ValueError(
            "Missing required argument(s): " + ", ".join(missing)
            + ".  (Or use --previous-month for a full-month run.)"
        )
    return ReleaseContext(
        year=year,
        month=month,
        part=part,
        full_month_content=full_month_content,
    )
