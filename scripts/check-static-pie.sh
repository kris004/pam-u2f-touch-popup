#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail

readonly binary=${1:-}

if [[ -z ${binary} || ! -x ${binary} ]]; then
  echo "usage: $0 EXECUTABLE" >&2
  exit 2
fi

file_info=$(file "${binary}")
elf_header=$(readelf --file-header "${binary}")
program_headers=$(readelf --wide --program-headers "${binary}")
dynamic_tags=$(readelf --wide --dynamic "${binary}")
readonly file_info elf_header program_headers dynamic_tags

grep -F 'static-pie linked' <<<"${file_info}" >/dev/null
grep -Eq 'Type:[[:space:]]+DYN' <<<"${elf_header}"
if grep -Eq '^[[:space:]]*INTERP[[:space:]]' <<<"${program_headers}"; then
  echo "release binary contains a dynamic interpreter" >&2
  exit 1
fi
if grep -F '(NEEDED)' <<<"${dynamic_tags}"; then
  echo "release binary contains a shared-library dependency" >&2
  exit 1
fi
grep -Eq 'GNU_STACK.*RW[[:space:]]' <<<"${program_headers}"
if grep -Eq 'GNU_STACK.*RWE' <<<"${program_headers}"; then
  echo "release binary requests an executable stack" >&2
  exit 1
fi
grep -F 'GNU_RELRO' <<<"${program_headers}" >/dev/null
grep -F 'BIND_NOW' <<<"${dynamic_tags}" >/dev/null
grep -Eq '\(FLAGS_1\).*PIE' <<<"${dynamic_tags}"

printf 'Static PIE hardening checks passed: %s\n' "${binary}"
