CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra -pedantic
LDFLAGS ?=
PREFIX ?= $(HOME)/.local
BINDIR ?= $(PREFIX)/bin

.PHONY: all clean install

all: pam-u2f-touch-popup

pam-u2f-touch-popup: pam-u2f-touch-popup.c
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $<

install: pam-u2f-touch-popup
	install -Dm755 pam-u2f-touch-popup $(DESTDIR)$(BINDIR)/pam-u2f-touch-popup

clean:
	rm -f pam-u2f-touch-popup
