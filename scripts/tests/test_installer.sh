#!/usr/bin/env bash
#
# Behavioral tests for the calfcord installer's generated shims and pure
# helpers. Network-free: `uv`, `curl`, and the source download are stubbed, so
# this runs anywhere with no GitHub access.
#
# Run with `bash scripts/tests/test_installer.sh`. CI runs it under /bin/bash
# on macOS to exercise the bash 3.2 compatibility contract; the harness invokes
# the shims and the installer library under the SAME interpreter ($BASH) so
# that coverage is real.
#
# `cmd && pass || fail` below is the intentional test idiom; pass/fail are
# printf wrappers that never fail, so the SC2015 caveat does not apply here.
# shellcheck disable=SC2015
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
B="${BASH:-bash}"          # run shims/lib under whatever bash ran this suite
FAIL=0
pass(){ printf 'PASS  %s\n' "$1"; }
fail(){ printf 'FAIL  %s\n' "$1"; FAIL=1; }

BASE="$(mktemp -d)"
trap 'rm -rf "$BASE"' EXIT
TD="$BASE/home"; mkdir -p "$TD"
SB="$BASE/stubbin"; mkdir -p "$SB"
LIB="$BASE/lib.sh"; sed '$d' "$ROOT/scripts/install.sh" > "$LIB"   # strip `main "$@"`

CALFCORD_HOME="$TD" "$B" -c "source '$LIB'; write_shims" || fail "write_shims"
C="$TD/shims/calfcord"
CS="$TD/shims/calfcord-self"
export CALFCORD_HOME="$TD"

A=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
Bsha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
mkdir -p "$TD/bin" "$TD/config" "$TD/versions/$A" "$TD/versions/$Bsha"
: > "$TD/versions/$A/.calfcord-ok"
: > "$TD/versions/$Bsha/.calfcord-ok"
printf '#!/usr/bin/env bash\necho "STUB_UV $*"\n' > "$TD/bin/uv"; chmod +x "$TD/bin/uv"
printf 'CALF_HOST_URL=\nDISCORD_BOT_TOKEN=keep-me\n' > "$TD/config/.env"; chmod 600 "$TD/config/.env"
ln -sfn "$TD/versions/$Bsha" "$TD/current"

marker(){ # $1=commit  $2=previous(optional)
  { printf 'CALFCORD_COMMIT=%s\nCALFCORD_INSTALLED_AT=2026-01-01T00:00:00Z\n' "$1"
    printf 'CALFCORD_REPO=ryan-yuuu/calfcord\nCALFCORD_REF=main\n'
    [ -n "${2:-}" ] && printf 'CALFCORD_PREVIOUS_COMMIT=%s\n' "$2"; } > "$TD/version"
}
marker "$Bsha" "$A"

# version reflects the parsed marker
"$B" "$CS" version 2>&1 | grep -q "bbbbbbbbbbbb" && pass "version shows marker commit" || fail "version"

# `calfcord self ...` routes to calfcord-self
"$B" "$C" self version 2>&1 | grep -q "bbbbbbbbbbbb" && pass "self routing" || fail "self routing"

# a metacharacter-laden ref is DATA (parsed), never executed
PWN="$BASE/PWNED"
# the literal backticks are the point of this test — they must NOT be executed
# shellcheck disable=SC2016
{ printf 'CALFCORD_COMMIT=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n'
  printf 'CALFCORD_REF=main`touch %s`\nCALFCORD_REPO=ryan-yuuu/calfcord\n' "$PWN"; } > "$TD/version"
"$B" "$CS" version >/dev/null 2>&1
[ ! -e "$PWN" ] && pass "metacharacter ref not executed" || fail "ref code-exec!"
marker "$Bsha" "$A"

# passthrough forwards exact args to `uv run`
out="$("$B" "$C" calfkit-agent --foo bar 2>&1)"
printf '%s' "$out" | grep -Fq \
  "STUB_UV run --frozen --no-sync --project $TD/current --env-file $TD/config/.env -- calfkit-agent --foo bar" \
  && pass "passthrough args" || fail "passthrough: $out"

# set-broker: single replaced line, other keys preserved, mode 600
"$B" "$CS" set-broker kafka-b:19092 >/dev/null 2>&1
n="$(grep -c '^CALF_HOST_URL=' "$TD/config/.env")"
v="$(grep '^CALF_HOST_URL=' "$TD/config/.env")"
k="$(grep -c '^DISCORD_BOT_TOKEN=keep-me$' "$TD/config/.env")"
{ [ "$n" -eq 1 ] && [ "$v" = "CALF_HOST_URL=kafka-b:19092" ] && [ "$k" -eq 1 ]; } \
  && pass "set-broker replace + preserve" || fail "set-broker (n=$n v=$v k=$k)"
