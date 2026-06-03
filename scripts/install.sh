#!/usr/bin/env bash
#
# calfcord installer — native, no-prerequisites, reproducible one-line install.
#
#   curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/calfcord/main/scripts/install.sh | bash
#
# What it does, making NO assumptions about the box (no git, no system Python):
#   1. bootstraps `uv` (a static binary) privately under ~/.calfcord
#   2. pins + downloads the source for a single commit of `main` (tarball, no git)
#   3. builds an isolated, locked venv with `uv sync --locked --no-dev`
#   4. installs a `calfcord` command that thinly wraps `uv run` in that venv
#
# Each version is built in its own `versions/<sha>` dir (Python venvs are not
# relocatable, so they must be built in their final home); a `current` symlink
# is flipped only after a build succeeds, making activation atomic and rollback
# a symlink flip. The command surface is a pure passthrough — `calfcord <x>`
# forwards `<x>` to `uv run`, so new entry points need no installer changes.
#
# Env knobs:
#   CALFCORD_HOME   install root          (default: ~/.calfcord)
#   CALFCORD_REF    branch or commit SHA  (default: main)
#   CALFCORD_REPO   owner/repo            (default: ryan-yuuu/calfcord)
#   GITHUB_TOKEN    optional, for API rate limits / private mirrors
#
set -Eeuo pipefail

# ------------------------------------------------------------------ config ---
REPO="${CALFCORD_REPO:-ryan-yuuu/calfcord}"
REF="${CALFCORD_REF:-main}"
CALFCORD_HOME="${CALFCORD_HOME:-$HOME/.calfcord}"

BIN_DIR="$CALFCORD_HOME/bin"          # private uv (NOT placed on PATH)
SHIM_DIR="$CALFCORD_HOME/shims"       # calfcord + calfcord-self (placed on PATH)
VERSIONS_DIR="$CALFCORD_HOME/versions"
CONFIG_DIR="$CALFCORD_HOME/config"
CONFIG_ENV="$CONFIG_DIR/.env"
CURRENT_LINK="$CALFCORD_HOME/current"
VERSION_FILE="$CALFCORD_HOME/version"

API_BASE="https://api.github.com/repos/$REPO"
DL_BASE="https://github.com/$REPO"

UV=""            # resolved by ensure_uv
INSTALLED_DEST=""   # set by install_version
PREVIOUS_SHA=""     # set by activate_version (for GC)

# ---------------------------------------------------------------------- ui ---
if [ -t 2 ]; then
  C_I=$'\033[1;36m'; C_W=$'\033[1;33m'; C_E=$'\033[1;31m'; C_0=$'\033[0m'
else
  C_I=''; C_W=''; C_E=''; C_0=''
fi
log()  { printf '%scalfcord%s %s\n' "$C_I" "$C_0" "$*" >&2; }
warn() { printf '%scalfcord%s %s\n' "$C_W" "$C_0" "$*" >&2; }
die()  { printf '%scalfcord error%s %s\n' "$C_E" "$C_0" "$*" >&2; exit 1; }
trap 'die "install failed: $BASH_COMMAND"' ERR

have() { command -v "$1" >/dev/null 2>&1; }

# True if this uv supports the flags the calfcord shim relies on (notably
# `uv run --env-file`, a relatively recent addition).
uv_supported() { "$1" run --help 2>/dev/null | grep -q -- '--env-file'; }

require_bash() {
  [ -n "${BASH_VERSION:-}" ] || die "this installer needs bash; run: curl -fsSL <url> | bash"
}

# ------------------------------------------------------------------- steps ---

