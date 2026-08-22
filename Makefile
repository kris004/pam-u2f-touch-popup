# SPDX-License-Identifier: GPL-3.0-or-later

CC ?= cc
CPPFLAGS ?=
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -pedantic
LDFLAGS ?=
PYTHON ?= python3
PREFIX ?= $(HOME)/.local
BINDIR ?= $(PREFIX)/bin
UNITDIR ?= $(PREFIX)/share/systemd/user
VERSION ?=
RELEASE_TARGET ?= linux-$(shell uname -m)

.PHONY: all check clean dist install test uninstall

all: pam-u2f-touch-popup

pam-u2f-touch-popup: pam-u2f-touch-popup.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -o $@ $< $(LDFLAGS)

check test: pam-u2f-touch-popup
	$(PYTHON) tests/integration.py ./pam-u2f-touch-popup

dist: pam-u2f-touch-popup
	@test -n "$(VERSION)" || { echo "VERSION is required (for example, VERSION=v0.1.0)" >&2; exit 2; }
	./scripts/package-release.sh "$(VERSION)" "$(RELEASE_TARGET)"

install: pam-u2f-touch-popup
	install -Dm755 pam-u2f-touch-popup $(DESTDIR)$(BINDIR)/pam-u2f-touch-popup
	install -Dm644 pam-u2f-touch-popup.service.in $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup.service
	sed -i 's|@BINDIR@|$(BINDIR)|g' $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup.service

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/pam-u2f-touch-popup
	rm -f $(DESTDIR)$(UNITDIR)/pam-u2f-touch-popup.service

clean:
	rm -f pam-u2f-touch-popup
