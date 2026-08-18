"""Authentic small-batch SourceAudio demonstration for screen recording."""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import SPECIALS_BASE, context_from_cli_args


FILES_DIR = Path(__file__).resolve().parent
TOTAL_STEPS = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run an authentic US SourceAudio demo: Domo metadata, UniSync WAV, "
            "WAV w COVERS staging, HDF1 Soundminer AIFF mirror, final reveal."
        )
    )
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--part", type=int, choices=[1, 2])
    parser.add_argument("--previous-month", action="store_true")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--tracks", type=int, default=20, metavar="N")
    parser.add_argument("--sourceaudio-db-shortcut", default="8", metavar="KEY")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without opening apps, submitting HDF1 work, or changing files.",
    )
    return parser


def _display_command(command: list[str]) -> str:
    import shlex

    return shlex.join(command)


def _announce(step: int, title: str, explanation: str) -> None:
    print("\n" + "═" * 72)
    print(f"DEMO STEP {step}/{TOTAL_STEPS} — {title}")
    print(explanation)
    print("═" * 72, flush=True)


def _copy_demo_batch(source_csv: Path, destination_csv: Path, tracks: int) -> None:
    from unisync_automation import _write_limited_test_csv

    temporary = _write_limited_test_csv(
        str(source_csv), tracks, logging.getLogger("ai_team_demo")
    )
    if temporary is None:
        raise RuntimeError("Could not build the varied SourceAudio demo metadata.")
    try:
        destination_csv.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(temporary, destination_csv)
    finally:
        temporary.unlink(missing_ok=True)


def _workaudioids(metadata_csv: Path) -> set[str]:
    from tracklist_columns import POSSIBLE_EXTERNAL_ID_COLS, _find_column

    with metadata_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        id_column = _find_column(fields, POSSIBLE_EXTERNAL_ID_COLS)
        if not id_column:
            raise RuntimeError("SourceAudio metadata has no External Id/WorkAudioId column.")
        return {
            (row.get(id_column) or "").strip()
            for row in reader
            if (row.get(id_column) or "").strip()
        }


def _metadata_id_filename_map(metadata_csv: Path) -> dict[str, str]:
    from soundminer import _normalise_audio_identity
    from tracklist_columns import (
        POSSIBLE_EXTERNAL_ID_COLS,
        POSSIBLE_FILENAME_COLS,
        _find_column,
    )

    with metadata_csv.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        id_column = _find_column(fields, POSSIBLE_EXTERNAL_ID_COLS)
        filename_column = _find_column(fields, POSSIBLE_FILENAME_COLS)
        if not id_column or not filename_column:
            raise RuntimeError("SourceAudio metadata lacks External Id or Filename.")
        return {
            (row.get(id_column) or "").strip(): _normalise_audio_identity(
                row.get(filename_column) or ""
            )
            for row in reader
            if (row.get(id_column) or "").strip()
            and (row.get(filename_column) or "").strip()
        }


def _build_unisync_request(metadata_csv: Path, destination: Path) -> None:
    from sourceaudio_delta import _write_unisync_request

    ids = _workaudioids(metadata_csv)
    temporary = _write_unisync_request(metadata_csv, ids)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _expected_files(request_csv: Path, extension: str) -> set[str]:
    from unisync_automation import _expected_output_filenames

    return _expected_output_filenames(
        str(request_csv), extension, logging.getLogger("ai_team_demo")
    )


