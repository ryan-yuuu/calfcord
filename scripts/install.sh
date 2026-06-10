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
AGENTS_DIR="$CALFCORD_HOME/agents"            # operator's agent .md files (stable across updates)
STATE_DIR="$CALFCORD_HOME/state/agents"       # per-agent runtime state; matches the shim's CALFKIT_STATE_DIR
CURRENT_LINK="$CALFCORD_HOME/current"

# Native broker: pinned tansu-io/tansu release, downloaded into BIN_DIR/tansu.
TANSU_VERSION="${CALFCORD_TANSU_VERSION:-v0.6.0}"
# Process supervisor: pinned F1bonacc1/process-compose release, downloaded into
# BIN_DIR/process-compose. Pin matches the Phase-0 gate (docs §13.2): the REST
# update semantics and the disabled-slot start path are version-specific.
PROCESS_COMPOSE_VERSION="${CALFCORD_PROCESS_COMPOSE_VERSION:-v1.110.0}"
VERSION_FILE="$CALFCORD_HOME/version"

API_BASE="https://api.github.com/repos/$REPO"
DL_BASE="https://github.com/$REPO"

UV=""            # resolved by ensure_uv
INSTALLED_DEST=""   # set by install_version
PREVIOUS_SHA=""     # set by activate_version (for GC)
SEEDED_STARTER=0    # set by seed_agents when it drops in the starter agent
TANSU_OK=0          # set by ensure_tansu when the native broker binary is in place
PROCESS_COMPOSE_OK=0  # set by ensure_process_compose when the supervisor binary is in place

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

# fetch URL [accept] -> response body on stdout. Single home for the
# curl/wget + optional-auth matrix. For curl, --location-trusted keeps the
# auth header across GitHub's github.com -> codeload redirect (private mirrors).
fetch() {
  local url="$1" accept="${2:-}"
  local acc=() auth=()
  if have curl; then
    if [ -n "$accept" ]; then acc=(-H "Accept: $accept"); fi
    if [ -n "${GITHUB_TOKEN:-}" ]; then auth=(--location-trusted -H "Authorization: Bearer $GITHUB_TOKEN"); fi
    curl -fsSL "${acc[@]+"${acc[@]}"}" "${auth[@]+"${auth[@]}"}" "$url"
  elif have wget; then
    if [ -n "$accept" ]; then acc=(--header="Accept: $accept"); fi
    if [ -n "${GITHUB_TOKEN:-}" ]; then auth=(--header="Authorization: Bearer $GITHUB_TOKEN"); fi
    wget -qO- "${acc[@]+"${acc[@]}"}" "${auth[@]+"${auth[@]}"}" "$url"
  else
    die "need curl or wget"
  fi
}

require_bash() {
  [ -n "${BASH_VERSION:-}" ] || die "this installer needs bash; run: curl -fsSL <url> | bash"
}

# ------------------------------------------------------------------- steps ---

# Echo the bare 40-char commit SHA for a ref (no git; GitHub returns it directly
# via the application/vnd.github.sha media type).
resolve_sha() {
  local ref="$1"
  local sha
  sha="$(fetch "$API_BASE/commits/$ref" 'application/vnd.github.sha')"
  case "$sha" in
    "" | *[!0-9a-f]*) die "could not resolve '$ref' to a commit (got: ${sha:0:60})" ;;
  esac
  [ "${#sha}" -eq 40 ] || die "resolved '$ref' to a non-commit value (${#sha} chars): ${sha:0:60}"
  printf '%s' "$sha"
}

