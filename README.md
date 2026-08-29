# pam-u2f-touch-popup

`pam-u2f-touch-popup` is a small Linux user-session helper that shows a Zenity
dialog while [`pam_u2f`](https://github.com/Yubico/pam-u2f) is waiting for a
touch on a YubiKey or another compatible U2F/FIDO2 authenticator.

The helper does not participate in authentication and does not modify PAM. If
it is stopped or misconfigured, authentication continues without the graphical
notification.

## How it works

`pam_u2f` opens its `authpending_file` while it waits for user presence and
closes the file when the wait ends. The file is normally left in place, so its
mere existence does not indicate an active request.

This helper watches the file's parent directory for Linux inotify open and
close events. Watching the directory also captures the first open that creates
the file, avoiding a race that occurs when a watcher waits for file creation
before attaching a file-specific watch. Brief opens are debounced so failed
no-device probes do not flash a dialog.

## Requirements

- Linux with inotify support and procfs mounted at `/proc`
- a C11 compiler and `make` to build
- Python 3 to run the integration tests
- `pam_u2f` configured for the graphical-session user
- Zenity installed at `/usr/bin/zenity`, or another absolute path selected at
  build time
- Bash for `pam-u2f-touch-popup-setup`
- systemd, udev, and sudo when using the optional device-presence gate

No library beyond libc is linked into the helper.

## Build and test

```sh
make
make test
```

The integration suite uses a temporary fake Zenity executable; it neither
opens graphical dialogs nor invokes PAM.

## Release downloads

Each GitHub release provides:

- `pam-u2f-touch-popup-VERSION-linux-x86_64-musl.tar.gz`, containing a static
  position-independent x86-64 Linux binary, the setup command, the default and
  optional device-gated user units, and an example udev rule laid out for
  `$HOME/.local`;
- `pam-u2f-touch-popup-VERSION-src.tar.gz`, containing the exact tagged source;
  and
- `SHA256SUMS`, covering both archives.

After downloading all three assets, verify them before installation:

```sh
sha256sum --check SHA256SUMS
```

Releases produced by the current workflow also include signed build-provenance
attestations. With GitHub CLI installed, verify each downloaded archive against
this repository:

```sh
gh attestation verify \
  pam-u2f-touch-popup-VERSION-linux-x86_64-musl.tar.gz \
  --repo kris004/pam-u2f-touch-popup
gh attestation verify \
  pam-u2f-touch-popup-VERSION-src.tar.gz \
  --repo kris004/pam-u2f-touch-popup
```

The binary archive can be installed without root access by extracting it into
the user-local prefix and starting the included service:

```sh
mkdir -p "$HOME/.local"
tar -xzf pam-u2f-touch-popup-VERSION-linux-x86_64-musl.tar.gz \
  --strip-components=1 -C "$HOME/.local"
systemctl --user daemon-reload
systemctl --user enable --now pam-u2f-touch-popup.service
```

For the optional device gate, extract the archive in the same way but run the
included setup command instead of enabling the default service:

```sh
"$HOME/.local/bin/pam-u2f-touch-popup-setup" list
"$HOME/.local/bin/pam-u2f-touch-popup-setup" enable
```

Users of other architectures can build from the source archive with the
commands below.

## Install and start

The default installation is user-local and installs both the executable and a
systemd user unit:

```sh
make install
systemctl --user daemon-reload
systemctl --user enable --now pam-u2f-touch-popup.service
```

For a source installation with device gating, list the connected FIDO models
and pass the selected USB ID during installation:

```sh
make list-devices
make install DEVICE=1050:0402
```

`DEVICE` is optional. When it is absent, `make install` never invokes sudo or
changes live user services. When present, it installs the udev rule and enables
the gated path after installing the user-local files. It cannot be combined
with `DESTDIR`, so staged distribution builds remain side-effect free.

The defaults are:

- executable: `$HOME/.local/bin/pam-u2f-touch-popup`
- setup command: `$HOME/.local/bin/pam-u2f-touch-popup-setup`
- user units: `$HOME/.local/share/systemd/user/pam-u2f-touch-popup*.service`
  and `$HOME/.local/share/systemd/user/pam-u2f-touch-popup-device.path`
- optional udev example:
  `$HOME/.local/share/doc/pam-u2f-touch-popup/examples/70-pam-u2f-touch-popup-device-gate.rules`

Packagers can override the executable, user-unit, and documentation directories
while staging a system installation. For example, the following uses
conventional distribution paths without embedding the staging root in the
service:

```sh
make \
  DESTDIR=/tmp/package-root \
  PREFIX=/usr \
  BINDIR=/usr/libexec \
  SETUPDIR=/usr/bin \
  UNITDIR=/usr/lib/systemd/user \
  DOCDIR=/usr/share/doc/pam-u2f-touch-popup \
  install
```

`BINDIR` is embedded in each service's `ExecStart=` setting. `SETUPDIR`,
`UNITDIR`, and `DOCDIR` only control where files are installed. Distribution
packages should use their package-manager helpers to select the native command,
libexec, systemd user-unit, and documentation directories.

The service is tied to `graphical-session.target`. Desktop environments usually
import `DISPLAY` or `WAYLAND_DISPLAY` into the systemd user manager. Minimal
window-manager sessions may need to do so during session startup before the
service starts:

```sh
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY
systemctl --user restart pam-u2f-touch-popup.service
```

Without systemd, start `pam-u2f-touch-popup` from the graphical session's
autostart mechanism.

### Optional device-presence gate

Nothing in this section is required on a system where every connected FIDO
authenticator should use the popup. The default
`pam-u2f-touch-popup.service` remains the normal setup.

On a system with multiple authenticator models, the optional gate keeps the
helper stopped unless a selected model is present. An example udev rule exposes
the selected FIDO device as `/dev/pam-u2f-touch-popup-key`; a path unit starts
the alternate service when that path appears, and the service's device binding
stops it when the device is unplugged. Device selection therefore does not
depend on the popup delay.

Run the setup command as the desktop user, not with sudo. It discovers
connected FIDO models, asks for confirmation, invokes sudo only to install the
udev rule, reloads existing hidraw devices, and switches the user services. If
only one model is connected, `enable` selects it automatically; otherwise it
presents a numbered choice:

```sh
pam-u2f-touch-popup-setup list
pam-u2f-touch-popup-setup enable
```

An explicit USB ID also works when the key is not currently connected:

```sh
pam-u2f-touch-popup-setup enable 1050:0402
```

Source users can perform the install and explicit selection together:

```sh
make install DEVICE=1050:0402
```

Inspect the resulting state or return to the default service with:

```sh
pam-u2f-touch-popup-setup status
pam-u2f-touch-popup-setup disable
```

From a source checkout, `make device-gate-status` and
`make disable-device-gate` provide the same operations.

The setup command validates the generated rule before installation when the
local udev version supports `udevadm verify`. If enabling the path unit fails,
it restores the default service and leaves the rule in place for inspection.

Selection is model-wide: every FIDO key with the chosen USB vendor and product
ID will match. Selecting one physical unit additionally requires a stable
serial attribute and a corresponding `ATTRS{serial}==` match; not all
authenticators expose a USB serial number.

For manual configuration, the installed example selects the Yubico USB ID
`1050:0402`. Copy it into the system udev configuration, edit `idVendor` and
`idProduct` if necessary, reload udev, reconnect the selected key, and switch
the user services:

```sh
sudo install -Dm644 \
  "$HOME/.local/share/doc/pam-u2f-touch-popup/examples/70-pam-u2f-touch-popup-device-gate.rules" \
  /etc/udev/rules.d/70-pam-u2f-touch-popup-device-gate.rules
sudoedit /etc/udev/rules.d/70-pam-u2f-touch-popup-device-gate.rules
sudo udevadm control --reload
systemctl --user disable --now pam-u2f-touch-popup.service
systemctl --user enable --now pam-u2f-touch-popup-device.path
```

## Configuration

Configuration is read from the helper's environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAM_U2F_AUTHPENDING_FILE` | `/var/run/user/$UID/pam-u2f-authpending` | File whose opens and closes are observed |
| `PAM_U2F_TOUCH_DELAY_MS` | `250` | Popup delay from 0 through 5000 milliseconds; invalid values use 250 |
| `PAM_U2F_TOUCH_TITLE` | `Security key touch required` | Dialog title |
| `PAM_U2F_TOUCH_MESSAGE` | `Touch your security key to approve authentication.` | Dialog text |

The default authpending path exactly matches the upstream `pam_u2f` default.
If a PAM service sets `authpending_file` explicitly, configure the helper with
the same path. The file's parent must be a real directory owned by the helper's
effective user ID, with no group or other permission bits. The default
`/run/user/$UID` directory normally meets these requirements. For the systemd
unit, use a drop-in. When the optional gate is enabled, edit
`pam-u2f-touch-popup-device.service` instead:

```sh
systemctl --user edit pam-u2f-touch-popup.service
```

For example:

```ini
[Service]
Environment="PAM_U2F_AUTHPENDING_FILE=/run/user/1000/custom-authpending"
Environment="PAM_U2F_TOUCH_MESSAGE=Touch your security key to continue."
```

Use a portable path appropriate for the target system; the numeric path above
is only an example.

The popup executable is fixed at build time rather than selected from the
service environment. Packagers whose Zenity-compatible executable is not at
`/usr/bin/zenity` can set an absolute path while building:

```sh
make CPPFLAGS='-DPAM_U2F_ZENITY_PATH=\"/opt/bin/zenity\"'
```

## Security model

The popup is an advisory usability signal, not part of authentication and not
proof that `pam_u2f` initiated a request. Inotify reports that the configured
file was opened or closed, but does not authenticate the process responsible.

Configuration environment variables are trusted same-user input. The popup
executable is not configurable at runtime and is executed directly with fixed
arguments, without a shell or `PATH` lookup. Do not install the helper setuid or
setgid, grant it file capabilities, or launch it with elevated privileges while
accepting an environment from a less-privileged user.

Requiring an owner-only parent directory prevents other ordinary local users
from reaching the watched file. A process running as the same user, or as root,
can still open the file to spoof a popup, close it to suppress a popup, or
replace the graphical helper. Authenticated source identity would require a
cooperating producer and an authenticated IPC protocol rather than passive
filesystem observation.

## Limitations

- The helper must already be running when `pam_u2f` opens the file. Inotify
  cannot report an open that predates the watcher.
- After connecting a key selected by the optional device gate, the device
  service must finish starting before an authentication request opens the
  authpending file. This normally happens before a person can begin the
  request, but software that starts authentication immediately on hotplug can
  still race the watcher.
- One authpending file represents one active request at a time. Linux may
  coalesce identical inotify events, so overlapping PAM conversations using the
  same file cannot be counted reliably. With overlapping requests, the dialog
  may close when the first request ends. If exact overlap handling is required,
  configure distinct authpending files and run one helper instance per file.
- The helper is Linux-specific and currently supports Zenity as its dialog
  frontend.

Queue overflows, file deletion or replacement, and watched-directory removal
reset the popup state rather than leaving a stale dialog behind.

## Uninstall

If the optional device gate is enabled, disable it first so the default service
is restored and the system udev rule is removed. `make uninstall` only removes
files installed below its configured prefix.

```sh
pam-u2f-touch-popup-setup disable
systemctl --user disable --now pam-u2f-touch-popup.service
make uninstall
systemctl --user daemon-reload
```

## License

`pam-u2f-touch-popup` is licensed under the GNU General Public License, version
3 or (at your option) any later version (`GPL-3.0-or-later`). See
[`LICENSE`](LICENSE).
