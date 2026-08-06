#!/usr/bin/env python3
"""Fail closed when credentials or per-user auth artifacts enter Git."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


CORPORATE_EMAIL = re.compile(
    r"[A-Z0-9._%+\-]+@(?:umusic|umg|universalmusic)\.[A-Z]{2,}", re.I
)
CORPORATE_EMAIL_GIT_PATTERN = r"[A-Z0-9._%+\-]+@(umusic|umg|universalmusic)\.[A-Z]{2,}"
LITERAL_SECRET = re.compile(
    r"(?i)\b(password|client_secret|access_token|refresh_token|api_key)\b"
    r"\s*[:=]\s*['\"][^'\"\r\n]{8,}['\"]"
)
PRIVATE_KEY_MARKER = "-----BEGIN " + "PRIVATE KEY-----"
SENSITIVE_BASENAMES = {
    "cookies", "login data", "web data", "unisync.xml", "unisync.xml.bak",
}
SENSITIVE_PARTS = {".auth", "domo_browser_profile"}


@dataclass(frozen=True)
class Finding:
    location: str
    rule: str


def scan_paths(paths: list[Path], root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        try:
            relative = path.resolve().relative_to(root.resolve())
        except ValueError:
            relative = path
        lowered_parts = {part.casefold() for part in relative.parts}
        if relative.name.casefold() in SENSITIVE_BASENAMES or lowered_parts & SENSITIVE_PARTS:
            findings.append(Finding(str(relative), "per-user authentication artifact"))
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:
            continue
        findings.extend(_scan_content(data, str(relative)))
    return findings


def _scan_content(data: bytes, location: str) -> list[Finding]:
    if b"\x00" in data:
        return []
    findings: list[Finding] = []
    text = data.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), 1):
        coordinate = f"{location}:{number}"
        if CORPORATE_EMAIL.search(line):
            findings.append(Finding(coordinate, "corporate email identity"))
        if LITERAL_SECRET.search(line):
            findings.append(Finding(coordinate, "literal credential assignment"))
        if PRIVATE_KEY_MARKER in line:
            findings.append(Finding(coordinate, "private key material"))
    return findings


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    )
    return [root / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]


def scan_tracked(root: Path) -> list[Finding]:
    return scan_paths(tracked_paths(root), root)


def scan_worktree(root: Path) -> list[Finding]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=True,
    )
    paths = [root / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]
    return scan_paths(paths, root)


def scan_index(root: Path) -> list[Finding]:
    """Scan exactly what Git would commit, not potentially-cleaner worktree files."""
    findings: list[Finding] = []
    names = subprocess.run(
        ["git", "ls-files", "-z", "--cached"], cwd=root,
        capture_output=True, check=True,
    ).stdout.split(b"\0")
    for raw in (item for item in names if item):
        name = raw.decode("utf-8")
        path = Path(name)
        parts = {part.casefold() for part in path.parts}
        if path.name.casefold() in SENSITIVE_BASENAMES or parts & SENSITIVE_PARTS:
            findings.append(Finding(name, "per-user authentication artifact"))
            continue
        blob = subprocess.run(
            ["git", "show", f":{name}"], cwd=root, capture_output=True
        )
        if blob.returncode == 0:
            findings.extend(_scan_content(blob.stdout, name))
    return findings


def scan_history(root: Path) -> list[Finding]:
    """Audit every reachable commit without printing the sensitive value."""
    findings: list[Finding] = []
    commits = subprocess.run(
        ["git", "rev-list", "--all"], cwd=root, capture_output=True,
        text=True, check=True,
    ).stdout.splitlines()
    for commit in commits:
        names = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", commit], cwd=root,
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        for name in names:
            path = Path(name)
            parts = {part.casefold() for part in path.parts}
            if path.name.casefold() in SENSITIVE_BASENAMES or parts & SENSITIVE_PARTS:
                findings.append(Finding(f"{commit[:12]}:{name}", "auth artifact in history"))
        grep = subprocess.run(
            ["git", "grep", "-I", "-i", "-n", "-E", CORPORATE_EMAIL_GIT_PATTERN, commit, "--"],
            cwd=root, capture_output=True, text=True,
        )
        if grep.returncode not in (0, 1):
            raise RuntimeError(grep.stderr.strip())
        for match in grep.stdout.splitlines():
            # git grep output is commit:path:line:value. Retain only coordinates.
            pieces = match.split(":", 3)
            coordinate = ":".join(pieces[:3]) if len(pieces) >= 3 else commit[:12]
            findings.append(Finding(coordinate, "corporate email identity in history"))
    authors = subprocess.run(
        ["git", "log", "--all", "--format=%H%x00%ae"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    for row in authors:
        commit, _, email = row.partition("\0")
        if CORPORATE_EMAIL.fullmatch(email.strip()):
            findings.append(Finding(commit[:12], "corporate author email in history"))
    return findings


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan Git for private authentication material")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--history", action="store_true", help="Scan every reachable commit")
    scope.add_argument("--index", action="store_true", help="Scan exactly the staged Git index")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent
    findings = (
        scan_history(root) if args.history
        else scan_index(root) if args.index
        else scan_worktree(root)
    )
    if findings:
        print("SECURITY SCAN FAILED (values redacted):", file=sys.stderr)
        for finding in findings:
            print(f"  {finding.location} — {finding.rule}", file=sys.stderr)
        return 1
    scope_name = "history" if args.history else "staged index" if args.index else "worktree"
    print(f"Security scan passed: no private auth material in {scope_name}.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