# Stream the source tarball for a SHA into DEST, stripping the top-level dir.
extract_source() {
  local sha="$1" dest="$2"
  mkdir -p "$dest"
  fetch "$DL_BASE/archive/$sha.tar.gz" | tar -xz -C "$dest" --strip-components=1
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

# Download the pinned native Tansu broker binary into $BIN_DIR/tansu. Best
# effort: an unsupported platform or a failed download WARNS and leaves
# TANSU_OK=0 rather than aborting the install — calfcord can still run against a
# Docker or remote broker. Net-new vs ensure_uv (which delegates to uv's own
# installer): Tansu ships raw per-target tarballs, so we detect the target
# triple, fetch, extract bin/tansu, and clear the macOS quarantine xattr that
# would otherwise block first launch.
ensure_tansu() {
  if [ -x "$BIN_DIR/tansu" ]; then
    TANSU_OK=1
    log "using existing tansu broker at $BIN_DIR/tansu"
    return 0
  fi
  local os arch
  case "$(uname -s)" in
    Darwin) os="apple-darwin" ;;
    Linux) os="unknown-linux-gnu" ;;
    *) warn "no native tansu broker for $(uname -s); use the Docker broker (docker compose up tansu) or a remote CALF_HOST_URL"; return 0 ;;
  esac
  case "$(uname -m)" in
    arm64 | aarch64) arch="aarch64" ;;
    x86_64 | amd64) arch="x86_64" ;;
    *) warn "no native tansu broker for CPU $(uname -m); use the Docker broker or a remote CALF_HOST_URL"; return 0 ;;
  esac
  local target="tansu-$arch-$os"
  local url="https://github.com/tansu-io/tansu/releases/download/$TANSU_VERSION/$target.tar.gz"
  log "installing tansu broker $TANSU_VERSION ($target) ..."
  mkdir -p "$BIN_DIR"
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/calfcord-tansu.XXXXXX")"
  # Extract the whole tarball to a temp dir then move just the binary — most
  # portable across GNU/BSD tar (avoids strip-components + leading-./ quirks).
  if ! fetch "$url" | tar -xz -C "$tmp"; then
    rm -rf "$tmp"
    warn "failed to download tansu from $url; native broker unavailable (use Docker or a remote broker)"
    return 0
  fi
  if [ ! -f "$tmp/bin/tansu" ]; then
    rm -rf "$tmp"
    warn "tansu tarball did not contain bin/tansu (release layout changed?); native broker unavailable"
    return 0
  fi
  # Guard the placement like every other step in this function: a filesystem
  # fault moving the OPTIONAL broker binary must not trip the ERR trap and abort
  # the whole install (calfcord still runs against Docker / a remote broker).
  if ! { mv "$tmp/bin/tansu" "$BIN_DIR/tansu" && chmod +x "$BIN_DIR/tansu"; }; then
    rm -rf "$tmp"
    warn "failed to install tansu into $BIN_DIR (filesystem/permissions?); native broker unavailable (use Docker or a remote broker)"
    return 0
  fi
  rm -rf "$tmp"
  # macOS quarantines downloaded binaries; clear it so first launch isn't blocked.
  if [ "$os" = "apple-darwin" ] && have xattr; then
    xattr -d com.apple.quarantine "$BIN_DIR/tansu" 2>/dev/null || true
  fi
  if [ -x "$BIN_DIR/tansu" ]; then
    TANSU_OK=1
    log "tansu broker installed at $BIN_DIR/tansu"
  else
    warn "tansu binary not executable after install; native broker unavailable"
  fi
  return 0
}

