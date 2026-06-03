#!/usr/bin/env bash
#
# Lint (with shellcheck) the installer AND the two shims it generates. The
# shims live inside single-quoted heredocs in install.sh, so shellcheck treats
# them as opaque strings — we materialize them first, then lint all three.
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
B="${BASH:-bash}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
lib="$tmp/lib.sh"
sed '$d' "$ROOT/scripts/install.sh" > "$lib"   # strip `main "$@"`
CALFCORD_HOME="$tmp" "$B" -c "source '$lib'; write_shims"

shellcheck -s bash "$ROOT/scripts/install.sh"
shellcheck -s bash "$tmp/shims/calfcord"
shellcheck -s bash "$tmp/shims/calfcord-self"
shellcheck -s bash "$ROOT/scripts/tests/test_installer.sh"
shellcheck -s bash "$ROOT/scripts/tests/lint.sh"
echo "shellcheck: clean"