perm="$(stat -f '%Lp' "$TD/config/.env" 2>/dev/null || stat -c '%a' "$TD/config/.env")"
[ "$perm" = "600" ] && pass "config perms 600" || fail "perms ($perm)"

# rollback: uses recorded previous, flips, rewrites marker with swapped previous
"$B" "$CS" rollback >/dev/null 2>&1; rc=$?
tgt="$(readlink "$TD/current")"
{ [ "$rc" -eq 0 ] && [ "$tgt" = "$TD/versions/$A" ]; } && pass "rollback flips to previous" || fail "rollback (rc=$rc)"
{ grep -q "CALFCORD_COMMIT=aaaa" "$TD/version" && grep -q "CALFCORD_PREVIOUS_COMMIT=bbbb" "$TD/version"; } \
  && pass "rollback rewrites marker" || fail "rollback marker"

# rollback errors when there is no valid previous
marker "$Bsha" ""; ln -sfn "$TD/versions/$Bsha" "$TD/current"
"$B" "$CS" rollback >/dev/null 2>&1; [ $? -eq 1 ] && pass "rollback errors w/o previous" || fail "rollback no-prev"
marker "$Bsha" cccccccccccccccccccccccccccccccccccccccc   # previous lacks a built dir/sentinel
"$B" "$CS" rollback >/dev/null 2>&1; [ $? -eq 1 ] && pass "rollback errors w/o sentinel" || fail "rollback no-sentinel"
marker "$Bsha" "$A"

# status: offline -> branded error + exit 1
printf '#!/usr/bin/env bash\nexit 7\n' > "$SB/curl"; chmod +x "$SB/curl"
out="$(PATH="$SB:$PATH" "$B" "$CS" status 2>&1)"; rc=$?
{ [ "$rc" -eq 1 ] && printf '%s' "$out" | grep -q "could not reach GitHub"; } \
  && pass "status offline -> branded error" || fail "status offline (rc=$rc): $out"

# status: outdated when remote sha differs
printf '#!/usr/bin/env bash\nprintf "%%s" ffffffffffffffffffffffffffffffffffffffff\n' > "$SB/curl"; chmod +x "$SB/curl"
PATH="$SB:$PATH" "$B" "$CS" status 2>&1 | grep -q "outdated" && pass "status outdated" || fail "status outdated"

# update: forwards CALFCORD_REF/REPO/HOME into the re-run
{ printf 'CALFCORD_COMMIT=%s\nCALFCORD_INSTALLED_AT=x\n' "$Bsha"
  printf 'CALFCORD_REPO=someone/fork\nCALFCORD_REF=release-9\nCALFCORD_PREVIOUS_COMMIT=%s\n' "$A"; } > "$TD/version"
cat > "$SB/curl" <<'STUBCURL'
#!/usr/bin/env bash
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
[ -n "$out" ] && printf '#!/usr/bin/env bash\necho "FWD ref=$CALFCORD_REF repo=$CALFCORD_REPO home=$CALFCORD_HOME"\n' > "$out"
exit 0
STUBCURL
chmod +x "$SB/curl"
PATH="$SB:$PATH" "$B" "$CS" update 2>&1 | grep -Fq "FWD ref=release-9 repo=someone/fork home=$TD" \
  && pass "update forwards ref/repo/home" || fail "update forwarding"
marker "$Bsha" "$A"

# resolve_sha: accepts a 40-hex sha, rejects short / non-hex
resolve_check(){ # $1=canned curl output  $2=ok|die
  { printf '#!/usr/bin/env bash\n'; printf 'printf "%%s" %q\n' "$1"; } > "$SB/curl"; chmod +x "$SB/curl"
  o="$(PATH="$SB:$PATH" "$B" -c "source '$LIB'; resolve_sha main" 2>&1)"; r=$?
  if [ "$2" = ok ]; then [ "$r" -eq 0 ] && [ "$o" = "$1" ]; else [ "$r" -ne 0 ]; fi
}
resolve_check 1111111111111111111111111111111111111111 ok  && pass "resolve_sha accepts 40-hex" || fail "resolve 40-hex"
resolve_check abcabc die                                   && pass "resolve_sha rejects short" || fail "resolve short"
resolve_check "Not Found" die                              && pass "resolve_sha rejects non-hex" || fail "resolve non-hex"

# ensure_path writes to ~/.bash_profile (macOS bash login shells)
H2="$BASE/home2"; mkdir -p "$H2"; : > "$H2/.bash_profile"
CALFCORD_HOME="$TD" HOME="$H2" PATH="/usr/bin:/bin" "$B" -c "source '$LIB'; ensure_path" >/dev/null 2>&1
grep -q "$TD/shims" "$H2/.bash_profile" && pass "ensure_path writes ~/.bash_profile" || fail "bash_profile"

echo "----"; [ "$FAIL" -eq 0 ] && echo "ALL TESTS PASSED" || echo "SOME TESTS FAILED"
exit "$FAIL"