# Download the pinned process-compose supervisor binary into
# $BIN_DIR/process-compose. Mirrors ensure_tansu's best-effort contract: an
# unsupported platform or any download/extract/placement failure WARNS and
# leaves PROCESS_COMPOSE_OK=0 rather than aborting the install — calfcord can
# still run its processes manually (`calfcord run …`) or under Docker without the
# native supervisor. Two deliberate divergences from ensure_tansu, both verified
# against the real v1.110.0 release assets:
#   * os/arch use the Go-style names process-compose ships (darwin/linux,
#     arm64/amd64), not Tansu's Rust target triple.
#   * the binary sits at the TARBALL ROOT (./process-compose), not under bin/.
# Windows ships a .zip and is intentionally not bootstrapped (use WSL/Docker).
ensure_process_compose() {
  if [ -x "$BIN_DIR/process-compose" ]; then
    PROCESS_COMPOSE_OK=1
    log "using existing process-compose at $BIN_DIR/process-compose"
    return 0
  fi
  local os arch
  case "$(uname -s)" in
    Darwin) os="darwin" ;;
    Linux) os="linux" ;;
    *) warn "no native process-compose for $(uname -s); run components manually (calfcord run …) or use Docker"; return 0 ;;
  esac
  case "$(uname -m)" in
    arm64 | aarch64) arch="arm64" ;;
    x86_64 | amd64) arch="amd64" ;;
    *) warn "no native process-compose for CPU $(uname -m); run components manually (calfcord run …) or use Docker"; return 0 ;;
  esac
  local asset="process-compose_${os}_${arch}.tar.gz"
  # Capital F1bonacc1 is the actual GitHub org name — not a typo.
  local url="https://github.com/F1bonacc1/process-compose/releases/download/$PROCESS_COMPOSE_VERSION/$asset"
  log "installing process-compose $PROCESS_COMPOSE_VERSION ($os/$arch) ..."
  mkdir -p "$BIN_DIR"
  local tmp
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/calfcord-pc.XXXXXX")"
  # Extract the whole tarball to a temp dir then move just the binary — most
  # portable across GNU/BSD tar (avoids strip-components + leading-./ quirks).
  if ! fetch "$url" | tar -xz -C "$tmp"; then
    rm -rf "$tmp"
    warn "failed to download process-compose from $url; native supervisor unavailable (run components manually or use Docker)"
    return 0
  fi
  if [ ! -f "$tmp/process-compose" ]; then
    rm -rf "$tmp"
    warn "process-compose tarball did not contain process-compose (release layout changed?); native supervisor unavailable"
    return 0
  fi
  # Guard the placement like every other step: a filesystem fault moving the
  # OPTIONAL supervisor binary must not trip the ERR trap and abort the install.
  if ! { mv "$tmp/process-compose" "$BIN_DIR/process-compose" && chmod +x "$BIN_DIR/process-compose"; }; then
    rm -rf "$tmp"
    warn "failed to install process-compose into $BIN_DIR (filesystem/permissions?); native supervisor unavailable (run components manually or use Docker)"
    return 0
  fi
  rm -rf "$tmp"
  # macOS quarantines downloaded binaries; clear it so first launch isn't blocked.
  if [ "$os" = "darwin" ] && have xattr; then
    xattr -d com.apple.quarantine "$BIN_DIR/process-compose" 2>/dev/null || true
  fi
  if [ -x "$BIN_DIR/process-compose" ]; then
    PROCESS_COMPOSE_OK=1
    log "process-compose installed at $BIN_DIR/process-compose"
  else
    warn "process-compose binary not executable after install; native supervisor unavailable"
  fi
  return 0
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
# Also seed an empty mcp.json beside it (same once-only rule): the MCP CLI and
# the compose generator read it, and 0600 because entries may carry literal
# credentials.
seed_config() {
  local dest="$1"
  mkdir -p "$CONFIG_DIR"
  if [ ! -f "$CONFIG_DIR/mcp.json" ]; then
    printf '{\n  "mcpServers": {}\n}\n' > "$CONFIG_DIR/mcp.json"
    chmod 600 "$CONFIG_DIR/mcp.json"
    log "seeded MCP config at $CONFIG_DIR/mcp.json (add servers with: calfcord mcp add)"
  fi
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

# Give the native install a stable home for agent definitions and per-agent
# state, and drop in the bundled starter agent on first install. ``calfkit-agent``
# resolves these dirs from CALFKIT_AGENTS_DIR / CALFKIT_STATE_DIR — the shim points
# them at $AGENTS_DIR ($CALFCORD_HOME/agents) and $STATE_DIR ($CALFCORD_HOME/state/agents)
# respectively, so this pre-creates exactly the two dirs the runtime uses. They
# live outside the GC'd ``versions/<sha>`` tree to survive ``calfcord self update``.
# Seeding only happens when the agents dir is empty, so an operator who removed
# the starter (or added their own agents) is never clobbered on re-install.
seed_agents() {
  local dest="$1"
  mkdir -p "$AGENTS_DIR" "$STATE_DIR"
  if [ -n "$(ls -A "$AGENTS_DIR" 2>/dev/null)" ]; then
    log "keeping existing agents in $AGENTS_DIR"
    return 0
  fi
  if [ -f "$dest/agents/assistant.md" ]; then
    cp "$dest/agents/assistant.md" "$AGENTS_DIR/assistant.md"
    SEEDED_STARTER=1
    log "seeded starter agent at $AGENTS_DIR/assistant.md"
  else
    warn "no starter agent in source; create one with: calfcord init"
  fi
}

# Read one field from the existing version marker by PARSING, never sourcing
# (a repo/ref value could contain shell metacharacters) — mirrors the shim's meta().
_version_field() {
  local key="$1" line
  [ -f "$VERSION_FILE" ] || return 0
  while IFS= read -r line; do
    case "$line" in "$key="*) printf '%s' "${line#*=}"; return 0 ;; esac
  done < "$VERSION_FILE"
  return 0
}

