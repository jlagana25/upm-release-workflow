#!/usr/bin/env python3
"""Login-session Soundminer agent and HDF2 control client.

The daemon is installed as a per-user macOS LaunchAgent on HDF1.  That is the
important distinction from SSH: launchd starts it inside the logged-in Aqua
session, so Screen Recording and Accessibility can reach Soundminer.  HDF2
submits atomic JSON over SSH into HDF1's local queue and monitors results
without ever launching Soundminer locally or attempting GUI work over SSH.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import shutil
import shlex
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import (
    REMOTE_SOUNDMINER,
    SOUNDMINER_AGENT_HEARTBEAT_TIMEOUT,
    SOUNDMINER_AGENT_JOB_TIMEOUT,
    SOUNDMINER_AGENT_POLL_SECONDS,
    SOUNDMINER_AGENT_ROOT,
    SOUNDMINER_HOSTNAME,
    ReleaseContext,
    current_hostname,
)

PROTOCOL_VERSION = 1
AGENT_LABEL = "com.upm.soundminer-agent"
FILES_DIR = Path(__file__).resolve().parent
REPO_ROOT = FILES_DIR.parent
AGENT_LOG_DIR = REPO_ROOT / "_logs" / "soundminer_agent"
INSTALLED_ROOT = Path.home() / "Library" / "Application Support" / "UPM Soundminer Agent"
INSTALLED_FILES_DIR = INSTALLED_ROOT / "files"
LAUNCH_AGENT_LOG_DIR = Path.home() / "Library" / "Logs" / "UPM Soundminer Agent"


def _uses_remote_transport(root: Path) -> bool:
    return root == SOUNDMINER_AGENT_ROOT and current_hostname() != SOUNDMINER_HOSTNAME.upper()


def _remote_agent_command(*arguments: str) -> list[str]:
    target = f"{REMOTE_SOUNDMINER['user']}@{REMOTE_SOUNDMINER['host']}"
    remote = shlex.join([
        "/usr/bin/python3",
        str(INSTALLED_FILES_DIR / "soundminer_agent.py"),
        *arguments,
    ])
    return ["ssh", target, remote]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _paths(root: Path = SOUNDMINER_AGENT_ROOT) -> dict[str, Path]:
    return {
        "root": root,
        "pending": root / "pending",
        "running": root / "running",
        "completed": root / "completed",
        "status": root / "status",
        "agent": root / "agent.json",
    }


def _ensure_dirs(root: Path = SOUNDMINER_AGENT_ROOT) -> dict[str, Path]:
    paths = _paths(root)
    for key in ("pending", "running", "completed", "status"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def agent_health(root: Path = SOUNDMINER_AGENT_ROOT) -> tuple[bool, str]:
    if _uses_remote_transport(root):
        try:
            result = subprocess.run(
                _remote_agent_command("--status"),
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as exc:
            return False, f"could not query HDF1 agent over SSH: {exc}"
        detail = (result.stdout or result.stderr).strip()
        return result.returncode == 0, detail or f"HDF1 status exited {result.returncode}"
    paths = _paths(root)
    path = paths["agent"]
    if not path.exists():
        return False, f"agent heartbeat does not exist: {path}"
    try:
        payload = _read_json(path)
        age = (datetime.now(timezone.utc) - _parse_utc(payload["heartbeat_at"])).total_seconds()
    except Exception as exc:
        return False, f"agent heartbeat is unreadable: {exc}"
    if payload.get("host", "").upper() != SOUNDMINER_HOSTNAME.upper():
        return False, f"heartbeat came from unexpected host {payload.get('host')!r}"
    if age > SOUNDMINER_AGENT_HEARTBEAT_TIMEOUT:
        # The single-worker daemon waits synchronously for soundminer.py. Its
        # idle heartbeat pauses during a job, while the request status is
        # refreshed by each workflow progress line. Treat that fresh HDF1-owned
        # status as authoritative so a restarted HDF2 can safely reattach.
        for status_path in sorted(paths["status"].glob("*.json")):
            try:
                status = _read_json(status_path)
                status_age = (
                    datetime.now(timezone.utc)
                    - _parse_utc(str(status["heartbeat_at"]))
                ).total_seconds()
            except Exception:
                continue
            if (
                status.get("state") == "running"
                and str(status.get("host", "")).upper() == SOUNDMINER_HOSTNAME.upper()
                and status_age <= SOUNDMINER_AGENT_HEARTBEAT_TIMEOUT
            ):
                return (
                    True,
                    "HDF1 agent busy with "
                    f"{status.get('request_id', status_path.stem)} "
                    f"({int(status_age)}s progress heartbeat age)",
                )
        return False, f"agent heartbeat is stale ({int(age)}s old)"
    return True, f"HDF1 agent online ({int(age)}s heartbeat age)"


def submit_request(
    ctx: ReleaseContext,
    workflow: str,
    logger: logging.Logger,
    *,
    options: dict[str, Any] | None = None,
    root: Path = SOUNDMINER_AGENT_ROOT,
) -> str:
    if workflow not in {"sourceaudio", "nbc", "probe"}:
        raise ValueError(f"Unsupported Soundminer agent workflow: {workflow}")
    request_id = (
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-"
        f"{ctx.release_id.lower()}-{workflow}-{uuid.uuid4().hex[:8]}"
    )
    request = {
        "protocol": PROTOCOL_VERSION,
        "request_id": request_id,
        "created_at": _utc_now(),
        "created_by": current_hostname(),
        "release_id": ctx.release_id,
        "workflow": workflow,
        "pinned_args": ctx.pinned_cli_args(),
        "options": options or {},
    }
    if _uses_remote_transport(root):
        result = subprocess.run(
            _remote_agent_command("--accept-request"),
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode:
            raise RuntimeError(
                "HDF1 rejected the Soundminer request: "
                + (result.stderr or result.stdout).strip()
            )
        accepted_id = result.stdout.strip().splitlines()[-1]
        logger.info(f"  Submitted HDF1 Soundminer request: {accepted_id}")
        return accepted_id
    return _accept_request(request, logger, root=root)


def _accept_request(
    request: dict[str, Any],
    logger: logging.Logger,
    *,
    root: Path = SOUNDMINER_AGENT_ROOT,
) -> str:
    workflow = request.get("workflow")
    if workflow not in {"sourceaudio", "nbc", "probe"}:
        raise ValueError(f"Unsupported Soundminer agent workflow: {workflow}")
    paths = _ensure_dirs(root)
    # If HDF2 was restarted while HDF1 kept working, attach to that request
    # instead of queueing a duplicate destructive database run.
    for directory in (paths["running"], paths["pending"]):
        for existing_path in sorted(directory.glob("*.json")):
            try:
                existing = _read_json(existing_path)
            except Exception:
                continue
            if (
                existing.get("release_id") == request.get("release_id")
                and existing.get("workflow") == workflow
                and existing.get("options", {}).get("specials_dir_override")
                == request.get("options", {}).get("specials_dir_override")
            ):
                existing_id = str(existing.get("request_id", existing_path.stem))
                logger.warning(
                    f"  ↻ Reattaching to existing HDF1 request: {existing_id}"
                )
                return existing_id
    request_id = str(request["request_id"])
    _atomic_json(paths["pending"] / f"{request_id}.json", request)
    _atomic_json(paths["status"] / f"{request_id}.json", {
        "protocol": PROTOCOL_VERSION,
        "request_id": request_id,
        "state": "queued",
        "phase": "waiting for HDF1 agent",
        "heartbeat_at": _utc_now(),
        "host": current_hostname(),
    })
    logger.info(f"  Submitted HDF1 Soundminer request: {request_id}")
    return request_id


def wait_for_request(
    request_id: str,
    logger: logging.Logger,
    *,
    root: Path = SOUNDMINER_AGENT_ROOT,
    timeout: int = SOUNDMINER_AGENT_JOB_TIMEOUT,
    heartbeat_timeout: int = SOUNDMINER_AGENT_HEARTBEAT_TIMEOUT,
) -> bool:
    if _uses_remote_transport(root):
        process = subprocess.Popen(
            _remote_agent_command("--wait-request", request_id),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.strip()
            if line:
                logger.info(f"  HDF1 {line}")
        return process.wait() == 0
    status_path = _paths(root)["status"] / f"{request_id}.json"
    started = time.monotonic()
    last_rendered: tuple[str, str] | None = None
    while time.monotonic() - started <= timeout:
        try:
            status = _read_json(status_path)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            time.sleep(SOUNDMINER_AGENT_POLL_SECONDS)
            continue
        state = str(status.get("state", "unknown"))
        phase = str(status.get("phase", ""))
        rendered = (state, phase)
        if rendered != last_rendered:
            logger.info(f"  HDF1 [{state}] {phase}".rstrip())
            last_rendered = rendered
        if state == "completed":
            logger.info(f"  ✓ HDF1 Soundminer request completed: {request_id}")
            return True
        if state == "failed":
            logger.error(
                f"  ✗ HDF1 Soundminer request failed: {request_id}\n"
                f"     {status.get('message', phase)}"
            )
            if status.get("failure_screenshot"):
                logger.error(f"     Screenshot: {status['failure_screenshot']}")
            if status.get("log_path"):
                logger.error(f"     Agent log: {status['log_path']}")
            return False
        try:
            age = (
                datetime.now(timezone.utc)
                - _parse_utc(str(status["heartbeat_at"]))
            ).total_seconds()
        except Exception:
            age = heartbeat_timeout + 1
        if state == "running" and age > heartbeat_timeout:
            logger.error(
                f"  ✗ HDF1 Soundminer heartbeat stopped for {int(age)}s. "
                "The GUI agent may have crashed or its local queue stalled."
            )
            return False
        time.sleep(SOUNDMINER_AGENT_POLL_SECONDS)
    logger.error(f"  ✗ HDF1 Soundminer request exceeded {timeout}s: {request_id}")
    return False


def run_via_agent(
    ctx: ReleaseContext,
    workflow: str,
    dry_run: bool,
    logger: logging.Logger,
    *,
    options: dict[str, Any] | None = None,
    root: Path = SOUNDMINER_AGENT_ROOT,
) -> bool:
    if dry_run:
        logger.info(
            f"  [DRY RUN] Would submit unattended HDF1 agent job: {workflow} "
            f"for {ctx.release_id}."
        )
        return True
    healthy, detail = agent_health(root)
    if not healthy:
        logger.error(
            f"  ✗ HDF1 Soundminer agent unavailable: {detail}\n"
            "     On HDF1, install/start it once with:\n"
            "       python3 soundminer_agent.py --install"
        )
        return False
    logger.info(f"  ✓ {detail}")
    request_id = submit_request(ctx, workflow, logger, options=options, root=root)
    return wait_for_request(request_id, logger, root=root)


def _status_update(
    status_path: Path,
    request: dict[str, Any],
    state: str,
    phase: str,
    **extra: Any,
) -> None:
    payload = {
        "protocol": PROTOCOL_VERSION,
        "request_id": request["request_id"],
        "release_id": request.get("release_id"),
        "workflow": request.get("workflow"),
        "state": state,
        "phase": phase,
        "heartbeat_at": _utc_now(),
        "host": current_hostname(),
        "pid": os.getpid(),
    }
    payload.update(extra)
    _atomic_json(status_path, payload)


def _build_command(request: dict[str, Any]) -> list[str]:
    workflow = request["workflow"]
    command = [sys.executable, str(FILES_DIR / "soundminer.py")]
    if workflow in {"sourceaudio", "nbc"}:
        command.append(f"--{workflow}")
    elif workflow == "probe":
        command.extend(["--nbc", "--preflight-only"])
    command.extend(str(value) for value in request.get("pinned_args", []))
    options = request.get("options", {})
    if options.get("capture_steps"):
        command.append("--capture-steps")
    if options.get("resume"):
        command.append("--resume")
    if options.get("restart_app"):
        command.append("--restart-app")
    for option_name, flag in (
        ("skip_delete_records", "--skip-delete-records"),
        ("skip_import", "--skip-import"),
        ("skip_embed", "--skip-embed"),
        ("skip_mirror", "--skip-mirror"),
    ):
        if options.get(option_name):
            command.append(flag)
    for option_name, flag in (
        ("specials_dir_override", "--specials-dir-override"),
        ("client_label_override", "--client-label-override"),
        ("nbc_metadata_override", "--nbc-metadata-override"),
    ):
        if options.get(option_name):
            command.extend([flag, str(options[option_name])])
    if workflow == "sourceaudio" and options.get("db_shortcut"):
        command.extend(["--sourceaudio-db-shortcut", str(options["db_shortcut"])])
    if workflow == "sourceaudio" and options.get("sourceaudio_us_only"):
        command.append("--sourceaudio-us-only")
    return command


def _run_in_login_terminal(
    command: list[str],
    request: dict[str, Any],
    status_path: Path,
    log_path: Path,
    logger: logging.Logger,
) -> tuple[int, str]:
    """Run GUI automation under Terminal's existing macOS TCC grants.

    The LaunchAgent owns the queue but cannot itself capture the display.
    Opening a short ``.command`` file in the logged-in Terminal keeps execution
    in the Aqua session and attributes Screen Recording/Accessibility to the
    already-approved Terminal app. The agent tails progress and remains the
    sole authority that marks the request complete or failed.
    """
    request_id = str(request["request_id"])
    wrapper = AGENT_LOG_DIR / f"{request_id}.command"
    result_path = AGENT_LOG_DIR / f"{request_id}.exit"
    result_tmp = result_path.with_suffix(".exit.tmp")
    for stale in (wrapper, result_path, result_tmp):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    shell_lines = [
        "#!/bin/zsh",
        f"cd {shlex.quote(str(FILES_DIR))}",
        # Keep the HDF1 display and Aqua session awake for the entire GUI job.
        # If the console locks, macOS returns wallpaper-only screenshots and
        # neither pyautogui nor Accessibility can handle Soundminer dialogs.
        f"/usr/bin/caffeinate -dimsu {shlex.join(command)} > "
        f"{shlex.quote(str(log_path))} 2>&1",
        "upm_exit_code=$?",
        f"printf '%s\\n' \"$upm_exit_code\" > {shlex.quote(str(result_tmp))}",
        f"mv {shlex.quote(str(result_tmp))} {shlex.quote(str(result_path))}",
        "exit \"$upm_exit_code\"",
    ]
    wrapper.write_text("\n".join(shell_lines) + "\n", encoding="utf-8")
    wrapper.chmod(0o700)
    opened = subprocess.run(
        ["open", "-a", "Terminal", str(wrapper)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if opened.returncode:
        raise RuntimeError(
            "Could not dispatch GUI job to HDF1 Terminal: "
            + opened.stderr.strip()
        )

    started = time.monotonic()
    last_position = 0
    last_phase = "running in HDF1 Terminal"
    failure_screenshot = ""
    next_heartbeat = 0.0
    while time.monotonic() - started <= SOUNDMINER_AGENT_JOB_TIMEOUT:
        if log_path.exists():
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(last_position)
                while True:
                    raw = handle.readline()
                    if not raw:
                        break
                    line = raw.strip()
                    if not line:
                        continue
                    last_phase = line[-500:]
                    logger.info("[%s] %s", request_id, line)
                    if "Failure screenshot saved:" in line:
                        failure_screenshot = line.split(
                            "Failure screenshot saved:", 1
                        )[1].strip()
                last_position = handle.tell()
        now = time.monotonic()
        if now >= next_heartbeat:
            _status_update(
                status_path, request, "running", last_phase,
                command=command, log_path=str(log_path),
                failure_screenshot=failure_screenshot,
            )
            next_heartbeat = now + 10
        if result_path.exists():
            try:
                exit_code = int(result_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                time.sleep(1)
                continue
            try:
                wrapper.unlink()
                result_path.unlink()
            except OSError:
                pass
            return exit_code, failure_screenshot
        time.sleep(1)
    raise RuntimeError(
        f"HDF1 Terminal job exceeded {SOUNDMINER_AGENT_JOB_TIMEOUT}s"
    )


def _process_request(request_path: Path, root: Path, logger: logging.Logger) -> None:
    paths = _ensure_dirs(root)
    running_path = paths["running"] / request_path.name
    try:
        request_path.replace(running_path)
    except FileNotFoundError:
        return
    request = _read_json(running_path)
    status_path = paths["status"] / running_path.name
    request_id = request.get("request_id", running_path.stem)
    log_path = AGENT_LOG_DIR / f"{request_id}.log"
    AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    _status_update(status_path, request, "running", "agent accepted request", log_path=str(log_path))
    exit_code = 1
    failure_screenshot = ""
    try:
        if request.get("protocol") != PROTOCOL_VERSION:
            raise ValueError(f"Unsupported protocol {request.get('protocol')!r}")
        if current_hostname() != SOUNDMINER_HOSTNAME.upper():
            raise RuntimeError(
                f"Agent must run on {SOUNDMINER_HOSTNAME}, not {current_hostname()}"
            )
        command = _build_command(request)
        _status_update(
            status_path, request, "running", "launching Soundminer workflow",
            command=command, log_path=str(log_path),
        )
        exit_code, failure_screenshot = _run_in_login_terminal(
            command, request, status_path, log_path, logger
        )
        if exit_code:
            raise RuntimeError(f"soundminer.py exited {exit_code}")
        detail = (
            "Soundminer GUI/crop/Accessibility preflight passed"
            if request.get("workflow") == "probe"
            else "Soundminer workflow completed"
        )
        _status_update(
            status_path, request, "completed", detail,
            command=command, log_path=str(log_path), exit_code=0,
        )
    except Exception as exc:
        _status_update(
            status_path, request, "failed", "Soundminer workflow failed",
            message=f"{type(exc).__name__}: {exc}", log_path=str(log_path),
            failure_screenshot=failure_screenshot, exit_code=exit_code,
        )
        logger.exception("Request %s failed", request_id)
    finally:
        destination = paths["completed"] / running_path.name
        if running_path.exists():
            running_path.replace(destination)


def serve(
    *,
    root: Path = SOUNDMINER_AGENT_ROOT,
    once: bool = False,
    poll_seconds: int = SOUNDMINER_AGENT_POLL_SECONDS,
) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )
    logger = logging.getLogger("soundminer_agent")
    paths = _ensure_dirs(root)
    if current_hostname() != SOUNDMINER_HOSTNAME.upper():
        logger.error("This daemon may only run on %s.", SOUNDMINER_HOSTNAME)
        return 2
    logger.info("Soundminer agent online; queue=%s", paths["pending"])

    def heartbeat() -> None:
        _atomic_json(paths["agent"], {
            "protocol": PROTOCOL_VERSION,
            "host": current_hostname(),
            "pid": os.getpid(),
            "state": "online",
            "heartbeat_at": _utc_now(),
            "queue": str(paths["pending"]),
        })

    while True:
        heartbeat()
        pending = sorted(paths["pending"].glob("*.json"))
        if pending:
            _process_request(pending[0], root, logger)
            heartbeat()
        if once:
            return 0
        time.sleep(max(1, poll_seconds))


def _select_agent_python() -> Path:
    """Choose an HDF1 Python that has the GUI automation dependencies.

    ``--install`` is often invoked over SSH, where macOS resolves ``python3``
    to the Command Line Tools interpreter instead of the Framework Python used
    by the logged-in Terminal. Probe known candidates rather than persisting
    that accidental SSH interpreter in the LaunchAgent.
    """
    configured = os.environ.get("UPM_SOUNDMINER_PYTHON")
    candidates = [
        configured,
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/opt/homebrew/bin/python3",
        sys.executable,
        shutil.which("python3"),
    ]
    failures: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        if not raw:
            continue
        candidate = str(Path(raw).resolve())
        if candidate in seen or not Path(candidate).exists():
            continue
        seen.add(candidate)
        probe = subprocess.run(
            [candidate, "-c", "import pyautogui, PIL, cv2"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return Path(candidate)
        failures.append(f"{candidate}: {probe.stderr.strip().splitlines()[-1:]}")
    raise RuntimeError(
        "No Python interpreter with pyautogui, Pillow, and OpenCV was found. "
        "Install the GUI requirements on HDF1 or set UPM_SOUNDMINER_PYTHON. "
        f"Tried: {'; '.join(failures)}"
    )


def _deploy_runtime_copy() -> Path:
    """Copy the small code/runtime tree outside macOS-protected Documents."""
    INSTALLED_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        FILES_DIR,
        INSTALLED_FILES_DIR,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )
    return INSTALLED_FILES_DIR / "soundminer_agent.py"


def install_launch_agent() -> Path:
    if current_hostname() != SOUNDMINER_HOSTNAME.upper():
        raise RuntimeError(f"Install this agent on {SOUNDMINER_HOSTNAME} only")
    uid = os.getuid()
    launch_dir = Path.home() / "Library" / "LaunchAgents"
    launch_dir.mkdir(parents=True, exist_ok=True)
    plist_path = launch_dir / f"{AGENT_LABEL}.plist"
    agent_python = _select_agent_python()
    installed_script = _deploy_runtime_copy()
    LAUNCH_AGENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [str(agent_python), str(installed_script), "--serve"],
        "WorkingDirectory": str(INSTALLED_FILES_DIR),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(LAUNCH_AGENT_LOG_DIR / "launchagent.out.log"),
        "StandardErrorPath": str(LAUNCH_AGENT_LOG_DIR / "launchagent.err.log"),
    }
    temporary = plist_path.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(payload, handle)
    temporary.replace(plist_path)
    domain = f"gui/{uid}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{AGENT_LABEL}"], check=True)
    return plist_path


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HDF1 Soundminer login-session agent")
    parser.add_argument("--serve", action="store_true", help="Run the persistent queue daemon")
    parser.add_argument("--once", action="store_true", help="Process at most one queued request")
    parser.add_argument("--install", action="store_true", help="Install/start the HDF1 LaunchAgent")
    parser.add_argument("--status", action="store_true", help="Print current shared agent health")
    parser.add_argument("--accept-request", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wait-request", metavar="ID", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.install:
        path = install_launch_agent()
        print(f"Installed and started: {path}")
        return 0
    if args.status:
        ok, detail = agent_health()
        print(detail)
        return 0 if ok else 1
    if args.accept_request:
        request = json.load(sys.stdin)
        request_id = _accept_request(
            request, logging.getLogger("soundminer_agent.accept")
        )
        print(request_id)
        return 0
    if args.wait_request:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        return 0 if wait_for_request(
            args.wait_request, logging.getLogger("soundminer_agent.wait")
        ) else 1
    if args.serve or args.once:
        return serve(once=args.once)
    parser.error("choose --serve, --once, --install, or --status")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
