#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Integration tests for pam-u2f-touch-popup-setup."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tarfile
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
        self.setup = root / "setup-fixture"
        self.bin.mkdir(parents=True)
        self.sys_class_hidraw.mkdir(parents=True)
        self.properties.mkdir()
        self._write_fake_commands()
        self._write_test_artifact()

        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "PAM_U2F_TOUCH_POPUP_SETUP_TESTING": "0",
                "PAM_TEST_PROPERTIES_DIR": str(self.properties),
                "PAM_TEST_COMMAND_LOG": str(self.log),
            }
        )

    def _write_test_artifact(self) -> None:
        # Only this disposable artifact gets injection. There is no production
        # switch, and missing stubs fail instead of falling back to real sudo.
        source = SETUP.read_text()
        replacements = {
            "readonly sys_class_hidraw='/sys/class/hidraw'": (
                f"readonly sys_class_hidraw={shlex.quote(str(self.sys_class_hidraw))}", 1
            ),
            "readonly rule_path='/etc/udev/rules.d/70-pam-u2f-touch-popup-device-gate.rules'": (
                f"readonly rule_path={shlex.quote(str(self.rule))}", 2
            ),
            "readonly rule_dir='/etc/udev/rules.d'": (
                f"readonly rule_dir={shlex.quote(str(self.rule.parent))}", 1
            ),
            "readonly marker_path='/dev/pam-u2f-touch-popup-key'": (
                f"readonly marker_path={shlex.quote(str(self.marker))}", 1
            ),
            'if (( EUID == 0 )); then': ('if false; then', 1),
            "(( EUID == 0 )) || die 'the privileged setup operation requires root'": (':', 1),
        }
        for original, (replacement, count) in replacements.items():
            assert source.count(original) == count, original
            source = source.replace(original, replacement)
        for directory in ("/usr/bin", "/usr/sbin", "/bin", "/sbin"):
            original = f'"{directory}/${{name}}"'
            assert source.count(original) == 2, original
            source = source.replace(original, f'"{self.bin}/${{name}}"')
        self.setup.write_text(source)
        self.setup.chmod(0o755)

    def _write_executable(self, name: str, content: str) -> None:
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def _write_fake_commands(self) -> None:
        self._write_executable(
            "udevadm",
            """#!/bin/bash -p
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
    /bin/cat "${PAM_TEST_PROPERTIES_DIR}/${device}"
    ;;
  verify)
    [[ ${2-} == --help ]] && exit 0
    [[ ${PAM_TEST_FAIL_VERIFY:-0} != 1 ]] || exit 1
    # The root-side staging directory must remain private even after chmod.
    [[ $(/usr/bin/stat -c %a -- "${2%/*}") == 700 ]]
    ;;
  control|trigger|settle) ;;
  *) exit 2 ;;
esac
""",
        )
        self._write_executable(
            "systemctl",
            """#!/bin/bash -p
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
            """#!/bin/bash -p
set -euo pipefail
[[ $# -ge 6 && $1 == -- && $2 == /bin/bash && $3 == -p && $4 == -c ]]
printf 'sudo -- /bin/bash -p -c <helper> %s %s %s\\n' "${6-}" "${7-}" "${8-}" >>"${PAM_TEST_COMMAND_LOG}"
if [[ -n ${PAM_TEST_OLD_RULE:-} ]]; then
  printf '%s\\n' 'RUN+="/attacker/payload"' >"${PAM_TEST_OLD_RULE}"
fi
shift
exec "$@"
""",
        )
        for name in ("mkdir", "mktemp", "chmod", "mv", "sync", "rm"):
            real_command = shutil.which(name, path="/usr/bin:/bin")
            assert real_command is not None, name
            self._write_executable(
                name,
                "#!/bin/bash -p\nset -euo pipefail\n"
                f"printf '{name} %s\\n' \"$*\" >>\"${{PAM_TEST_COMMAND_LOG}}\"\n"
                f"exec {shlex.quote(real_command)} \"$@\"\n",
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
            [str(self.setup), *arguments],
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

    def privileged_run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        code = self.setup.read_text().split("<<'PRIVILEGED_SETUP' || :\n", 1)[1]
        code = code.split("\nPRIVILEGED_SETUP", 1)[0]
        return subprocess.run(
            ["/bin/bash", "-p", "-c", code, "setup-fixture", *arguments],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )


def new_harness() -> tuple[tempfile.TemporaryDirectory[str], Harness]:
    temporary_directory = tempfile.TemporaryDirectory(prefix="setup-test-", dir="/tmp")
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
        for directory in (harness.rule.parent, harness.rule.parent.parent, harness.rule.parent.parent.parent):
            assert directory.stat().st_mode & 0o777 == 0o755
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
        assert any("<helper> pam-u2f-touch-popup-setup enable 1050:0402" in command for command in commands)
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
            if command.startswith("sudo -- ") and " disable " in command
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


def assert_production_rejects_testing(setup: Path, root: Path) -> None:
    log = root / "poisoned-path.log"
    malicious_bin = root / "poisoned-bin"
    malicious_bin.mkdir()
    for name in ("sudo", "install", "rm", "udevadm", "mktemp", "cat", "systemctl"):
        command = malicious_bin / name
        command.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' {shlex.quote(name)} >>{shlex.quote(str(log))}\n"
            "exit 99\n"
        )
        command.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=f"{malicious_bin}:/usr/bin:/bin",
        PAM_U2F_TOUCH_POPUP_SETUP_TESTING="1",
        PAM_U2F_TOUCH_POPUP_SYS_CLASS_HIDRAW=str(root / "hidraw"),
        PAM_U2F_TOUCH_POPUP_RULE_PATH=str(root / "attacker.rules"),
        PAM_U2F_TOUCH_POPUP_MARKER_PATH=str(root / "marker"),
    )
    for args in (("enable", "--yes", "1050:0402"), ("disable", "--yes")):
        result = subprocess.run(
            [str(setup), *args], env=env, text=True, capture_output=True,
            timeout=5, check=False,
        )
        assert result.returncode == 2, result.stderr
        assert "runtime setup testing is not supported" in result.stderr
        assert not log.exists(), "production reached a PATH command"
        assert not (root / "attacker.rules").exists()


def test_production_and_distributed_artifacts_reject_testing() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp_string:
        tmp = Path(tmp_string)
        source_probe = tmp / "source-probe"
        source_probe.mkdir()
        assert_production_rejects_testing(SETUP, source_probe)
        stage = tmp / "stage"
        result = subprocess.run(
            ["make", "--silent", "install", "PREFIX=/usr", f"DESTDIR={stage}"],
            cwd=PROJECT_ROOT, text=True, capture_output=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        staged_probe = tmp / "staged-probe"
        staged_probe.mkdir()
        assert_production_rejects_testing(stage / "usr/bin/pam-u2f-touch-popup-setup", staged_probe)
        output = tmp / "dist"
        env = os.environ.copy()
        env.update(OUTPUT_DIR=str(output), RELEASE_REF="HEAD")
        result = subprocess.run(
            ["/bin/bash", str(PROJECT_ROOT / "scripts/package-release.sh"), "v0.0.0", "fixture"],
            cwd=PROJECT_ROOT, env=env, text=True, capture_output=True, timeout=10, check=False,
        )
        assert result.returncode == 0, result.stderr
        with tarfile.open(output / "pam-u2f-touch-popup-0.0.0-fixture.tar.gz") as archive:
            member = archive.getmember("pam-u2f-touch-popup-0.0.0-fixture/bin/pam-u2f-touch-popup-setup")
            stream = archive.extractfile(member)
            assert stream is not None
            packaged_setup = tmp / "packaged-setup"
            packaged_setup.write_bytes(stream.read())
            packaged_setup.chmod(0o755)
        packaged_probe = tmp / "packaged-probe"
        packaged_probe.mkdir()
        assert_production_rejects_testing(packaged_setup, packaged_probe)


def test_production_ignores_bash_startup_injection() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp_string:
        tmp = Path(tmp_string)
        marker = tmp / "startup-ran"
        startup = tmp / "startup"
        startup.write_text(f"printf injected >{shlex.quote(str(marker))}\n")
        env = os.environ.copy()
        env.update(BASH_ENV=str(startup), ENV=str(startup))
        result = subprocess.run(
            [str(SETUP), "--help"], env=env, text=True, capture_output=True,
            timeout=5, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not marker.exists()


def test_production_resolvers_keep_fixed_executable_paths() -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp_string:
        tmp = Path(tmp_string)
        poisoned_bin = tmp / "bin"
        poisoned_bin.mkdir()
        marker = tmp / "poisoned-command-ran"
        for name in ("bash", "install", "rm", "udevadm", "sudo"):
            fake = poisoned_bin / name
            fake.write_text(f"#!/bin/sh\nprintf ran >{shlex.quote(str(marker))}\nexit 99\n")
            fake.chmod(0o755)
        source = SETUP.read_text()
        assert source.endswith('main "$@"\n')
        outer = source.removesuffix('main "$@"\n')
        inner = source.split("<<'PRIVILEGED_SETUP' || :\n", 1)[1]
        inner = inner.split("(( EUID == 0 )) || die", 1)[0]
        env = os.environ.copy()
        env.update(PATH=str(poisoned_bin), PAM_U2F_TOUCH_POPUP_SETUP_TESTING="0")
        probe = '\nfor name in bash install rm udevadm sudo; do find_command "$name" || :; done\n'
        for definitions in (outer, inner):
            result = subprocess.run(
                ["/bin/bash", "-p", "-c", definitions + probe],
                env=env, text=True, capture_output=True, timeout=5, check=False,
            )
            assert result.returncode == 0, result.stderr
            paths = result.stdout.splitlines()
            assert paths
            for path in paths:
                assert Path(path).parent in tuple(Path(p) for p in ("/usr/bin", "/usr/sbin", "/bin", "/sbin")), path
            assert not marker.exists()


def test_helper_rejects_untrusted_arguments_before_commands() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        for arguments in (
            ("enable", "1050:0402\nRUN+=payload"),
            ("enable", "ABCD:1234"),
            ("enable", "1050:0402", "/tmp/injected.rules"),
            ("enable",), ("disable", "extra"), ("execute", "/bin/true"),
        ):
            result = harness.privileged_run(*arguments)
            assert result.returncode == 1, (arguments, result.stderr)
            assert harness.commands() == []
            assert not harness.rule.exists()


def test_enable_reconstructs_rule_after_unprivileged_replacement() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        old_rule = harness.root / "unprivileged.rules"
        old_rule.write_text("originally validated content\n")
        # Mimic a same-UID replacement at the old sudo/installation window.
        # The new helper has no argument or open referring to this pathname.
        harness._write_executable("cat", "#!/bin/sh\nexit 99\n")
        result = harness.run("enable", "--yes", "ABCD:1234", env={"PAM_TEST_OLD_RULE": str(old_rule)})
        assert result.returncode == 0, result.stderr
        assert old_rule.read_text() == 'RUN+="/attacker/payload"\n'
        assert harness.rule.read_text() == (
            "# SPDX-License-Identifier: GPL-3.0-or-later\n"
            "# Managed by pam-u2f-touch-popup-setup. Manual edits may be replaced.\n"
            "# Match every FIDO authenticator with USB ID abcd:1234.\n\n"
            'SUBSYSTEM=="hidraw", KERNEL=="hidraw*", ENV{ID_FIDO_TOKEN}=="1", \\\n'
            '  ATTRS{idVendor}=="abcd", ATTRS{idProduct}=="1234", TAG+="systemd", \\\n'
            '  SYMLINK+="pam-u2f-touch-popup-key", \\\n'
            '  ENV{SYSTEMD_ALIAS}+="/dev/pam-u2f-touch-popup-key"\n'
        )
        assert harness.rule.stat().st_mode & 0o777 == 0o644
        assert not list(harness.rule.parent.glob(".pam-u2f-touch-popup.*"))
        commands = harness.commands()
        verify_index = next(i for i, c in enumerate(commands) if c.startswith("udevadm verify /"))
        rename_index = next(i for i, c in enumerate(commands) if c.startswith("mv -fT -- "))
        reload_index = commands.index("udevadm control --reload")
        assert verify_index < rename_index < reload_index
        assert str(old_rule) not in "\n".join(commands)


def test_validation_failure_preserves_existing_rule() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        harness.rule.parent.mkdir(parents=True)
        previous = 'SYMLINK+="pam-u2f-touch-popup-key"\n'
        harness.rule.write_text(previous)
        result = harness.run("enable", "--yes", "1050:0402", env={"PAM_TEST_FAIL_VERIFY": "1"})
        assert result.returncode == 1
        assert "generated udev rule failed validation" in result.stderr
        assert harness.rule.read_text() == previous
        assert not list(harness.rule.parent.glob(".pam-u2f-touch-popup.*"))
        assert not any(c.startswith("mv ") or c == "udevadm control --reload" for c in harness.commands())


def test_helper_rechecks_rule_after_sudo_boundary() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        harness.rule.parent.mkdir(parents=True)
        harness.rule.write_text('SYMLINK+="pam-u2f-touch-popup-key"\n')
        for operation in (("enable", "--yes", "1050:0402"), ("disable", "--yes")):
            harness.rule.write_text('SYMLINK+="pam-u2f-touch-popup-key"\n')
            result = harness.run(*operation, env={"PAM_TEST_OLD_RULE": str(harness.rule)})
            assert result.returncode == 1
            assert "is not a pam-u2f-touch-popup device-gate rule" in result.stderr
            assert harness.rule.read_text() == 'RUN+="/attacker/payload"\n'


def test_missing_sudo_fixture_never_falls_back_to_real_sudo() -> None:
    temporary_directory, harness = new_harness()
    with temporary_directory:
        (harness.bin / "sudo").unlink()
        for arguments in (("enable", "--yes", "1050:0402"), ("disable", "--yes")):
            if arguments[0] == "disable":
                harness.rule.parent.mkdir(parents=True)
                harness.rule.write_text('SYMLINK+="pam-u2f-touch-popup-key"\n')
            result = harness.run(*arguments)
            assert result.returncode == 1
            assert "required command not found in a system directory: sudo" in result.stderr
            assert not any(c.startswith("sudo ") for c in harness.commands())


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
        test_production_and_distributed_artifacts_reject_testing,
        test_production_ignores_bash_startup_injection,
        test_production_resolvers_keep_fixed_executable_paths,
        test_helper_rejects_untrusted_arguments_before_commands,
        test_enable_reconstructs_rule_after_unprivileged_replacement,
        test_validation_failure_preserves_existing_rule,
        test_helper_rechecks_rule_after_sudo_boundary,
        test_missing_sudo_fixture_never_falls_back_to_real_sudo,
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