# Flip the current symlink atomically and record the version marker.
activate_version() {
  local dest="$1" sha now old_sha
  sha="$(basename "$dest")"
  old_sha=""
  if [ -L "$CURRENT_LINK" ]; then
    old_sha="$(basename "$(readlink "$CURRENT_LINK")")"
  fi
  # Re-activating the SAME sha — a no-op re-install, or `self update` when already
  # current (it has no up-to-date short-circuit) — must NOT make the version its
  # own predecessor: that records prev == current and then `gc_versions` deletes
  # the genuine rollback target. Keep the existing previous in that case; otherwise
  # the outgoing sha becomes the new previous.
  if [ "$old_sha" = "$sha" ]; then
    PREVIOUS_SHA="$(_version_field CALFCORD_PREVIOUS_COMMIT)"
  else
    PREVIOUS_SHA="$old_sha"
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
# shellcheck disable=SC2154  # rc is assigned by rc=$? at the start of the trap body
trap 'rc=$?; printf "calfcord: failed (exit %s): %s\n" "$rc" "$BASH_COMMAND" >&2; exit "$rc"' ERR

H="${CALFCORD_HOME:-$HOME/.calfcord}"
export CALFCORD_HOME="$H"  # so calfcord-cli can locate config/.env and the agents dir

if [ "${1:-}" = "self" ]; then
  shift
  exec "$H/shims/calfcord-self" "$@"
fi

# `calfcord broker` runs the bundled native Tansu broker (a standalone Rust
# binary, NOT a uv console script, so it short-circuits before the uv passthrough
# below). Defaults are supplied via env so an operator's env or passthrough CLI
# args still override them: ephemeral memory storage, advertised on
# localhost:9092. Run it, then point the runners at CALF_HOST_URL=localhost:9092.
if [ "${1:-}" = "broker" ]; then
  shift
  TANSU="$H/bin/tansu"
  [ -x "$TANSU" ] || { echo "calfcord: native tansu broker not installed at $TANSU; re-run the installer, or use the Docker broker (docker compose up tansu)" >&2; exit 1; }
  exec env \
    STORAGE_ENGINE="${STORAGE_ENGINE:-memory://tansu/}" \
    ADVERTISED_LISTENER_URL="${ADVERTISED_LISTENER_URL:-tcp://localhost:9092}" \
    "$TANSU" broker "$@"
fi

usage() {
  cat <<'USAGE'
usage:
  calfcord init                  guided setup; ends with your first agent live in Discord
  calfcord doctor                check config, broker, Discord token/app id, and agents
  calfcord start                 open the workspace (broker + bridge — the always-on substrate)
  calfcord stop                  stop the local org
  calfcord status                show what's running locally
  calfcord logs [component] [-f] tail unified or per-component logs
  calfcord explain topology      explain how the pieces split, and why
  calfcord deploy <systemd|k8s|docker> [-o PATH]
                                 generate deployment manifests (advanced)
  calfcord broker                run a local Tansu broker (ephemeral, localhost:9092)
  calfcord run <bridge|agent|router|tools|mcp>
                                 run a calfcord process in the pinned env
  calfcord agent <create|list|show|edit|set|rename|delete|tools> [<name>]
                                 manage agents (create/inspect/edit/rename/delete)
  calfcord agent <start|stop|restart> [<name>|--all]
                                 clock an agent (or every agent on this host) in/out/reload
  calfcord router <show|set|edit|start|stop|restart [--all]>
                                 configure / run the optional ambient-message router
  calfcord tools <start|stop|restart> [--all]
                                 bring the tools host online / offline / reload
  calfcord mcp <add|list|remove> [<server>]
                                 manage MCP servers in mcp.json
  calfcord mcp <start|stop|restart> <server>|--all
                                 bring MCP servers online / offline / reload
  calfcord auth [args]           Codex (ChatGPT subscription) login
  calfcord self <version|status|update|rollback|set-broker>
USAGE
}

# Explicit help -> stdout, exit 0; a bare invocation -> usage on stderr, exit 2.
# (stdout-for-help diverges from calfcord-self, which writes help to stderr; intentional.)
case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  "") usage >&2; exit 2 ;;
esac

UV="$H/bin/uv"
if [ ! -x "$UV" ]; then
  UV="$(command -v uv || true)"
fi
{ [ -n "$UV" ] && [ -x "$UV" ]; } || { echo "calfcord: uv not found; re-run the installer" >&2; exit 1; }
[ -e "$H/current" ] || { echo "calfcord: no active install at $H/current; re-run the installer" >&2; exit 1; }

ENVF="$H/config/.env"

