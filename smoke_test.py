#!/usr/bin/env python3
"""
smoke_test.py — fast, offline sanity check for the UPM workflow.

Runs in seconds, needs no Pegasus volumes and touches nothing on disk.  It
catches the failure modes that bite during refactors: a broken import or
cross-module reference, an arg/step registry that drifted out of sync, or the
shared column matcher getting un-shared.  Run it after every edit and before
pushing:

    python3 smoke_test.py

Exits 0 if all checks pass, 1 otherwise.  For a deeper check on a machine with
the volumes mounted, also run the orchestrator in dry-run:

    python3 upm_release_workflow.py --previous-month --dry-run
"""

from __future__ import annotations

import importlib
import sys

# Every first-party module — importing all of them catches broken cross-imports
# (e.g. a helper that moved modules) before they fail mid-run.
MODULES = [
    "config", "tracklist_columns", "logging_utils",
    "covers", "verification", "final_metadata_verification", "remediation",
    "final_packaging", "cleanup", "audio_conversion", "domo_exports",
    "split_se_ingest_forms", "folder_setup", "album_list_doc", "soundminer",
    "make_soundminer_crops", "prune", "unisync_automation", "unisync_prefs",
    "remote_runner", "soundmouse", "upm_release_workflow",
]

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))
        failures.append(label)


def main() -> int:
    print("UPM workflow smoke test\n")

    # 1) Every module imports cleanly.
    print("Imports:")
    mods = {}
    for name in MODULES:
        try:
            mods[name] = importlib.import_module(name)
            print(f"  ok   import {name}")
        except Exception as exc:                       # noqa: BLE001
            print(f"  FAIL import {name} — {exc!r}")
            failures.append(f"import {name}")
    if "upm_release_workflow" not in mods or "config" not in mods:
        print("\nCore modules failed to import — aborting further checks.")
        return 1

    wf = mods["upm_release_workflow"]
    config = mods["config"]

    # 2) ReleaseContext builds and exposes the SoundExchange dirs we rely on.
    print("\nContext:")
    try:
        ctx = config.ReleaseContext(year=2026, month=5, part=1)
        check("ReleaseContext builds", True)
        check("soundexchange_staging_dir present",
              hasattr(ctx, "soundexchange_staging_dir"))
        check("soundexchange_final_dir present",
              hasattr(ctx, "soundexchange_final_dir"))
        check("partner_metadata has SoundExchange xlsx",
              str(ctx.partner_metadata["soundexchange_mgb"]).endswith(".xlsx"))
        check("SoundMouse tracklist uses exclusive end date",
              ctx.soundmouse_tracklist_csv.name ==
              "Soundmouse 05-01-26 to 05-15-26.csv")
        check("SoundMouse ActivationRange uses inclusive end date",
              ctx.soundmouse_activation_range ==
              "2026-05-01_to_2026-05-14")
        check("SoundMouse validation report is a CSV",
              ctx.soundmouse_validation_report.suffix == ".csv")
    except Exception as exc:                           # noqa: BLE001
        check("ReleaseContext builds", False, repr(exc))

    # 3) Arg parser builds, and --only/--start-at tokens parse.
    print("\nArg parser:")
    try:
        parser = wf.build_parser()
        check("build_parser()", True)
        dests = {a.dest for a in parser._actions}
    except Exception as exc:                           # noqa: BLE001
        check("build_parser()", False, repr(exc))
        return 1

    # 4) Step registry <-> argparse consistency (the drift guard).
    print("\nStep registry consistency:")
    step_attrs = {attr for (_t, _p, attr, _n) in wf._STEP_UNITS if attr}
    # every skip flag a step references must be a real parser dest
    for attr in sorted(step_attrs):
        check(f"_STEP_UNITS attr '{attr}' is a real flag", attr in dests)
    # every skip-flag in _ALL_SKIP_ATTRS must be a real parser dest …
    for attr in wf._ALL_SKIP_ATTRS:
        check(f"_ALL_SKIP_ATTRS '{attr}' is a real flag", attr in dests)
    # … and every argparse --skip-* dest must be registered in _ALL_SKIP_ATTRS
    parser_skips = {d for d in dests if d.startswith("skip_")}
    for d in sorted(parser_skips):
        check(f"parser flag '{d}' is registered in _ALL_SKIP_ATTRS",
              d in wf._ALL_SKIP_ATTRS)
    # --list-steps exists and renders
    check("--list-steps flag exists", "list_steps" in dests)
    try:
        check("format_step_list() renders all tokens",
              all(t in wf.format_step_list() for t in wf._STEP_TOKENS))
    except Exception as exc:                           # noqa: BLE001
        check("format_step_list() renders all tokens", False, repr(exc))

    # 5) Shared column matcher is actually shared (today's consolidation).
    print("\nShared column helpers:")
    tc = mods["tracklist_columns"]
    for name in ("covers", "verification", "final_metadata_verification",
                 "remediation"):
        m = mods.get(name)
        check(f"{name}._find_column is the shared one",
              getattr(m, "_find_column", None) is tc._find_column)

    print()
    if failures:
        print(f"SMOKE TEST FAILED — {len(failures)} check(s): "
              + ", ".join(failures))
        return 1
    print("SMOKE TEST PASSED — all checks green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