# Echo the bare 40-char commit SHA for a ref (no git; GitHub returns it directly
# via the application/vnd.github.sha media type).
resolve_sha() {
  local ref="$1"
  local url="$API_BASE/commits/$ref"
  local sha
  if have curl; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      sha="$(curl -fsSL -H 'Accept: application/vnd.github.sha' -H "Authorization: Bearer $GITHUB_TOKEN" "$url")"
    else
      sha="$(curl -fsSL -H 'Accept: application/vnd.github.sha' "$url")"
    fi
  elif have wget; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      sha="$(wget -qO- --header='Accept: application/vnd.github.sha' --header="Authorization: Bearer $GITHUB_TOKEN" "$url")"
    else
      sha="$(wget -qO- --header='Accept: application/vnd.github.sha' "$url")"
    fi
  else
    die "need curl or wget"
  fi
  case "$sha" in
    "" | *[!0-9a-f]*) die "could not resolve '$ref' to a commit (got: ${sha:0:60})" ;;
  esac
  [ "${#sha}" -eq 40 ] || die "resolved '$ref' to a non-commit value (${#sha} chars): ${sha:0:60}"
  printf '%s' "$sha"
}

# Stream the source tarball for a SHA into DEST, stripping the top-level dir.
extract_source() {
  local sha="$1" dest="$2"
  local url="$DL_BASE/archive/$sha.tar.gz"
  mkdir -p "$dest"
  if have curl; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      # --location-trusted keeps the auth header across the github.com -> codeload
      # redirect (curl drops it by default), so private repos / mirrors work.
      curl -fsS --location-trusted -H "Authorization: Bearer $GITHUB_TOKEN" "$url" | tar -xz -C "$dest" --strip-components=1
    else
      curl -fsSL "$url" | tar -xz -C "$dest" --strip-components=1
    fi
  else
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      wget -qO- --header="Authorization: Bearer $GITHUB_TOKEN" "$url" | tar -xz -C "$dest" --strip-components=1
    else
      wget -qO- "$url" | tar -xz -C "$dest" --strip-components=1
    fi
  fi
}

# Bootstrap uv privately, or reuse an existing one.
ensure_uv() {
  if [ -x "$BIN_DIR/uv" ]; then
    UV="$BIN_DIR/uv"
  elif have uv && uv_supported "$(command -v uv)"; then
    UV="$(command -v uv)"
    log "using existing uv at $UV"
  else
    if have uv; then
      warn "system uv lacks 'uv run --env-file'; installing a private uv under $BIN_DIR"
    else
      log "installing uv (no system Python or git required)..."
    fi
    mkdir -p "$BIN_DIR"
    if have curl; then
      curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$BIN_DIR" sh
    elif have wget; then
      wget -qO- https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$BIN_DIR" sh
    else
      die "need curl or wget to install uv"
    fi
    UV="$BIN_DIR/uv"
  fi
  [ -x "$UV" ] || die "uv unavailable after bootstrap"
}

# Build versions/<sha> in place (idempotent). Sets INSTALLED_DEST.
install_version() {
  local sha="$1"
  local dest="$VERSIONS_DIR/$sha"
  INSTALLED_DEST="$dest"
  if [ -f "$dest/.calfcord-ok" ]; then
    log "version ${sha:0:12} already built — reusing"
    return 0
  fi
  log "downloading source @ ${sha:0:12} ..."
  rm -rf "$dest"
  extract_source "$sha" "$dest"
  [ -f "$dest/pyproject.toml" ] || die "extracted source looks wrong (no pyproject.toml)"
  log "building isolated environment (uv sync --locked --no-dev) ..."
  ( cd "$dest" && "$UV" sync --locked --no-dev )
  : > "$dest/.calfcord-ok"
}

# Copy .env.example -> config/.env once; never clobber an operator's edits.
seed_config() {
  local dest="$1"
  mkdir -p "$CONFIG_DIR"
  if [ -f "$CONFIG_ENV" ]; then
    log "keeping existing config at $CONFIG_ENV"
    return 0
  fi
  if [ -f "$dest/.env.example" ]; then
    cp "$dest/.env.example" "$CONFIG_ENV"
  else
    : > "$CONFIG_ENV"
  fi
  chmod 600 "$CONFIG_ENV"
  log "seeded config at $CONFIG_ENV (fill in DISCORD_*, CALF_HOST_URL, API keys)"
}

