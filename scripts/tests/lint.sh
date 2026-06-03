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

# Gate at warning+ so the result is stable across shellcheck versions: info /
# style checks differ between releases (and CI's apt build lags the latest).
# The sources are also info-clean under recent shellcheck locally; intentional
# exceptions carry inline `# shellcheck disable=` directives.
sc() { shellcheck --severity=warning --shell=bash "$@"; }
sc "$ROOT/scripts/install.sh"
sc "$tmp/shims/calfcord"
sc "$tmp/shims/calfcord-self"
sc "$ROOT/scripts/tests/test_installer.sh"
sc "$ROOT/scripts/tests/lint.sh"
echo "shellcheck: clean"
