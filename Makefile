# SPDX-License-Identifier: GPL-3.0-or-later

CC ?= cc
CPPFLAGS ?=
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -pedantic
LDFLAGS ?=
PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BINDIR ?= $(PREFIX)/bin
SETUPDIR ?= $(PREFIX)/bin
UNITDIR ?= $(PREFIX)/share/systemd/user
DOCDIR ?= $(PREFIX)/share/doc/pam-u2f-touch-popup
DEVICE_GATE_SETUP ?= ./pam-u2f-touch-popup-setup
VERSION ?=
RELEASE_TARGET ?= linux-$(shell uname -m)

# Only an explicit command-line DEVICE opts into privileged live configuration.
ifneq ($(origin DEVICE),command line)
DEVICE :=
endif

export DESTDIR DEVICE

.PHONY: all check clean device-gate-status disable-device-gate dist install list-devices test uninstall

all: pam-u2f-touch-popup

pam-u2f-touch-popup: pam-u2f-touch-popup.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LDFLAGS)

check test: pam-u2f-touch-popup
	@set -e; \
	tmp=$$(mktemp -d); \
	trap 'rm -rf "$$tmp"' 0 HUP INT TERM; \
	$(CC) $(CPPFLAGS) -DPAM_U2F_TESTING=1 $(CFLAGS) \
		-o "$$tmp/pam-u2f-touch-popup-test" pam-u2f-touch-popup.c $(LDFLAGS); \
	$(PYTHON) tests/integration.py "$$tmp/pam-u2f-touch-popup-test" ./pam-u2f-touch-popup; \
	$(CC) $(CPPFLAGS) $(CFLAGS) -o "$$tmp/inotify-decoder" tests/inotify-decoder.c $(LDFLAGS); \
	"$$tmp/inotify-decoder"; \
	bash -n pam-u2f-touch-popup-setup; \
	$(PYTHON) tests/setup.py ./pam-u2f-touch-popup-setup

dist: pam-u2f-touch-popup
	@test -n "$(VERSION)" || { echo "VERSION is required (for example, VERSION=v0.1.0)" >&2; exit 2; }
	./scripts/package-release.sh "$(VERSION)" "$(RELEASE_TARGET)"

install: pam-u2f-touch-popup
	@if [ -n "$${DEVICE}" ]; then \
		case "$${DEVICE}" in \
			[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]) ;; \
			*) echo 'DEVICE must have the form VENDOR_ID:PRODUCT_ID (for example, 1050:0402)' >&2; exit 2 ;; \
		esac; \
		test -z "$${DESTDIR}" || { echo 'DEVICE cannot be used with DESTDIR staging' >&2; exit 2; }; \
	fi
	install -Dm755 pam-u2f-touch-popup $(DESTDIR)$(BINDIR)/pam-u2f-touch-popup
	install -Dm755 pam-u2f-touch-popup-setup $(DESTDIR)$(SETUPDIR)/pam-u2f-touch-popup-setup
	install -Dm644 pam-u2f-touch-popup.service.in $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup.service
	sed -i 's|@BINDIR@|$(BINDIR)|g' $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup.service
	install -Dm644 pam-u2f-touch-popup-device.service.in $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup-device.service
	sed -i 's|@BINDIR@|$(BINDIR)|g' $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup-device.service
	install -Dm644 pam-u2f-touch-popup-device.path $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup-device.path
	install -Dm644 examples/70-pam-u2f-touch-popup-device-gate.rules $(DESTDIR)$(DOCDIR)/examples/70-pam-u2f-touch-popup-device-gate.rules
	@if [ -n "$${DEVICE}" ]; then \
		$(DEVICE_GATE_SETUP) enable --yes "$${DEVICE}"; \
	fi

list-devices:
	$(DEVICE_GATE_SETUP) list

device-gate-status:
	$(DEVICE_GATE_SETUP) status

disable-device-gate:
	$(DEVICE_GATE_SETUP) disable

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/pam-u2f-touch-popup
	rm -f $(DESTDIR)$(SETUPDIR)/pam-u2f-touch-popup-setup
	rm -f $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup.service
	rm -f $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup-device.service
	rm -f $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup-device.path
	rm -f $(DESTDIR)$(DOCDIR)/examples/70-pam-u2f-touch-popup-device-gate.rules

clean:
	rm -f pam-u2f-touch-popup