# Flip the current symlink atomically and record the version marker.
activate_version() {
  local dest="$1" sha now
  sha="$(basename "$dest")"
  PREVIOUS_SHA=""
  if [ -L "$CURRENT_LINK" ]; then
    PREVIOUS_SHA="$(basename "$(readlink "$CURRENT_LINK")")"
  fi
  ln -sfn "$dest" "$CURRENT_LINK"
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  cat > "$VERSION_FILE" <<EOF
CALFCORD_COMMIT=$sha
CALFCORD_INSTALLED_AT=$now
CALFCORD_REPO=$REPO
CALFCORD_REF=$REF
CALFCORD_PREVIOUS_COMMIT=$PREVIOUS_SHA
EOF
}

# Keep only current + previous version dirs.
gc_versions() {
  local cur="$1" prev="${2:-}" d b
  for d in "$VERSIONS_DIR"/*/; do
    [ -d "$d" ] || continue
    b="$(basename "$d")"
    [ "$b" = "$cur" ] && continue
    [ -n "$prev" ] && [ "$b" = "$prev" ] && continue
    log "pruning old version ${b:0:12}"
    rm -rf "$d"
  done
}

write_shims() {
  mkdir -p "$SHIM_DIR"

  cat > "$SHIM_DIR/calfcord" <<'CALF_SHIM'
#!/usr/bin/env bash
# calfcord — thin passthrough to `uv run` inside the pinned install.
# `calfcord <command> [args]` runs any console script in the locked env;
# `calfcord self ...` handles install management. New entry points need no
# changes here.
set -euo pipefail
trap 'rc=$?; printf "calfcord: failed (exit %s): %s\n" "$rc" "$BASH_COMMAND" >&2; exit "$rc"' ERR

H="${CALFCORD_HOME:-$HOME/.calfcord}"

if [ "${1:-}" = "self" ]; then
  shift
  exec "$H/shims/calfcord-self" "$@"
fi

if [ "$#" -eq 0 ]; then
  cat >&2 <<'USAGE'
usage:
  calfcord <command> [args...]   run a calfcord process in the pinned env, e.g.
                                   calfcord calfkit-bridge
                                   calfcord calfkit-agent
                                   calfcord calfkit-router
                                   calfcord calfkit-tools
  calfcord self <version|status|update|rollback|set-broker>
USAGE
  exit 2
fi

UV="$H/bin/uv"
if [ ! -x "$UV" ]; then
  UV="$(command -v uv || true)"
fi
{ [ -n "$UV" ] && [ -x "$UV" ]; } || { echo "calfcord: uv not found; re-run the installer" >&2; exit 1; }
[ -e "$H/current" ] || { echo "calfcord: no active install at $H/current; re-run the installer" >&2; exit 1; }

ENVF="$H/config/.env"
if [ -f "$ENVF" ]; then
  exec "$UV" run --frozen --no-sync --project "$H/current" --env-file "$ENVF" -- "$@"
else
  exec "$UV" run --frozen --no-sync --project "$H/current" -- "$@"
fi
CALF_SHIM

  cat > "$SHIM_DIR/calfcord-self" <<'CALF_SELF'
#!/usr/bin/env bash
# calfcord self-management: version | status | update | rollback | set-broker
set -euo pipefail
trap 'rc=$?; printf "calfcord self: failed (exit %s): %s\n" "$rc" "$BASH_COMMAND" >&2; exit "$rc"' ERR

H="${CALFCORD_HOME:-$HOME/.calfcord}"
VERSION_FILE="$H/version"
VERSIONS_DIR="$H/versions"
CURRENT_LINK="$H/current"
CONFIG_ENV="$H/config/.env"

# Read the install marker by PARSING, never sourcing: a ref/repo containing
# shell metacharacters must be treated as data, not executed.
meta() {
  local _line
  [ -f "$VERSION_FILE" ] || return 0
  while IFS= read -r _line; do
    case "$_line" in "$1="*) printf '%s' "${_line#*=}"; return 0 ;; esac
  done < "$VERSION_FILE"
  return 0
}
CALFCORD_COMMIT="$(meta CALFCORD_COMMIT)"
CALFCORD_INSTALLED_AT="$(meta CALFCORD_INSTALLED_AT)"
CALFCORD_REPO="$(meta CALFCORD_REPO)"
CALFCORD_REF="$(meta CALFCORD_REF)"
CALFCORD_PREVIOUS_COMMIT="$(meta CALFCORD_PREVIOUS_COMMIT)"
REPO="${CALFCORD_REPO:-ryan-yuuu/calfcord}"

short() { printf '%s' "${1:0:12}"; }

remote_sha() {
  local ref="${1:-main}"
  local url="https://api.github.com/repos/$REPO/commits/$ref"
  if command -v curl >/dev/null 2>&1; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      curl -fsSL -H 'Accept: application/vnd.github.sha' -H "Authorization: Bearer $GITHUB_TOKEN" "$url"
    else
      curl -fsSL -H 'Accept: application/vnd.github.sha' "$url"
    fi
  elif command -v wget >/dev/null 2>&1; then
    if [ -n "${GITHUB_TOKEN:-}" ]; then
      wget -qO- --header='Accept: application/vnd.github.sha' --header="Authorization: Bearer $GITHUB_TOKEN" "$url"
    else
      wget -qO- --header='Accept: application/vnd.github.sha' "$url"
    fi
  else
    echo "calfcord self: need curl or wget" >&2; return 1
  fi
}

cmd="${1:-}"; [ "$#" -gt 0 ] && shift || true
case "$cmd" in
  version)
    echo "commit:       ${CALFCORD_COMMIT:-unknown}"
    echo "installed_at: ${CALFCORD_INSTALLED_AT:-unknown}"
    echo "repo:         $REPO"
    echo "ref:          ${CALFCORD_REF:-main}"
    ;;
  status)
    have="${CALFCORD_COMMIT:-}"
    [ -n "$have" ] || { echo "no install metadata; re-run the installer" >&2; exit 1; }
    ref="${CALFCORD_REF:-main}"
    if ! latest="$(remote_sha "$ref")" || [ -z "$latest" ]; then
      echo "calfcord self: could not reach GitHub to check for updates (offline or rate-limited)" >&2
      exit 1
    fi
    if [ "$have" = "$latest" ]; then
      echo "up to date ($(short "$have") on $ref)"
    else
      echo "outdated: have $(short "$have"), latest $(short "$latest") on $ref"
      echo "run 'calfcord self update' to upgrade"
    fi
    ;;
  update)
    url="https://raw.githubusercontent.com/$REPO/main/scripts/install.sh"
    ref="${CALFCORD_REF:-main}"
    echo "calfcord: updating $REPO ($ref)..." >&2
    tmp="$(mktemp)"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$url" -o "$tmp" || { echo "calfcord self: update download failed" >&2; rm -f "$tmp"; exit 1; }
    else
      wget -qO- "$url" > "$tmp" || { echo "calfcord self: update download failed" >&2; rm -f "$tmp"; exit 1; }
    fi
    [ -s "$tmp" ] || { echo "calfcord self: downloaded installer is empty" >&2; rm -f "$tmp"; exit 1; }
    # Re-run for the SAME ref/repo/home this install used, not a hardcoded main.
    rc=0
    CALFCORD_REPO="$REPO" CALFCORD_REF="$ref" CALFCORD_HOME="$H" bash "$tmp" || rc=$?
    rm -f "$tmp"
    [ "$rc" -eq 0 ] || exit "$rc"
    ;;
  rollback)
    [ -L "$CURRENT_LINK" ] || { echo "no active install to roll back" >&2; exit 1; }
    cur_sha="$(basename "$(readlink "$CURRENT_LINK")")"
    prev="${CALFCORD_PREVIOUS_COMMIT:-}"
    if [ -z "$prev" ] || [ ! -f "$VERSIONS_DIR/$prev/.calfcord-ok" ]; then
      echo "calfcord self: no valid previous version to roll back to" >&2
      exit 1
    fi
    ln -sfn "$VERSIONS_DIR/$prev" "$CURRENT_LINK"
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    cat > "$VERSION_FILE" <<EOF
CALFCORD_COMMIT=$prev
CALFCORD_INSTALLED_AT=$now
CALFCORD_REPO=$REPO
CALFCORD_REF=${CALFCORD_REF:-main}
CALFCORD_PREVIOUS_COMMIT=$cur_sha
EOF
    echo "rolled back to $(short "$prev")"
    ;;
  set-broker)
    val="${1:-}"
    [ -n "$val" ] || { echo "usage: calfcord self set-broker <host:port>" >&2; exit 2; }
    mkdir -p "$(dirname "$CONFIG_ENV")"
    [ -f "$CONFIG_ENV" ] || { : > "$CONFIG_ENV"; chmod 600 "$CONFIG_ENV"; }
    tmp="$(mktemp)"
    rc=0
    grep -v '^CALF_HOST_URL=' "$CONFIG_ENV" > "$tmp" || rc=$?
    if [ "$rc" -gt 1 ]; then
      echo "calfcord self: failed to read $CONFIG_ENV (grep exit $rc)" >&2; rm -f "$tmp"; exit 1
    fi
    echo "CALF_HOST_URL=$val" >> "$tmp"
    mv "$tmp" "$CONFIG_ENV"
    chmod 600 "$CONFIG_ENV"
    echo "set CALF_HOST_URL=$val in $CONFIG_ENV"
    ;;
  ""|-h|--help|help)
    cat >&2 <<'USAGE'
calfcord self <command>:
  version              show installed commit + timestamp
  status               compare installed commit to the latest on the branch
  update               re-run the installer to upgrade to the latest
  rollback             switch back to the previous installed version
  set-broker <host:port>  set CALF_HOST_URL (Kafka bootstrap) in the config .env
USAGE
    [ -z "$cmd" ] && exit 2
    exit 0
    ;;
  *)
    echo "calfcord self: unknown command '$cmd'" >&2
    exit 2
    ;;
esac
CALF_SELF

  chmod +x "$SHIM_DIR/calfcord" "$SHIM_DIR/calfcord-self"
}

ensure_path() {
  case ":$PATH:" in
    *":$SHIM_DIR:"*) return 0 ;;
  esac
  local rc added=0
  local line='export PATH="'"$SHIM_DIR"':$PATH"'
  for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    [ -e "$rc" ] || continue
    if ! grep -qs "$SHIM_DIR" "$rc"; then
      printf '\n# calfcord\n%s\n' "$line" >> "$rc"
      log "added $SHIM_DIR to PATH in $rc"
      added=1
    fi
  done
  if [ "$added" -eq 0 ]; then
    warn "add this line to your shell profile, then restart your shell:"
    warn "  $line"
  else
    warn "restart your shell, or run now:  $line"
  fi
}

# -------------------------------------------------------------------- main ---
main() {
  require_bash
  log "installing calfcord from $REPO @ $REF"
  mkdir -p "$CALFCORD_HOME" "$VERSIONS_DIR"
  ensure_uv
  local sha
  sha="$(resolve_sha "$REF")"
  log "resolved $REF -> ${sha:0:12}"
  install_version "$sha"
  seed_config "$INSTALLED_DEST"
  activate_version "$INSTALLED_DEST"
  gc_versions "$sha" "$PREVIOUS_SHA"
  write_shims
  ensure_path
  log "done."
  log "  version:  calfcord self version"
  log "  config:   $CONFIG_ENV  (set CALF_HOST_URL, or: calfcord self set-broker <url>)"
  log "  deploy:   calfcord calfkit-bridge | calfkit-agent | calfkit-router | calfkit-tools"
}

main "$@"
