#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration tests for pam-u2f-touch-popup-setup."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SETUP = Path(sys.argv[1] if len(sys.argv) > 1 else "./pam-u2f-touch-popup-setup").resolve()
PROJECT_ROOT = SETUP.parent


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.sys_class_hidraw = root / "sys" / "class" / "hidraw"
        self.properties = root / "properties"
        self.rule = root / "etc" / "udev" / "rules.d" / "gate.rules"
        self.marker = root / "dev" / "pam-u2f-touch-popup-key"
        self.log = root / "commands.log"
        self.bin.mkdir(parents=True)
        self.sys_class_hidraw.mkdir(parents=True)
        self.properties.mkdir()
        self._write_fake_commands()

        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "PAM_U2F_TOUCH_POPUP_SETUP_TESTING": "1",
                "PAM_U2F_TOUCH_POPUP_SYS_CLASS_HIDRAW": str(self.sys_class_hidraw),
                "PAM_U2F_TOUCH_POPUP_RULE_PATH": str(self.rule),
                "PAM_U2F_TOUCH_POPUP_MARKER_PATH": str(self.marker),
                "PAM_TEST_PROPERTIES_DIR": str(self.properties),
                "PAM_TEST_COMMAND_LOG": str(self.log),
            }
        )

    def _write_executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def _write_fake_commands(self) -> None:
        self._write_executable(
            "udevadm",
            """#!/usr/bin/env bash
set -euo pipefail
printf 'udevadm %s\\n' "$*" >>"${PAM_TEST_COMMAND_LOG}"
case ${1-} in
  info)
    device=
    for arg in "$@"; do
      case ${arg} in
        --path=*) device=${arg##*/} ;;
      esac
    done
    cat "${PAM_TEST_PROPERTIES_DIR}/${device}"
    ;;
  verify|control|trigger|settle) ;;
  *) exit 2 ;;
esac
""",
        )
        self._write_executable(
            "systemctl",
            """#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\\n' "$*" >>"${PAM_TEST_COMMAND_LOG}"
arguments=" $* "
if [[ ${arguments} == *' is-active --quiet pam-u2f-touch-popup-device.service '* ]]; then
  [[ ${PAM_TEST_DEVICE_ACTIVE:-1} == 1 ]]
  exit
fi
if [[ ${arguments} == *' enable --now pam-u2f-touch-popup-device.path '* &&
      ${PAM_TEST_FAIL_DEVICE_ENABLE:-0} == 1 ]]; then
  exit 1
fi
if [[ ${arguments} == *' try-restart pam-u2f-touch-popup-device.service '* &&
      ${PAM_TEST_FAIL_DEVICE_RESTART:-0} == 1 ]]; then
  exit 1
fi
case ${arguments} in
  *' cat '*) ;;
  *' is-enabled pam-u2f-touch-popup.service '*) printf '%s\\n' disabled ;;
  *' is-active pam-u2f-touch-popup.service '*) printf '%s\\n' inactive; exit 3 ;;
  *' is-enabled pam-u2f-touch-popup-device.path '*) printf '%s\\n' enabled ;;
  *' is-active pam-u2f-touch-popup-device.path '*) printf '%s\\n' active ;;
  *' is-enabled pam-u2f-touch-popup-device.service '*) printf '%s\\n' static; exit 1 ;;
  *' is-active pam-u2f-touch-popup-device.service '*) printf '%s\\n' inactive; exit 3 ;;
esac
""",
        )
        self._write_executable(
            "sudo",
            """#!/usr/bin/env bash
set -euo pipefail
printf 'sudo %s\\n' "$*" >>"${PAM_TEST_COMMAND_LOG}"
if [[ ${1-} == -v ]]; then
  exit 0
fi
if [[ ${1-} == -- ]]; then
  shift
fi
exec "$@"
""",
        )

    def add_device(
        self,
        name: str,
        vendor_id: str,
        product_id: str,
        vendor: str,
        model: str,
        *,
        fido: bool = True,
    ) -> None:
        (self.sys_class_hidraw / name).mkdir()
        (self.properties / name).write_text(
            f"ID_FIDO_TOKEN={int(fido)}\n"
            f"ID_VENDOR_ID={vendor_id}\n"
            f"ID_MODEL_ID={product_id}\n"
            f"ID_VENDOR={vendor}\n"
            f"ID_MODEL={model}\n"
        )

    def run(self, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        command_env = self.env.copy()
        if env:
            command_env.update(env)
        return subprocess.run(
            [str(SETUP), *arguments],
            env=command_env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

    def commands(self) -> list[str]:
        if not self.log.exists():
            return []
        return self.log.read_text().splitlines()


def new_harness() -> tuple[tempfile.TemporaryDirectory[str], Harness]:
    temporary_directory = tempfile.TemporaryDirectory()
    return temporary_directory, Harness(Path(temporary_directory.name))


def test_list_deduplicates_fido_models() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        harness.add_device("hidraw0", "1050", "0402", "Yubico", "YubiKey_FIDO")
        harness.add_device("hidraw1", "1050", "0402", "Yubico", "YubiKey_FIDO")
        harness.add_device("hidraw2", "1050", "0407", "Yubico", "YubiKey_OTP+FIDO+CCID")
        harness.add_device("hidraw3", "1234", "5678", "Other", "Keyboard", fido=False)

        result = harness.run("list")
        assert result.returncode == 0, result.stderr
        assert result.stdout.count("(1050:0402)") == 1
        assert result.stdout.count("(1050:0407)") == 1
        assert "YubiKey FIDO" in result.stdout
        assert "Keyboard" not in result.stdout


def test_enable_generates_rule_and_switches_units() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        result = harness.run("enable", "--yes", "1050:0402")
        assert result.returncode == 0, result.stderr
        rule = harness.rule.read_text()
        assert "Managed by pam-u2f-touch-popup-setup" in rule
        assert 'ATTRS{idVendor}=="1050"' in rule
        assert 'ATTRS{idProduct}=="0402"' in rule
        assert "Device gating enabled for 1050:0402." in result.stdout

        commands = harness.commands()
        disable_index = next(
            index
            for index, command in enumerate(commands)
            if "disable --now pam-u2f-touch-popup.service" in command
        )
        enable_index = next(
            index
            for index, command in enumerate(commands)
            if "enable --now pam-u2f-touch-popup-device.path" in command
        )
        restart_index = next(
            index
            for index, command in enumerate(commands)
            if "try-restart pam-u2f-touch-popup-device.service" in command
        )
        assert disable_index < enable_index < restart_index
        assert any(command == "sudo -v" for command in commands)
        assert any("udevadm control --reload" in command for command in commands)
        assert any("udevadm trigger --action=change --subsystem-match=hidraw" in command for command in commands)


def test_enable_autoselects_only_connected_model() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        harness.add_device("hidraw0", "1050", "0402", "Yubico", "YubiKey_FIDO")

        result = harness.run("enable", "--yes")
        assert result.returncode == 0, result.stderr
        assert "Using the only detected model" in result.stderr
        assert 'ATTRS{idProduct}=="0402"' in harness.rule.read_text()


def test_enable_rejects_invalid_device_id() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        result = harness.run("enable", "--yes", "1050:not-hex")
        assert result.returncode == 1
        assert "device ID must have the form" in result.stderr
        assert not harness.rule.exists()
        assert not any(command.startswith("sudo ") for command in harness.commands())


def test_enable_restores_default_service_on_failure() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        result = harness.run(
            "enable",
            "--yes",
            "1050:0402",
            env={"PAM_TEST_FAIL_DEVICE_ENABLE": "1"},
        )
        assert result.returncode == 1
        assert "restoring pam-u2f-touch-popup.service" in result.stderr
        assert "the udev rule remains installed" in result.stderr
        assert harness.rule.exists()
        assert any(
            "enable --now pam-u2f-touch-popup.service" in command
            for command in harness.commands()
        )


def test_enable_reports_device_restart_failure() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        result = harness.run(
            "enable",
            "--yes",
            "1050:0402",
            env={"PAM_TEST_FAIL_DEVICE_RESTART": "1"},
        )
        assert result.returncode == 1
        assert "device gating is enabled" in result.stderr
        assert "could not be restarted" in result.stderr
        assert harness.rule.exists()


def test_enable_does_not_start_inactive_device_service_directly() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        result = harness.run(
            "enable",
            "--yes",
            "1050:0402",
            env={"PAM_TEST_DEVICE_ACTIVE": "0"},
        )
        assert result.returncode == 0, result.stderr
        assert not any(
            "try-restart pam-u2f-touch-popup-device.service" in command
            for command in harness.commands()
        )


def test_enable_refuses_unrelated_rule() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        harness.rule.parent.mkdir(parents=True)
        harness.rule.write_text("unrelated\n")

        result = harness.run("enable", "--yes", "1050:0402")
        assert result.returncode == 1
        assert "is not a pam-u2f-touch-popup device-gate rule" in result.stderr
        assert harness.rule.read_text() == "unrelated\n"
        assert not any(command.startswith("sudo ") for command in harness.commands())


def test_disable_restores_default_and_removes_rule() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        harness.rule.parent.mkdir(parents=True)
        harness.rule.write_text('SYMLINK+="pam-u2f-touch-popup-key"\n')

        result = harness.run("disable", "--yes")
        assert result.returncode == 0, result.stderr
        assert not harness.rule.exists()
        assert "Device gating disabled" in result.stdout
        commands = harness.commands()
        disable_index = next(
            index
            for index, command in enumerate(commands)
            if "disable --now pam-u2f-touch-popup-device.path" in command
        )
        enable_index = next(
            index
            for index, command in enumerate(commands)
            if "enable --now pam-u2f-touch-popup.service" in command
        )
        remove_index = next(
            index
            for index, command in enumerate(commands)
            if command.startswith("sudo -- ") and " -f -- " in command
        )
        assert disable_index < enable_index < remove_index


def test_status_reports_marker_and_units() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        harness.rule.parent.mkdir(parents=True)
        harness.rule.write_text('SYMLINK+="pam-u2f-touch-popup-key"\n')
        harness.marker.parent.mkdir(parents=True)
        harness.marker.touch()

        result = harness.run("status")
        assert result.returncode == 0, result.stderr
        assert "Udev rule:       installed" in result.stdout
        assert "Selected key:    present" in result.stdout
        assert "Default service: disabled, inactive" in result.stdout
        assert "Device path:     enabled, active" in result.stdout
        assert "Device service:  static, inactive" in result.stdout


def test_production_ignores_path_override() -> None:
    with tempfile.TemporaryDirectory() as tmp_string:
        tmp = Path(tmp_string)
        marker = tmp / "fake-systemctl-ran"
        fake_systemctl = tmp / "systemctl"
        fake_systemctl.write_text(
            "#!/bin/sh\n"
            f"touch {marker}\n"
            "exit 1\n"
        )
        fake_systemctl.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{tmp}:/usr/bin:/bin"
        for name in (
            "PAM_U2F_TOUCH_POPUP_SETUP_TESTING",
            "PAM_U2F_TOUCH_POPUP_SYS_CLASS_HIDRAW",
            "PAM_U2F_TOUCH_POPUP_RULE_PATH",
            "PAM_U2F_TOUCH_POPUP_MARKER_PATH",
        ):
            env.pop(name, None)

        result = subprocess.run(
            [str(SETUP), "status"],
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if os.geteuid() == 0:
            assert result.returncode == 1
            assert "run this command as the desktop user" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
        assert not marker.exists()


def test_make_install_device_delegates_to_setup() -> None:
    with tempfile.TemporaryDirectory() as tmp_string:
        tmp = Path(tmp_string)
        prefix = tmp / "prefix"
        setup_log = tmp / "setup.log"
        fake_setup = tmp / "fake-setup"
        fake_setup.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$*" >"$DEVICE_GATE_LOG"\n'
        )
        fake_setup.chmod(0o755)
        env = os.environ.copy()
        env["DEVICE_GATE_LOG"] = str(setup_log)

        result = subprocess.run(
            [
                "make",
                "--silent",
                "install",
                f"PREFIX={prefix}",
                "DEVICE=1050:0402",
                f"DEVICE_GATE_SETUP={fake_setup}",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (prefix / "bin" / "pam-u2f-touch-popup").is_file()
        assert (prefix / "bin" / "pam-u2f-touch-popup-setup").is_file()
        assert setup_log.read_text() == "enable --yes 1050:0402\n"


def test_make_install_device_rejects_staging() -> None:
    with tempfile.TemporaryDirectory() as tmp_string:
        tmp = Path(tmp_string)
        result = subprocess.run(
            [
                "make",
                "--silent",
                "install",
                "PREFIX=/usr",
                f"DESTDIR={tmp / 'stage'}",
                "DEVICE=1050:0402",
            ],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 2
        assert "DEVICE cannot be used with DESTDIR staging" in result.stderr
        assert not (tmp / "stage" / "usr" / "bin" / "pam-u2f-touch-popup").exists()


def test_make_install_ignores_environment_device() -> None:
    with tempfile.TemporaryDirectory() as tmp_string:
        tmp = Path(tmp_string)
        env = os.environ.copy()
        env["DEVICE"] = "1050:0402"
        result = subprocess.run(
            [
                "make",
                "--silent",
                "install",
                "PREFIX=/usr",
                f"DESTDIR={tmp / 'stage'}",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert (tmp / "stage" / "usr" / "bin" / "pam-u2f-touch-popup").is_file()


def main() -> int:
    tests = [
        test_list_deduplicates_fido_models,
        test_enable_generates_rule_and_switches_units,
        test_enable_autoselects_only_connected_model,
        test_enable_rejects_invalid_device_id,
        test_enable_restores_default_service_on_failure,
        test_enable_reports_device_restart_failure,
        test_enable_does_not_start_inactive_device_service_directly,
        test_enable_refuses_unrelated_rule,
        test_disable_restores_default_and_removes_rule,
        test_status_reports_marker_and_units,
        test_production_ignores_path_override,
        test_make_install_device_delegates_to_setup,
        test_make_install_device_rejects_staging,
        test_make_install_ignores_environment_device,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
