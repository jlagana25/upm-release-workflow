#!/usr/bin/env python3
"""
remote_runner.py
================

Run Step 12 (Soundminer NBC workflow) on a REMOTE Mac over SSH.

Soundminer v5Pro lives on a separate Mac on the same network.  This
machine runs Steps 1–11, 13 and 14; Step 12 has to execute on the Soundminer
Mac, where the GUI, the app, and the reference screenshots all are.  Both
Pegasus volumes are mounted at identical paths on both machines, so the
data hand-off is automatic — this box stages the WAVs into
`2-STAGING/SME WAV 48K NBC/MEDIA`, the remote Mac reads them.

The hard part is macOS, not the network
---------------------------------------
A process launched over SSH lands in a non-GUI bootstrap context and
cannot reach the WindowServer — so pyautogui, screencapture, and
AppleScript UI scripting all fail with a black screen or permission
errors.  Two things make remote GUI automation work:

  1. **Session injection.**  We wrap the remote command in
     `launchctl asuser <uid> …` (see config.REMOTE_SOUNDMINER["gui_wrapper"])
     so it runs inside the logged-in user's Aqua session.  `launchctl
     asuser` needs root, hence the `sudo` in the default wrapper; configure
     passwordless sudo for it, or run an attended session and type the
     password at the prompt (we allocate a TTY so the prompt is visible).

  2. **TCC permissions.**  On the remote Mac, grant **Accessibility** and
     **Screen Recording** to the process that drives the UI (Terminal/SSH
     daemon/python — whichever ends up being the responsible process).
     One-time, in System Settings → Privacy & Security.

Because none of this can be validated from the pipeline machine in the
abstract, run the **smoke test** first:

    python3 remote_runner.py --smoke-test

It checks, in order: SSH reachability → remote repo present → remote
pyautogui importable → GUI reachable (screencapture returns a real frame).
Each stage prints PASS/FAIL with a targeted fix hint, so you learn exactly
which of the two gotchas (if any) is in play before committing to a
multi-hour real run.

Real run (attended first time, so you can watch via Screen Sharing and
answer the import/embed/mirror handshakes):

    python3 remote_runner.py --test --year 2026 --month 5 --part 1

The orchestrator calls run_soundminer_remote() automatically when
config.REMOTE_SOUNDMINER_ENABLED is True.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from config import ReleaseContext, REMOTE_SOUNDMINER, REMOTE_SOUNDMINER_ENABLED


# SSH options shared by every invocation.  BatchMode is NOT set, because we
# may legitimately need an interactive sudo / key passphrase prompt; instead
# we set a short connect timeout so an unreachable host fails fast rather
# than hanging.
_SSH_BASE_OPTS = [
    "-o", "ConnectTimeout=10",
    "-o", "StrictHostKeyChecking=accept-new",
]


class RemoteError(RuntimeError):
    """Raised when the remote invocation can't be set up or fails its preflight."""


# ---------------------------------------------------------------------------
# Command construction (pure functions — unit-testable without a network)
# ---------------------------------------------------------------------------

def _ssh_target(cfg: dict) -> str:
    return f'{cfg["user"]}@{cfg["host"]}'


def _wrap_for_gui_session(
    inner_shell: str,
    cfg: dict,
    *,
    non_interactive: bool = False,
) -> str:
    """
    Wrap a shell command string so it runs inside the remote user's GUI
    (Aqua) session.

    `inner_shell` is a full shell command line (may contain &&, quoting,
    spaces).  It's handed to `/bin/bash -lc <inner_shell>` so the remote
    shell interprets it, and that bash invocation is what the GUI wrapper
    (launchctl asuser …) execs.

    non_interactive=True swaps every `sudo ` for `sudo -n ` so a missing
    passwordless-sudo rule fails FAST (with "a password is required")
    instead of hanging on an invisible password prompt.  The smoke test
    uses this; the real run leaves sudo interactive (a -t TTY makes the
    prompt answerable).

    Returns the complete remote command string to pass to ssh.
    """
    # `/bin/bash -lc '<inner_shell>'` — single program + args that launchctl
    # can exec.  shlex.quote makes inner_shell a single safe token.
    bash_cmd = f"/bin/bash -lc {shlex.quote(inner_shell)}"

    wrapper = cfg.get("gui_wrapper", "{cmd}")
    remote_cmd = (
        wrapper
        .replace("{uid}",  str(cfg.get("uid", "")))
        .replace("{user}", str(cfg.get("user", "")))
        .replace("{cmd}",  bash_cmd)
    )
    if non_interactive:
        remote_cmd = remote_cmd.replace("sudo ", "sudo -n ")
    return remote_cmd


