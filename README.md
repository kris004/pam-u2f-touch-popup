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
- Zenity available on `PATH` at runtime, or an explicit `PAM_U2F_ZENITY`
  command or path

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
  x86-64 Linux binary and a user service laid out for `$HOME/.local`;
- `pam-u2f-touch-popup-VERSION-src.tar.gz`, containing the exact tagged source;
  and
- `SHA256SUMS`, covering both archives.

After downloading all three assets, verify them before installation:

```sh
sha256sum --check SHA256SUMS
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

The defaults are:

- executable: `$HOME/.local/bin/pam-u2f-touch-popup`
- user unit: `$HOME/.local/share/systemd/user/pam-u2f-touch-popup.service`

Packagers can stage a system installation without embedding the staging path
in the service:

```sh
make DESTDIR=/tmp/package-root PREFIX=/usr install
```

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

## Configuration

Configuration is read from the helper's environment:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAM_U2F_AUTHPENDING_FILE` | `/var/run/user/$UID/pam-u2f-authpending` | File whose opens and closes are observed |
| `PAM_U2F_ZENITY` | `zenity` | Zenity command name or executable path |
| `PAM_U2F_TOUCH_DELAY_MS` | `250` | Popup delay from 0 through 5000 milliseconds; invalid values use 250 |
| `PAM_U2F_TOUCH_TITLE` | `Security key touch required` | Dialog title |
| `PAM_U2F_TOUCH_MESSAGE` | `Touch your security key to approve authentication.` | Dialog text |

The default authpending path exactly matches the upstream `pam_u2f` default.
If a PAM service sets `authpending_file` explicitly, configure the helper with
the same path. The file's parent must be a real directory owned by the helper's
effective user ID, with no group or other permission bits. The default
`/run/user/$UID` directory normally meets these requirements. For the systemd
unit, use a drop-in:

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

## Security model

The popup is an advisory usability signal, not part of authentication and not
proof that `pam_u2f` initiated a request. Inotify reports that the configured
file was opened or closed, but does not authenticate the process responsible.

Requiring an owner-only parent directory prevents other ordinary local users
from reaching the watched file. A process running as the same user, or as root,
can still open the file to spoof a popup, close it to suppress a popup, or
replace the graphical helper. Authenticated source identity would require a
cooperating producer and an authenticated IPC protocol rather than passive
filesystem observation.

## Limitations

- The helper must already be running when `pam_u2f` opens the file. Inotify
  cannot report an open that predates the watcher.
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

```sh
systemctl --user disable --now pam-u2f-touch-popup.service
make uninstall
systemctl --user daemon-reload
```

## License

`pam-u2f-touch-popup` is licensed under the GNU General Public License, version
3 or (at your option) any later version (`GPL-3.0-or-later`). See
[`LICENSE`](LICENSE).
