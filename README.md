# pam-u2f-touch-popup

Tiny user-session helper for showing a Zenity prompt while `pam_u2f` is waiting
for a YubiKey touch.

`pam_u2f` opens its `authpending_file` while it waits and closes it when the
request ends. systemd `.path` units cannot trigger on that open/close-only
state, so this helper watches the file with Linux inotify and starts/stops a
Zenity info dialog.

No extra library is required; it uses libc, Linux inotify, and `/usr/bin/zenity`
at runtime. Tests can override the Zenity path with `PAM_U2F_ZENITY`.

Build:

```sh
make
```

The default watched path is `$XDG_RUNTIME_DIR/pam-u2f-authpending`, matching
`pam_u2f`'s default for the current user. Override with
`PAM_U2F_AUTHPENDING_FILE` for tests.