# Default calfcord's runtime dirs into the install layout unless the operator
# already chose them (shell env OR config .env wins — checked here so we don't
# depend on `uv run --env-file` precedence). Agents and per-agent state live
# under the install home so they survive `self update` and are found from any
# directory; the tools workspace defaults to the *launch* directory so agents
# act where you ran the command (like Claude Code). Override any of these in
# config/.env.
#
# The `^$1=.` grep requires at least one char after the `=`: a bare `KEY=`
# (which `.env.example` ships for CALFCORD_WORKSPACE_DIR) counts as UNSET, so
# the workspace still defaults to $PWD. An operator must give a real value to
# override the default.
_default_env() {  # name default
  [ -n "${!1:-}" ] && return 0
  [ -f "$ENVF" ] && grep -q "^$1=." "$ENVF" && return 0
  export "$1=$2"
}
_default_env CALFKIT_AGENTS_DIR     "$H/agents"
_default_env CALFKIT_STATE_DIR      "$H/state/agents"
_default_env CALFCORD_WORKSPACE_DIR "$PWD"

# Translate friendly verbs to the underlying console scripts. Management verbs go to the
# calfcord-cli argparse entry point; raw `calfcord calfkit-*` runner names aren't matched
# here and fall through to the `uv run` passthrough below, so they keep working unchanged.
case "${1:-}" in
  # Management + day-to-day lifecycle verbs all resolve to the calfcord-cli
  # argparse entry point. `start|stop|status` drive the process-compose
  # supervisor; `_healthcheck` is the readiness-probe command PC's exec probes
  # invoke (`calfcord _healthcheck <component>`). These are listed explicitly so
  # they don't fall through to the `uv run` passthrough (which would try to exec
  # a nonexistent `start`/`stop`/… console script). `tools` is a calfcord-cli
  # verb group (the singleton tools-host lifecycle: `tools start|stop|restart`);
  # `mcp` is too (per-server MCP lifecycle + mcp.json management).
  # The graduation-tier verbs (`explain` / `logs` /
  # `deploy`) are calfcord-cli subcommands too — listed here so their sub-args
  # forward verbatim to the argparse entry point instead of the `uv run` passthrough.
  init|agent|router|tools|mcp|doctor|_healthcheck|start|stop|status|logs|explain|deploy) set -- calfcord-cli "$@" ;;
  run)
    shift
    case "${1:-}" in
      bridge|agent|router|tools|mcp) set -- "calfkit-$1" "${@:2}" ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac ;;
  auth) shift; set -- calfkit-auth "$@" ;;
esac

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
    echo "CALF_HOST_URL=$val" >> "$tmp" || { echo "calfcord self: failed to write $CONFIG_ENV" >&2; rm -f "$tmp"; exit 1; }
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
  # The literal $PATH is intentional: it must be expanded by the shell at
  # profile-load time, not now.
  # shellcheck disable=SC2016
  local line='export PATH="'"$SHIM_DIR"':$PATH"'
  for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    [ -e "$rc" ] || continue
    if ! grep -qsF "$SHIM_DIR" "$rc"; then
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
  ensure_tansu
  ensure_process_compose
  local sha
  sha="$(resolve_sha "$REF")"
  log "resolved $REF -> ${sha:0:12}"
  install_version "$sha"
  seed_config "$INSTALLED_DEST"
  seed_agents "$INSTALLED_DEST"
  activate_version "$INSTALLED_DEST"
  gc_versions "$sha" "$PREVIOUS_SHA"
  write_shims
  ensure_path
  log "done."
  log "  version:  calfcord self version"
  log "  config:   $CONFIG_ENV  (set CALF_HOST_URL, or: calfcord self set-broker <url>)"
  if [ "$TANSU_OK" -eq 1 ]; then
    log "  broker:   calfcord broker   (native Tansu, ephemeral, localhost:9092)"
  else
    log "  broker:   native broker unavailable — use Docker (docker compose up tansu) or a remote CALF_HOST_URL"
  fi
  if [ "$PROCESS_COMPOSE_OK" -eq 1 ]; then
    log "  supervisor: process-compose $PROCESS_COMPOSE_VERSION installed"
  else
    log "  supervisor: process-compose unavailable — run components manually (calfcord run …) or use Docker"
  fi
  if [ "$SEEDED_STARTER" -eq 1 ]; then
    log "  agents:   $AGENTS_DIR  (starter: assistant.md)"
  else
    log "  agents:   $AGENTS_DIR"
  fi
  log "  check:    calfcord doctor"
  log "  setup:    calfcord init      (guided; ends with your first agent live in Discord)"
  log "  status:   calfcord status    (the org board, once you're up)"
}

# Run main only when executed (``bash install.sh``) or piped (``curl | bash``),
# never when sourced — so tests can source this file to exercise individual
# functions. Piped execution leaves ``BASH_SOURCE[0]`` empty; a file execution
# makes it equal to ``$0``; sourcing makes it a non-empty path that differs from
# ``$0``. The ``:-`` guards keep this safe under ``set -u``.
if [ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
  main "$@"
fi
