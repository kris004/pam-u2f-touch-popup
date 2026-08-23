#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration tests for pam-u2f-touch-popup."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


BINARY = Path(sys.argv[1] if len(sys.argv) > 1 else "./pam-u2f-touch-popup").resolve()


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def read_records(log: Path) -> list[dict[str, object]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def event_count(log: Path, event: str) -> int:
    return sum(record["event"] == event for record in read_records(log))


def write_fake_zenity(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys\n"
        "log = os.environ['ZENITY_LOG']\n"
        "def record(event):\n"
        "    with open(log, 'a') as stream:\n"
        "        stream.write(json.dumps({'event': event, 'pid': os.getpid(), 'argv': sys.argv[1:]}) + '\\n')\n"
        "def stop(signum, frame):\n"
        "    record('stop')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "record('start')\n"
        "signal.pause()\n"
    )
    path.chmod(0o755)


def terminate_process(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def popup_identity_matches(pid: int, log: Path) -> bool:
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False

    expected_log = os.fsencode(f"ZENITY_LOG={log}")
    expected_script = os.fsencode(log.parent / "zenity")
    return expected_log in environ and expected_script in command


def cleanup_popups(log: Path) -> None:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return

    records = read_records(log)
    stopped = {int(record["pid"]) for record in records if record["event"] == "stop"}
    for record in records:
        if record["event"] != "start":
            continue
        pid = int(record["pid"])
        if pid in stopped:
            continue
        try:
            pidfd = os.pidfd_open(pid)
        except OSError:
            continue
        try:
            if popup_identity_matches(pid, log):
                signal.pidfd_send_signal(pidfd, signal.SIGKILL)
        except OSError:
            pass
        finally:
            os.close(pidfd)


@contextmanager
def running_helper(
    *,
    precreate: bool = True,
    delay_ms: str = "0",
    display: bool = True,
    extra_env: dict[str, str] | None = None,
) -> Iterator[tuple[subprocess.Popen[bytes], Path, Path, Path]]:
    with tempfile.TemporaryDirectory() as tmp_string:
        tmp = Path(tmp_string)
        pending = tmp / "pam-u2f-authpending"
        if precreate:
            pending.touch()
        log = tmp / "zenity.log"
        zenity = tmp / "zenity"
        write_fake_zenity(zenity)

        env = os.environ.copy()
        env.update(
            {
                "PAM_U2F_AUTHPENDING_FILE": str(pending),
                "PAM_U2F_TOUCH_DELAY_MS": delay_ms,
                "PAM_U2F_ZENITY": str(zenity),
                "ZENITY_LOG": str(log),
            }
        )
        if display:
            env["WAYLAND_DISPLAY"] = "wayland-test"
        else:
            env.pop("DISPLAY", None)
            env.pop("WAYLAND_DISPLAY", None)
        if extra_env:
            env.update(extra_env)

        proc = subprocess.Popen(
            [str(BINARY)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.1)
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise AssertionError(f"helper exited early ({proc.returncode}): {stderr}")

        try:
            yield proc, pending, log, tmp
        finally:
            terminate_process(proc)
            cleanup_popups(log)
            if proc.returncode != 0:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                raise AssertionError(f"helper exited with {proc.returncode}: {stderr}")


def open_pending(path: Path, *, create: bool = False) -> int:
    flags = os.O_RDONLY | (os.O_CREAT if create else 0)
    return os.open(path, flags, 0o664)


def test_preexisting_open_close_and_default_copy() -> None:
    with running_helper() as (_, pending, log, _):
        fd = open_pending(pending)
        try:
            assert wait_for(lambda: event_count(log, "start") == 1)
            record = next(record for record in read_records(log) if record["event"] == "start")
            assert record["argv"] == [
                "--info",
                "--title",
                "Security key touch required",
                "--text",
                "Touch your security key to approve authentication.",
                "--no-markup",
                "--width=480",
            ]
        finally:
            os.close(fd)
        assert wait_for(lambda: event_count(log, "stop") == 1)


def test_first_create_is_observed() -> None:
    with running_helper(precreate=False) as (_, pending, log, _):
        fd = open_pending(pending, create=True)
        try:
            assert wait_for(lambda: event_count(log, "start") == 1)
        finally:
            os.close(fd)
        assert wait_for(lambda: event_count(log, "stop") == 1)


def test_brief_open_is_debounced() -> None:
    with running_helper(delay_ms="250") as (_, pending, log, _):
        fd = open_pending(pending)
        time.sleep(0.05)
        os.close(fd)
        time.sleep(0.35)
        assert read_records(log) == []


def test_long_open_survives_debounce() -> None:
    with running_helper(delay_ms="250") as (_, pending, log, _):
        fd = open_pending(pending)
        try:
            assert wait_for(lambda: event_count(log, "start") == 1)
        finally:
            os.close(fd)
        assert wait_for(lambda: event_count(log, "stop") == 1)


def test_unrelated_file_is_ignored() -> None:
    with running_helper() as (_, _, log, tmp):
        unrelated = tmp / "unrelated"
        unrelated.touch()
        fd = open_pending(unrelated)
        time.sleep(0.2)
        os.close(fd)
        time.sleep(0.1)
        assert read_records(log) == []


def test_no_display_does_not_spawn_popup() -> None:
    with running_helper(display=False) as (_, pending, log, _):
        fd = open_pending(pending)
        time.sleep(0.2)
        os.close(fd)
        time.sleep(0.1)
        assert read_records(log) == []


def test_delete_and_recreate_resets_state() -> None:
    with running_helper() as (_, pending, log, _):
        first_fd = open_pending(pending)
        assert wait_for(lambda: event_count(log, "start") == 1)
        pending.unlink()
        assert wait_for(lambda: event_count(log, "stop") == 1)
        os.close(first_fd)

        second_fd = open_pending(pending, create=True)
        try:
            assert wait_for(lambda: event_count(log, "start") == 2)
        finally:
            os.close(second_fd)
        assert wait_for(lambda: event_count(log, "stop") == 2)


def test_coalesced_closes_do_not_leave_stale_popup() -> None:
    with running_helper() as (proc, pending, log, _):
        first_fd = open_pending(pending)
        assert wait_for(lambda: event_count(log, "start") == 1)
        second_fd = open_pending(pending)
        time.sleep(0.1)

        proc.send_signal(signal.SIGSTOP)
        time.sleep(0.05)
        try:
            os.close(first_fd)
            os.close(second_fd)
        finally:
            proc.send_signal(signal.SIGCONT)

        assert wait_for(lambda: event_count(log, "stop") == 1)


def test_custom_copy() -> None:
    extra_env = {
        "PAM_U2F_TOUCH_TITLE": "Authentication request",
        "PAM_U2F_TOUCH_MESSAGE": "Touch the configured authenticator.",
    }
    with running_helper(extra_env=extra_env) as (_, pending, log, _):
        fd = open_pending(pending)
        try:
            assert wait_for(lambda: event_count(log, "start") == 1)
            record = next(record for record in read_records(log) if record["event"] == "start")
            argv = record["argv"]
            assert argv[argv.index("--title") + 1] == extra_env["PAM_U2F_TOUCH_TITLE"]
            assert argv[argv.index("--text") + 1] == extra_env["PAM_U2F_TOUCH_MESSAGE"]
        finally:
            os.close(fd)
        assert wait_for(lambda: event_count(log, "stop") == 1)


def test_shutdown_closes_popup() -> None:
    with running_helper() as (proc, pending, log, _):
        fd = open_pending(pending)
        try:
            assert wait_for(lambda: event_count(log, "start") == 1)
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=3)
            assert proc.returncode == 0
            assert wait_for(lambda: event_count(log, "stop") == 1)
        finally:
            os.close(fd)


def test_idle_shutdown() -> None:
    with running_helper() as (proc, _, _, _):
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=3)
        assert proc.returncode == 0


def test_cleanup_does_not_kill_unrelated_process() -> None:
    with tempfile.TemporaryDirectory() as tmp_string:
        log = Path(tmp_string) / "zenity.log"
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            log.write_text(json.dumps({"event": "start", "pid": proc.pid}) + "\n")
            cleanup_popups(log)
            time.sleep(0.05)
            assert proc.poll() is None
        finally:
            terminate_process(proc)


def test_cleanup_kills_matching_popup() -> None:
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return

    with tempfile.TemporaryDirectory() as tmp_string:
        tmp = Path(tmp_string)
        log = tmp / "zenity.log"
        zenity = tmp / "zenity"
        write_fake_zenity(zenity)
        env = os.environ.copy()
        env["ZENITY_LOG"] = str(log)
        proc = subprocess.Popen(
            [str(zenity), "--info"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert wait_for(lambda: event_count(log, "start") == 1)
            cleanup_popups(log)
            proc.wait(timeout=3)
            assert proc.returncode == -signal.SIGKILL
        finally:
            terminate_process(proc)


def test_missing_parent_fails_cleanly() -> None:
    with tempfile.TemporaryDirectory() as tmp_string:
        tmp = Path(tmp_string)
        env = os.environ.copy()
        env["PAM_U2F_AUTHPENDING_FILE"] = str(tmp / "missing" / "authpending")
        proc = subprocess.run([str(BINARY)], env=env, capture_output=True, timeout=3, check=False)
        assert proc.returncode == 1
        assert b"inotify_add_watch parent" in proc.stderr


def main() -> int:
    tests = [
        test_preexisting_open_close_and_default_copy,
        test_first_create_is_observed,
        test_brief_open_is_debounced,
        test_long_open_survives_debounce,
        test_unrelated_file_is_ignored,
        test_no_display_does_not_spawn_popup,
        test_delete_and_recreate_resets_state,
        test_coalesced_closes_do_not_leave_stale_popup,
        test_custom_copy,
        test_shutdown_closes_popup,
        test_idle_shutdown,
        test_cleanup_does_not_kill_unrelated_process,
        test_cleanup_kills_matching_popup,
        test_missing_parent_fails_cleanly,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