def build_soundminer_remote_command(
    ctx: ReleaseContext,
    cfg: dict,
    *,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    """
    Build the full local argv (the `ssh …` command) that, when run, launches
    soundminer.py on the remote Mac inside its GUI session.

    extra_args are appended to the remote soundminer.py invocation
    (e.g. ["--unattended", "--capture-steps"]).
    """
    extra_args = extra_args or []

    repo   = cfg["repo_path"]
    python = cfg.get("python", "python3")

    # The soundminer.py invocation, as a shell command line run from repo.
    sm_args = [
        python, "soundminer.py", "--test",
        *ctx.pinned_cli_args(),
        *extra_args,
    ]
    # cd into the repo (quoted — it has spaces) then run soundminer.
    inner_shell = f"cd {shlex.quote(repo)} && " + " ".join(
        shlex.quote(a) for a in sm_args
    )

    remote_cmd = _wrap_for_gui_session(inner_shell, cfg)

    # -t allocates a TTY so interactive prompts (sudo password, soundminer's
    # import/embed/mirror handshakes) are visible and answerable locally.
    return ["ssh", "-t", *_SSH_BASE_OPTS, _ssh_target(cfg), remote_cmd]


# ---------------------------------------------------------------------------
# Smoke test — validate connectivity + GUI reachability before a real run
# ---------------------------------------------------------------------------

def _run_ssh(cmd_argv: list[str], *, timeout: int = 25) -> subprocess.CompletedProcess:
    return subprocess.run(cmd_argv, capture_output=True, text=True, timeout=timeout)


def _run_ssh_safe(
    cmd_argv: list[str], *, timeout: int = 25,
) -> Optional[subprocess.CompletedProcess]:
    """Run ssh; return None on timeout instead of raising (so the smoke test
    reports a clean FAIL rather than crashing with a traceback)."""
    try:
        return _run_ssh(cmd_argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def _password_required(text: str) -> bool:
    """Detect sudo's 'needs a password' responses so we can route the
    operator to the passwordless-sudo fix instead of a vague failure."""
    t = text.lower()
    return (
        "a password is required" in t
        or "a terminal is required to read the password" in t
        or "sudo: no tty present" in t
    )


def smoke_test(logger: logging.Logger, cfg: Optional[dict] = None) -> bool:
    """
    Staged preflight.  Returns True only if every stage passes.

    Stages 2–5 run THROUGH the GUI-session wrapper (launchctl asuser …)
    with `sudo -n`, so they (a) test in the same Aqua-session context the
    real run uses — where GUI-mounted volumes like /Volumes/hdfuser are
    actually visible — and (b) fail fast if passwordless sudo isn't set up
    rather than hanging on an invisible password prompt.

    Each stage prints PASS/FAIL with a targeted fix hint.
    """
    cfg = cfg or REMOTE_SOUNDMINER
    target = _ssh_target(cfg)
    repo   = cfg["repo_path"]
    python = cfg.get("python", "python3")
    ok = True

    logger.info(f"Smoke test against {target}")
    logger.info(f"  remote repo: {repo}")

    # Stage 1 — plain SSH reachability (no sudo, no GUI)
    r = _run_ssh_safe(["ssh", *_SSH_BASE_OPTS, target, "echo SSH_OK"], timeout=15)
    if r is None:
        logger.error(
            "  [1/5] SSH reachability ............ FAIL (timeout)\n"
            "        Host didn't answer.  Check hostname/IP and network."
        )
        return False
    if r.returncode == 0 and "SSH_OK" in r.stdout:
        logger.info("  [1/5] SSH reachability ............ PASS")
    else:
        logger.error(
            "  [1/5] SSH reachability ............ FAIL\n"
            f"        ssh exited {r.returncode}: {r.stderr.strip()}\n"
            "        Fix: confirm Remote Login is ON and `ssh " + target + "`\n"
            "        works by hand."
        )
        return False

    # Stage 2 — GUI-session injection + passwordless sudo.  This is the
    # gateway for everything else: it proves we can enter the Aqua session
    # via `sudo -n launchctl asuser <uid>` without a password prompt.
    probe = _wrap_for_gui_session("echo GUI_OK", cfg, non_interactive=True)
    r = _run_ssh_safe(["ssh", "-t", *_SSH_BASE_OPTS, target, probe], timeout=25)
    if r is None:
        logger.error(
            "  [2/5] GUI session injection ....... FAIL (timeout)\n"
            "        The launchctl/sudo probe hung.  Usually a password prompt\n"
            "        with no passwordless-sudo rule — see the sudoers fix below."
        )
        _print_sudoers_hint(logger, cfg)
        return False
    combined = (r.stdout or "") + (r.stderr or "")
    if "GUI_OK" in combined:
        logger.info("  [2/5] GUI session injection ....... PASS")
    elif _password_required(combined):
        ok = False
        logger.error(
            "  [2/5] GUI session injection ....... FAIL (needs password)\n"
            "        `sudo launchctl asuser` requires a password and no\n"
            "        passwordless-sudo rule is in place."
        )
        _print_sudoers_hint(logger, cfg)
        return False
    else:
        ok = False
        logger.error(
            "  [2/5] GUI session injection ....... FAIL\n"
            f"        Output: {combined.strip()[:400]}\n"
            "        Likely causes:\n"
            f"        • Wrong uid (config has {cfg.get('uid')!r}; confirm with\n"
            "          `id -u` while logged into the remote Mac's GUI).\n"
            "        • No one is logged into the GUI / Screen Sharing not\n"
            "          connected, so there's no Aqua session to enter.\n"
            "        • launchctl path differs; check `which launchctl` remotely."
        )
        return False

    # Helper to run an in-GUI-context probe and fetch combined output.
    def _gui_probe(inner: str, timeout: int = 30) -> Optional[str]:
        cmd = _wrap_for_gui_session(inner, cfg, non_interactive=True)
        res = _run_ssh_safe(["ssh", "-t", *_SSH_BASE_OPTS, target, cmd],
                            timeout=timeout)
        if res is None:
            return None
        return (res.stdout or "") + (res.stderr or "")

    # Stage 3 — repo present, checked IN GUI CONTEXT (so /Volumes/hdfuser is
    # mounted and visible, exactly as it will be for the real run).
    out = _gui_probe(
        f"test -f {shlex.quote(repo + '/soundminer.py')} && echo REPO_OK "
        f"|| echo REPO_MISSING"
    )
    if out is None:
        ok = False
        logger.error("  [3/5] Remote repo present ......... FAIL (timeout)")
    elif "REPO_OK" in out:
        logger.info("  [3/5] Remote repo present ......... PASS")
    else:
        ok = False
        logger.error(
            "  [3/5] Remote repo present ......... FAIL\n"
            f"        soundminer.py not found (in GUI context) under {repo}\n"
            "        Fix: copy the project's files/ folder to that path on the\n"
            "        remote Mac, or correct repo_path in config.  (This check\n"
            "        now runs in the GUI session, so a GUI-mounted volume not\n"
            "        being visible to plain SSH is no longer the culprit.)"
        )

    # Stage 4 — remote python can import pyautogui (in GUI context, same
    # interpreter the real run uses).
    out = _gui_probe(
        f"{shlex.quote(python)} -c 'import pyautogui' && echo PYAUTOGUI_OK "
        f"|| echo PYAUTOGUI_MISSING"
    )
    if out is None:
        ok = False
        logger.error("  [4/5] Remote pyautogui import ..... FAIL (timeout)")
    elif "PYAUTOGUI_OK" in out:
        logger.info("  [4/5] Remote pyautogui import ..... PASS")
    else:
        ok = False
        logger.error(
            "  [4/5] Remote pyautogui import ..... FAIL\n"
            f"        `{python} -c 'import pyautogui'` failed on the remote.\n"
            "        Fix: on the remote Mac, run\n"
            f"        `{python} -m pip install pyautogui Pillow`\n"
            "        (use the SAME interpreter — match python in config)."
        )

    # Stage 5 — GUI reachable: capture the screen THE SAME WAY the real run
    # does — through python/pyautogui — so the TCC "responsible process" is
    # the python interpreter (the binary you grant Screen Recording to), not
    # the bare screencapture CLI.  A permission-denied capture comes back as
    # an all-black frame rather than an error, so we detect that explicitly
    # instead of trusting file size alone.
    py_code = (
        "import pyautogui;"
        "im=pyautogui.screenshot();"
        "ex=im.getextrema();"
        "mx=[e[1] for e in ex] if type(ex[0])==tuple else [ex[1]];"
        'print("SMOKE_BLACK="+("1" if all(m==0 for m in mx) else "0"));'
        'print("SMOKE_SIZE=%dx%d"%im.size)'
    )
    inner = f"{shlex.quote(python)} -c {shlex.quote(py_code)}"
    out = _gui_probe(inner, timeout=40)
    if out is None:
        ok = False
        logger.error("  [5/5] GUI reachable (pyautogui) ... FAIL (timeout)")
    elif "SMOKE_BLACK=0" in out:
        size = _parse_token(out, "SMOKE_SIZE=") or "?"
        logger.info(
            f"  [5/5] GUI reachable (pyautogui) ... PASS  (captured {size})"
        )
    elif "SMOKE_BLACK=1" in out:
        ok = False
        size = _parse_token(out, "SMOKE_SIZE=") or "?"
        logger.error(
            "  [5/5] GUI reachable (pyautogui) ... FAIL (black frame)\n"
            f"        pyautogui captured a {size} all-black image — the\n"
            "        capture mechanism works but Screen Recording permission\n"
            "        is DENIED for the python interpreter.\n"
            "        ── Fix (on the remote Mac via Screen Sharing) ──\n"
            "        System Settings → Privacy & Security → Screen Recording\n"
            "        → click '+' → press \u2318\u21e7G and paste the python binary\n"
            f"        path:\n          {python if python.startswith('/') else _resolve_python_hint(python)}\n"
            "        → enable it → you may be asked to quit/relaunch (the SSH\n"
            "        session doesn't need relaunching; just re-run the smoke\n"
            "        test).  If '+' won't take a non-.app binary, see notes."
        )
    else:
        ok = False
        logger.error(
            "  [5/5] GUI reachable (pyautogui) ... FAIL\n"
            f"        Unexpected probe output: {out.strip()[:400]}\n"
            "        pyautogui may have errored before capturing.  Check it\n"
            "        imports cleanly on the remote (Stage 4 passed, so likely a\n"
            "        runtime/display issue)."
        )

    logger.info(f"Smoke test {'PASSED' if ok else 'FAILED'}.")
    return ok


def _parse_token(text: str, prefix: str) -> Optional[str]:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line.split("=", 1)[1]
    return None


def _resolve_python_hint(python: str) -> str:
    """Best-effort hint for where the python binary lives when config uses a
    bare name like 'python3' (so the Screen Recording '+' dialog has a path
    to paste).  Purely advisory."""
    return (
        f"(run `which {python}` on the remote to get the full path, then add "
        f"that binary)"
    )


def _print_sudoers_hint(logger: logging.Logger, cfg: dict) -> None:
    """Print the exact passwordless-sudo rule for the launchctl injection."""
    uid  = cfg.get("uid", "504")
    user = cfg.get("user", "hdfuser")
    logger.error(
        "        ── Passwordless-sudo fix (run ON the remote Mac) ──\n"
        "        sudo visudo -f /etc/sudoers.d/upm-soundminer\n"
        "        then add these two lines (exactly):\n"
        f"          {user} ALL=(root) NOPASSWD: /bin/launchctl asuser {uid} *\n"
        f"          {user} ALL=({user}) NOPASSWD: /bin/bash *\n"
        "        Save and exit.  Re-run the smoke test.\n"
        "        (Alternatively, run the real workflow attended — the -t TTY\n"
        "         lets you type the password — but unattended runs need this.)"
    )


# ---------------------------------------------------------------------------
# Public entry — run Step 12 remotely
# ---------------------------------------------------------------------------

def run_soundminer_remote(
    ctx:        ReleaseContext,
    dry_run:    bool,
    logger:     logging.Logger,
    *,
    cfg:        Optional[dict] = None,
    extra_args: Optional[list[str]] = None,
    run_smoke:  bool = True,
) -> bool:
    """
    Trigger Step 12 on the remote Soundminer Mac over SSH.

    dry_run    : Log the exact remote command and return True without
                 connecting — useful for verifying the wiring.
    run_smoke  : Run the staged smoke test first (default True).  A real
                 (non-dry) run aborts if the smoke test fails, so we never
                 kick off a multi-hour job into a broken session.
    extra_args : Passed through to the remote soundminer.py (e.g.
                 ["--unattended"], ["--capture-steps"]).

    Returns True on remote exit status 0.
    """
    cfg = cfg or REMOTE_SOUNDMINER
    logger.info("─── Step 12 (REMOTE) — Soundminer on " f"{_ssh_target(cfg)} ──")

    argv = build_soundminer_remote_command(ctx, cfg, extra_args=extra_args)
    # For display, render the argv the way a shell would show it.
    shown = " ".join(shlex.quote(a) for a in argv)

    if dry_run:
        logger.info("  [DRY RUN] Would run remote command:")
        logger.info(f"    {shown}")
        logger.info("            Nothing executed.")
        return True

    if run_smoke:
        logger.info("  Running pre-flight smoke test first…")
        if not smoke_test(logger, cfg):
            logger.error(
                "  ✗  Smoke test failed — NOT starting the remote workflow.\n"
                "     Fix the failing stage above and re-run.  You can re-test\n"
                "     in isolation with:  python3 remote_runner.py --smoke-test"
            )
            return False

    logger.info("  Launching remote Soundminer workflow…")
    logger.info(f"    {shown}")
    logger.info(
        "  (Watch the remote Mac via Screen Sharing; answer any import/embed/\n"
        "   mirror handshake prompts HERE in this terminal.)"
    )

    # Do NOT capture output — let it stream to this terminal so the operator
    # sees soundminer.py's progress live and can answer its input() prompts.
    try:
        result = subprocess.run(argv)
    except KeyboardInterrupt:
        logger.warning(
            "  ⚠  Remote run interrupted locally.  The remote Soundminer job\n"
            "     may still be running — reconnect via Screen Sharing to check,\n"
            "     and re-run individual phases with the soundminer --skip-* flags."
        )
        return False
    except FileNotFoundError:
        logger.error(
            "  ✗  `ssh` not found on this machine.  Install/enable OpenSSH "
            "client."
        )
        return False

    if result.returncode == 0:
        logger.info("  ✓  Remote Soundminer workflow completed (exit 0).")
        return True

    logger.error(
        f"  ✗  Remote Soundminer workflow exited {result.returncode}.\n"
        "     Check the streamed output above and the remote failure\n"
        "     screenshots under the remote Mac's\n"
        "     _Logs/UPM Release Workflow/soundminer_failures/ folder."
    )
    return False


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def _run_cli(argv: Optional[list[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Run Step 12 (Soundminer) on the remote Mac over SSH.",
    )
    p.add_argument("--smoke-test", action="store_true",
                   help="Run only the staged connectivity/GUI smoke test.")
    p.add_argument("--test", action="store_true",
                   help="Run the remote workflow (requires --year/--month/--part).")
    p.add_argument("--year",  type=int)
    p.add_argument("--month", type=int)
    p.add_argument("--part",  type=int, choices=[1, 2])
    p.add_argument("--dry-run", action="store_true",
                   help="Print the remote command without connecting.")
    p.add_argument("--no-smoke", action="store_true",
                   help="Skip the pre-run smoke test (not recommended).")
    p.add_argument("--unattended", action="store_true",
                   help="Pass --unattended through to the remote soundminer.py.")
    p.add_argument("--capture-steps", action="store_true",
                   help="Pass --capture-steps through to the remote soundminer.py.")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("remote_runner")

    if args.smoke_test:
        return 0 if smoke_test(logger) else 1

    if not args.test:
        p.error("pass --smoke-test, or --test with --year/--month/--part")
    if args.year is None or args.month is None or args.part is None:
        p.error("--test requires --year, --month, and --part")

    ctx = ReleaseContext(year=args.year, month=args.month, part=args.part)
    extra: list[str] = []
    if args.unattended:
        extra.append("--unattended")
    if args.capture_steps:
        extra.append("--capture-steps")

    ok = run_soundminer_remote(
        ctx, dry_run=args.dry_run, logger=logger,
        extra_args=extra, run_smoke=not args.no_smoke,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_run_cli())