def _source_files(root: Path, expected: set[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    duplicates: set[str] = set()
    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file() or path.name not in expected:
                continue
            if path.name in found:
                duplicates.add(path.name)
            else:
                found[path.name] = path
    if duplicates:
        raise RuntimeError(
            "Duplicate staged filenames make SourceAudio packaging ambiguous: "
            + ", ".join(sorted(duplicates))
        )
    return found


def _archive_demo_root(demo_root: Path) -> None:
    if not demo_root.exists():
        return
    archive = (
        SPECIALS_BASE
        / "_AI Team Demo Archive"
        / datetime.now().strftime("%Y%m%d-%H%M%S")
        / demo_root.name
    )
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(demo_root), str(archive))
    print(f"  ARCHIVE  {demo_root}")
    print(f"        →  {archive}")


def _quit_unisync() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "UniSync" to quit'],
        capture_output=True,
        text=True,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.tracks <= 0:
        parser.error("--tracks must be positive")

    try:
        ctx = context_from_cli_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    demo_root = SPECIALS_BASE / f"{ctx.release_id}-AI-SOURCEAUDIO-DEMO"
    demo_label = f"{ctx.client_delivery_label} AI Demo"
    original_metadata = demo_root / "1-ORIGINAL" / "Metadata" / "SourceAudio"
    domo_full_csv = original_metadata / "SourceAudio Full Domo Export.csv"
    selected_metadata = original_metadata / "SourceAudio 20 Track Metadata.csv"
    unisync_request = original_metadata / "SourceAudio 20 WorkAudioIds.csv"
    original_wav = demo_root / "1-ORIGINAL" / "Music" / "WAV"
    soundminer_source = (
        demo_root / "1-ORIGINAL" / "Music" / "WAV w COVERS" / "MEDIA"
    )
    final_partner = (
        demo_root
        / "3-FINAL PACKAGING"
        / f"Universal Production Music {demo_label} - SourceAudio"
    )
    final_music = final_partner / "Music"
    final_metadata = final_partner / "Metadata"
    final_metadata_file = final_metadata / "UPM AI Team Demo SourceAudio Metadata.csv"
    manifest_csv = final_partner / "AI Team Demo SourceAudio Manifest.csv"
    pinned = ctx.pinned_cli_args()

    domo_command = [
        sys.executable,
        str(FILES_DIR / "domo_exports.py"),
        "--test",
        *pinned,
        "--only",
        "sourceaudio_metadata",
        "--output-path",
        str(domo_full_csv),
    ]
    unisync_command = [
        sys.executable,
        str(FILES_DIR / "unisync_automation.py"),
        "--test",
        *pinned,
        "--job",
        "US WAV",
        "--csv-path",
        str(unisync_request),
        "--client-path",
        str(original_wav),
        "--capture-steps",
        "--timeout",
        "0.06",
    ]

    print("UPM AI-team authentic SourceAudio demo")
    print(f"Release:          {ctx.release_id} ({ctx.release_start} → {ctx.release_end})")
    print(f"Batch:            {args.tracks} varied SourceAudio tracks")
    print(f"Demo release:     {demo_root}")
    print(f"Original WAV:     {original_wav}")
    print(f"Soundminer input: {soundminer_source}")
    print(f"Final deliverable:{final_partner}")
    print("\nDomo command:")
    print(_display_command(domo_command))
    print("\nUniSync command:")
    print(_display_command(unisync_command))
    print("\nSoundminer: normal HDF1 login-session agent, US SourceAudio pair only")

    if args.dry_run:
        print("\nDRY RUN — no apps opened, no HDF1 request submitted, no files changed.")
        return 0

    logger = logging.getLogger("ai_team_demo")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    demo_ctx = context_from_cli_args(args)
    demo_ctx.specials_dir = demo_root
    demo_ctx.client_delivery_label = demo_label
    demo_ctx.partner_dirs = demo_ctx._build_partner_dirs()
    demo_ctx.soundminer_checkpoint_id = demo_root.name

    try:
        _archive_demo_root(demo_root)
        original_metadata.mkdir(parents=True, exist_ok=True)
        original_wav.mkdir(parents=True, exist_ok=True)
        soundminer_source.mkdir(parents=True, exist_ok=True)
        final_music.mkdir(parents=True, exist_ok=True)
        final_metadata.mkdir(parents=True, exist_ok=True)

        _announce(
            1,
            "Domo SourceAudio metadata",
            "Domo exports the real US SourceAudio metadata card. Twenty varied rows are selected, and their External Id values become the WorkAudioIds used for audio retrieval.",
        )
        subprocess.run(domo_command, check=True, timeout=120)
        if not domo_full_csv.is_file():
            raise RuntimeError("Domo did not produce the SourceAudio metadata CSV.")
        _copy_demo_batch(domo_full_csv, selected_metadata, args.tracks)
        ids = _workaudioids(selected_metadata)
        if len(ids) != args.tracks:
            raise RuntimeError(
                f"Expected {args.tracks} unique WorkAudioIds, found {len(ids)}."
            )
        _build_unisync_request(selected_metadata, unisync_request)
        print(f"  METADATA      {selected_metadata}")
        print(f"  WORKAUDIOIDS  {len(ids)}")

        _announce(
            2,
            "UniSync WAV into Original",
            "The normal US WAV UniSync job reads the WorkAudioId request and downloads the matching masters into the demo release's 1-ORIGINAL Music WAV tree.",
        )
        subprocess.run(unisync_command, check=True, timeout=220)
        _quit_unisync()
        expected_wav = _expected_files(unisync_request, ".wav")
        originals = _source_files(original_wav, expected_wav)
        missing = expected_wav - set(originals)
        if missing:
            raise RuntimeError(f"UniSync verification found {len(missing)} missing WAVs.")
        print(f"  VERIFIED  {len(originals)}/{len(expected_wav)} Original WAV files")

        _announce(
            3,
            "Prepare the Soundminer source",
            "As in the US SourceAudio workflow, the verified masters are prepared under WAV w COVERS. This is the exact source tree Soundminer will scan.",
        )
        from sourceaudio_delta import _download_addition_covers

        if not _download_addition_covers(
            demo_ctx, selected_metadata, original_wav, ids, logger
        ):
            raise RuntimeError("One or more SourceAudio covers could not be prepared.")
        shutil.copytree(original_wav, soundminer_source, dirs_exist_ok=True)
        staged_wav = _source_files(soundminer_source, expected_wav)
        if set(staged_wav) != expected_wav:
            raise RuntimeError("WAV w COVERS staging verification failed.")
        subprocess.run(["open", str(soundminer_source)], check=False)

        _announce(
            4,
            "Soundminer AIFF mirror on HDF1",
            "The normal HDF1 login-session agent switches to the SourceAudio database, scans the staged WAVs, applies the complete SourceAudio mirror profile, embeds metadata, and creates AIFF files in the final SourceAudio Music folder.",
        )
        from soundminer_agent import run_via_agent

        ok = run_via_agent(
            demo_ctx,
            "sourceaudio",
            False,
            logger,
            options={
                "capture_steps": True,
                "db_shortcut": args.sourceaudio_db_shortcut,
                "specials_dir_override": str(demo_root),
                "client_label_override": demo_label,
                "sourceaudio_us_only": True,
            },
        )
        if not ok:
            raise RuntimeError("The HDF1 SourceAudio Soundminer job failed.")

        from soundminer import (
            SOURCEAUDIO_OUTPUT_EXTS,
            _sourceaudio_manifest,
            _validate_destination_manifest,
        )

        expected_identities = _sourceaudio_manifest(soundminer_source)
        _validate_destination_manifest(
            final_music,
            expected_identities,
            SOURCEAUDIO_OUTPUT_EXTS,
            logger,
            "AI Team Demo SourceAudio",
            allow_empty=False,
        )
        shutil.copy2(selected_metadata, final_metadata_file)

        _announce(
            5,
            "Show the final SourceAudio deliverable",
            "The final partner folder now contains the selected SourceAudio metadata and verified AIFF media produced by Soundminer. The manifest records the WorkAudioIds and final filenames.",
        )
        final_aiffs = sorted(
            path for path in final_music.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".aif", ".aiff"}
        )
        from soundminer import _normalise_audio_identity

        final_by_identity = {
            _normalise_audio_identity(path.name): path for path in final_aiffs
        }
        id_to_identity = _metadata_id_filename_map(selected_metadata)
        with manifest_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["WorkAudioId", "Final AIFF"])
            for track_id in sorted(ids):
                writer.writerow(
                    [track_id, str(final_by_identity[id_to_identity[track_id]])]
                )
        subprocess.run(["open", str(final_partner)], check=False)
        print("\n✓ AUTHENTIC SOURCEAUDIO DEMO COMPLETED")
        print(f"  WorkAudioIds:    {len(ids)}")
        print(f"  AIFF files:      {len(final_aiffs)}")
        print(f"  Final package:   {final_partner}")
        return 0

    except subprocess.TimeoutExpired:
        _quit_unisync()
        print("\n✗ Demo stopped safely because Domo or UniSync exceeded its limit.")
        return 1
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        _quit_unisync()
        print(f"\n✗ Demo stopped safely: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
