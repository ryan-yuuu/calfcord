# Authoring a calfcord Tool

How to add a new tool that any calfcord agent can invoke. This is the
contributor reference for the file-drop workflow introduced in PR 2; for
the architectural background on tools as a calfkit node type, see
`src/calfkit_organization/tools/__init__.py` and the calfkit docs.

## 1. Overview

Tools are how an agent's LLM does anything besides emit text. The LLM
selects a tool from the schema list it was advertised, calfkit ships the
call across Kafka to the `calfkit-tools` process, that process executes
the tool's Python body, and the return string flows back to the LLM as
the tool result.

```
agent process (LLM picks tool, emits Call)
        │
        ▼  Kafka topic  tool.<name>.input
calfkit-tools process (executes async Python body)
        │
        ▼  Kafka topic  tool.<name>.output
agent process (LLM sees the string return)
```

A tool is, concretely, **an `async` function decorated with
`@agent_tool` from `calfkit.nodes` that returns `str`**. Tools live in
`src/calfkit_organization/tools/builtin/`. Drop a `.py` file there with
a `ToolNodeDef` attribute at module scope and the next `calfkit-tools`
boot picks it up — no edits to a central registry, no entry-point
metadata, no second deploy. The discovery walk in
`src/calfkit_organization/tools/discovery.py` does the work.

The canonical references are the modules under
`src/calfkit_organization/tools/builtin/`. Read a couple before writing
a new one.

## 2. The 3-step workflow

A worked example: a `pypi_info` tool that fetches package metadata from
PyPI's public JSON API. Useful in practice — agents can answer
"what version is requests on?" without web scraping — and exercises
every pattern this doc covers.

### Step 1 — Write the function

```python
# src/calfkit_organization/tools/builtin/pypi.py
"""``pypi_info`` — look up a package's metadata on PyPI.

Thin wrapper around the public ``https://pypi.org/pypi/<name>/json``
endpoint. No API key. The endpoint returns the package's latest release
plus a release index; we surface the bits an LLM typically wants
(version, summary, project page, recent releases) and drop the
heavyweight ``releases`` map.
"""

from __future__ import annotations

import logging

import httpx
from calfkit.models import ToolContext
from calfkit.nodes import ToolNodeDef, agent_tool

logger = logging.getLogger(__name__)

_PYPI_URL = "https://pypi.org/pypi/{package}/json"
_TIMEOUT_SECONDS = 10.0
_MAX_RECENT_RELEASES = 5

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy-init the module-global ``httpx.AsyncClient``.

    Constructed on first call so import stays cheap and tests can swap
    the singleton via ``monkeypatch.setattr(pypi, "_get_client", ...)``.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    return _client


async def pypi_info(ctx: ToolContext, package_name: str) -> str:
    """Fetch package metadata from PyPI's JSON API.

    Use this to answer questions like "what's the latest version of X?",
    "what does package Y actually do?", or "is this package still
    maintained?". For arbitrary web content, prefer ``web_fetch``.

    Args:
        package_name: The PyPI distribution name (e.g. ``"requests"``,
            ``"pydantic-ai"``). Case-insensitive; PyPI normalizes.

    Returns:
        A short markdown summary with the latest version, summary,
        homepage, and the last few releases. On a missing package
        (HTTP 404) or network failure, an ``"error: ..."`` string the
        calling LLM can adapt to.
    """
    _ = ctx
    url = _PYPI_URL.format(package=package_name)
    try:
        resp = await _get_client().get(url)
    except httpx.HTTPError as e:
        logger.warning("pypi_info network failure package=%r: %s", package_name, e)
        return f"error: pypi_info failed to reach pypi.org for {package_name!r}: {e}"
    if resp.status_code == 404:
        return f"error: pypi has no package named {package_name!r}"
    if resp.status_code >= 400:
        return (
            f"error: pypi returned HTTP {resp.status_code} for "
            f"{package_name!r}: {resp.text[:200]}"
        )

    data = resp.json()
    info = data.get("info") or {}
    name = info.get("name") or package_name
    version = info.get("version") or "(unknown)"
    summary = info.get("summary") or "(no summary)"
    home_page = info.get("home_page") or info.get("project_url") or ""
    releases = sorted((data.get("releases") or {}).keys(), reverse=True)
    recent = ", ".join(releases[:_MAX_RECENT_RELEASES]) or "(none)"

    lines = [
        f"**{name}** — {version}",
        "",
        summary,
        "",
        f"Home: {home_page}" if home_page else "",
        f"Recent releases: {recent}",
    ]
    return "\n".join(line for line in lines if line is not None)
```

### Step 2 — Decorate with `agent_tool`

At the bottom of the same module, wrap the bare function into a
`ToolNodeDef` by calling `agent_tool` directly. Keep the bare `async
def` importable under its real name — tests should call it without
going through calfkit's dispatch.

```python
# At the bottom of src/calfkit_organization/tools/builtin/pypi.py

# Call ``agent_tool`` as a function (not as the ``@agent_tool``
# decorator form on the def) so ``pypi_info`` stays directly
# importable for unit tests. Every builtin uses this same shape.
pypi_info_tool: ToolNodeDef = agent_tool(pypi_info)
```

The schema name the LLM sees comes from the wrapped function's
`__name__` (so `pypi_info`, not `pypi_info_tool`). Subscribe/publish
topics derive from the same name: `tool.pypi_info.input` and
`tool.pypi_info.output`. The function's docstring becomes the tool's
description in the LLM-facing tool advertisement — write the docstring
for the LLM, not just for humans.

### Step 3 — Drop the file, restart, declare

Save the file at `src/calfkit_organization/tools/builtin/pypi.py`. No
edits to `tools/__init__.py`, no registry insertions, no entry points.

Restart `calfkit-tools`. The discovery loader walks the package at
import time and logs each registration:

```
INFO calfkit_organization.tools.discovery: registered builtin
     tool=pypi_info from=calfkit_organization.tools.builtin.pypi:pypi_info_tool
```

Add the tool to an agent by listing it in the agent's `.md` frontmatter
`tools:` array — see `agents/example.md.template`:

```yaml
---
name: librarian
slash: /librarian
display_name: Librarian
description: Looks up Python packages and library docs.
tools:
  - pypi_info
  - web_fetch
---
You are the librarian. When users ask about a Python package, call
`pypi_info` first; follow up with `web_fetch` on the home page if you
need more detail.
```

Agent boot resolves each name against `TOOL_REGISTRY` (populated by
discovery). Unknown names fail fast with a "known tools: ..." error.

## 3. The `@agent_tool` contract

### Signature

```python
async def name(ctx: ToolContext, **kwargs) -> str: ...
```

- **First parameter is always `ToolContext`**. Calfkit injects it
  before dispatch and hides it from the LLM — the LLM never sees `ctx`
  in the tool's argument schema. Name it `ctx` by convention.
- **Other parameters are the tool arguments**. Their names, types, and
  default values become the schema the LLM is advertised. Stick to
  simple types (`str`, `int`, `float`, `bool`, `list[...]`,
  `dict[str, ...]`, `Optional[...]`); pydantic-ai builds the schema from
  the annotations and exotic types are likely to fail at schema-build
  or call time.
- **The docstring is the description**. The first paragraph is what the
  LLM sees as the tool's purpose. Use the `Args:` block to document each
  argument — pydantic-ai parses it. Write for the LLM: explain *when* to
  use the tool, not just *what* it does. Compare `private_chat`'s
  docstring for an example of explicit "when not to use" and "writing
  good content" guidance.
- **The function must be `async`**. Calfkit's tool runner is
  asyncio-only; a sync function will not be dispatchable. CPU-bound
  work belongs behind `asyncio.to_thread` or a process pool.

### Return type

Always `str`. Long output is fine — calfcord's outbox handles Discord
truncation downstream (the bridge chunk-splits and the LLM consumes the
full text regardless). Empty `""` is legal but discouraged; an LLM
seeing an empty tool result often loops. Return a one-line "no results"
message instead.

Other return types (dict, dataclass, etc.) are not part of the contract
in v1. Format what you have as a string with whatever shape your tool's
caller needs — markdown, JSON, plain text. The LLM is a flexible reader.

### `ToolContext` access

Two fields matter to tool authors:

- **`ctx.agent_name: str | None`** — the calling agent's id, populated
  by calfkit from the inbound `x-calf-emitter` Kafka header. `None`
  means dispatch was bypassed; that's an infra bug (see below).
  `private_chat` uses this to look up the caller in the phonebook.
- **`ctx.deps.provided_deps: dict[str, Any]`** — the per-call deps the
  bridge populates on every publish. The two keys in v1 are
  `"discord"` (the originating `WireMessage` dict) and `"phonebook"`
  (the canonical roster of registered agents). Most tools don't need
  either; `private_chat` is the only builtin that does.

`ctx.deps.correlation_id: str` is also available — useful in error
log lines so operators can grep across the Kafka audit trail.

## 4. Error handling convention

Two distinct failure modes, two distinct dispositions. This is the
single most important convention to internalize before shipping a tool.

### LLM-recoverable problems

Bad argument, missing target, malformed URL, transient network
failure, file not found — anything the LLM can plausibly adapt to.
**Return a string that starts with `"error: "`** so the calling LLM
can read the discriminator without parsing structure:

```python
if package_name.startswith("-"):
    return f"error: package_name {package_name!r} is not a valid PyPI distribution name"
```

The `"error: "` prefix is the convention every builtin tool uses (see
`tools/builtin/_observation.py` for the constant and rationale). When
your tool wraps an openhands `Observation`, use the shared helper
`flatten_observation_text(obs)` from `tools/builtin/_observation.py`
— it inspects `obs.is_error` and applies the prefix automatically:

```python
from calfkit_organization.tools.builtin._observation import flatten_observation_text

obs = _get_executor()(action)
return flatten_observation_text(obs)
```

### Infrastructure bugs

Missing required env var, malformed deps from the bridge, calfkit
dispatch bypassed, the tool was invoked without a setup phase running
— anything an operator (not the LLM) needs to see. **Raise
`RuntimeError` with full context**:

```python
if api_key is None:
    raise RuntimeError(
        "pypi_info requires the PYPI_TOKEN env var; "
        "the calfkit-tools deployment must set it"
    )
```

`private_chat.py` is the canonical reference: its `_raise_infra` helper
logs caller / target / correlation_id at ERROR level and raises
`RuntimeError` with the chained cause. Funneling every infra-bug path
through a single helper keeps operator triage uniform — read
`src/calfkit_organization/tools/builtin/private_chat.py` lines around
the `_raise_infra` definition and copy the shape.

The boundary rule: if a competent human operator could fix the
condition by editing config / re-deploying / restoring a service, it's
infra and you raise. If a competent LLM could fix the condition by
re-issuing the call with different arguments, it's recoverable and you
return `"error: ..."`. When in doubt, prefer to raise — a noisy
`RuntimeError` is easier to triage than a silent LLM loop.

## 5. Discovery rules

The auto-loader at `src/calfkit_organization/tools/discovery.py`
enforces these rules at import time:

- **Drop a `.py` file in `tools/builtin/`.** No edits to
  `tools/__init__.py`, no entries in a manifest, no decorator-side
  registration call. The loader walks the package with `pkgutil.iter_modules`
  on every boot.
- **Modules whose name starts with `_` are skipped.** Pytest-style
  convention. Use this for shared helpers — `_observation.py` holds
  `flatten_observation_text` and is not scanned for tools. Keep
  helper-only modules underscore-prefixed so a future contributor
  doesn't see them in the registry and assume they're tools.
- **Every module-level attribute that is a `ToolNodeDef` is
  registered.** The registry key is `value.tool_schema.name` — which
  is the wrapped function's `__name__` because `agent_tool` derives it
  there. So `pypi_info_tool: ToolNodeDef = agent_tool(pypi_info)`
  registers under the name `pypi_info`. The attribute name
  (`pypi_info_tool`) is irrelevant to discovery; pick a convention and
  stick with it (existing builtins use `<name>_tool`).
- **A single module may export multiple tools.** `fs.py` does this for
  `read_file`, `write_file`, and `edit_file` — they share a lazy
  executor singleton, so co-locating them is the right shape. Each
  gets its own `<name>_tool: ToolNodeDef = agent_tool(<name>)` line.
- **Re-exports are deduped by `id()`.** If module B does `from .a
  import foo_tool`, the loader sees the same `ToolNodeDef` instance
  twice and registers it once. You do not need to guard against this.
- **Name collisions raise `ValueError` at boot.** Two distinct
  `ToolNodeDef` instances advertising the same schema name is a hard
  failure with both `module:attribute` paths in the message. Pick a
  unique function name; alphabetical module + attribute ordering
  determines which side of the collision logs as "existing".
- **`ImportError` in a tool module aborts boot.** A broken tool module
  is a hard config error, not a condition to skip — see the discovery
  docstring for the rationale. Fix the import, restart.

## 6. Security model

**Tools run inside the `calfkit-tools` process with that process's full
host access.** No per-tool sandbox, no syscall filter, no per-call
permission grant. Treat the `calfkit-tools` process as a trusted
collaborator with the same blast radius as Claude Code running on a
laptop — it can read every file the process user can read, run every
binary on `$PATH`, open every socket. The shipped builtins
(`shell`, `read_file`, `write_file`, `edit_file`) lean directly into
this; new tools inherit it whether they want to or not.

The bridge's "trusted shared workspace" model (documented in the
README) is the org-level disposition. Your tool's responsibility within
that model is the call-level disposition:

- **Validate every LLM-supplied argument before using it.** The LLM is
  an untrusted string source. Type annotations give you JSON-encoded
  shape; they give you nothing about content. Treat every `str`
  argument as if it could be a prompt-injection payload or an attacker-
  crafted path.
- **Never pass an LLM argument directly to `subprocess.run` with
  `shell=True`.** Even with `shell=False`, validate the argv. The
  shipped `shell` tool exists precisely so that no other tool needs
  to invent its own subprocess pipeline — if you find yourself
  reaching for `subprocess`, ask first whether the workflow belongs as
  a shell command the LLM composes.
- **Validate URLs before fetching.** Reject `file://` / `gopher://` /
  unknown schemes if your tool only intends to fetch HTTP. Validate
  hostnames if your tool only intends to talk to a specific upstream.
  PyPI's API (the example above) is safe because the URL template is
  fixed and only the path segment is templated — the `package_name`
  argument cannot redirect the request to a different host.
- **Validate filesystem paths.** Absolute-path arguments are part of
  the trusted-workspace contract for the `fs` tools, but if your tool
  only operates on a fixed subdirectory, reject paths that escape it
  via `..` or absolute prefixes. `_resolve_path` in
  `src/calfkit_organization/tools/builtin/fs.py` shows the v1 baseline
  (no escape protection — by design); a more restrictive tool should
  do better.
- **Validate SQL / shell / templated strings.** Anything that's
  forwarded into a downstream interpreter needs the same hygiene you'd
  apply on a public web endpoint.

The threat model is not "malicious LLM"; it's "confused LLM running on
behalf of a user who can sometimes inject content the agent doesn't
expect". A reply containing `; rm -rf ~` in a Discord message that
flows into a tool call is realistic. Validate at the tool boundary —
treat every argument as untrusted string input, even when its type
annotation says otherwise.

## 7. Testing pattern

Tests live next to the source: a tool at
`src/calfkit_organization/tools/builtin/pypi.py` gets tests at
`tests/tools/builtin/test_pypi.py`. Two canonical references:

- **`tests/tools/builtin/test_fs.py`** — exercises the real
  `FileEditorExecutor` against `tmp_path`. No mocking. Use this shape
  when your tool wraps an executor that's cheap to construct against a
  temp dir.
- **`tests/tools/builtin/test_web.py`** — `monkeypatch.setattr` to
  replace the lazy-init singleton with a `MagicMock`. Use this shape
  when your tool talks to the network or to an upstream that's
  expensive / non-deterministic to instantiate.

Build a `ToolContext` with a minimal helper at the top of every test
module (the builtin tests duplicate this rather than importing — the
helper is three lines and the duplication keeps each test file
self-contained):

```python
# tests/tools/builtin/test_pypi.py
from calfkit.models import ToolContext
from calfkit.models.session_context import Deps


def _ctx(agent: str = "alice") -> ToolContext:
    return ToolContext(
        deps=Deps(correlation_id="c", provided_deps={}),
        agent_name=agent,
    )
```

For the `pypi_info` example, mock the lazy client singleton:

```python
# tests/tools/builtin/test_pypi.py
from unittest.mock import MagicMock

import httpx
import pytest

from calfkit_organization.tools.builtin import pypi


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    pypi._client = None
    yield
    pypi._client = None


async def test_returns_summary_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "info": {"name": "requests", "version": "2.32.0", "summary": "HTTP for humans."},
        "releases": {"2.32.0": [], "2.31.0": [], "2.30.0": []},
    }

    async def fake_get(url: str) -> MagicMock:
        return resp

    fake.get = fake_get
    monkeypatch.setattr(pypi, "_get_client", lambda: fake)

    result = await pypi.pypi_info(_ctx(), "requests")
    assert "requests" in result
    assert "2.32.0" in result


async def test_404_returns_recoverable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock()

    async def fake_get(url: str) -> MagicMock:
        return MagicMock(status_code=404)

    fake.get = fake_get
    monkeypatch.setattr(pypi, "_get_client", lambda: fake)

    result = await pypi.pypi_info(_ctx(), "definitely-not-a-real-package")
    assert result.startswith("error: ")


async def test_network_failure_returns_recoverable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()

    async def fake_get(url: str) -> None:
        raise httpx.ConnectError("dns lookup failed")

    fake.get = fake_get
    monkeypatch.setattr(pypi, "_get_client", lambda: fake)

    result = await pypi.pypi_info(_ctx(), "requests")
    assert result.startswith("error: ")
    assert "dns lookup failed" in result
```

Three notes:

- `asyncio_mode = "auto"` is set in `pyproject.toml`, so test functions
  can be `async def` without an `@pytest.mark.asyncio` decorator.
- Tests should not depend on a running Kafka broker. Call the bare
  async function directly — the `@agent_tool`-wrapped `ToolNodeDef` is
  for production dispatch, not for tests.
- A fixture that resets the lazy-init singleton (`pypi._client = None`)
  before and after each test keeps cross-test bleed-through from
  silently sharing a `MagicMock`.

## 8. Lazy-init pattern for heavy resources

Don't construct HTTP clients, DB pools, subprocess sessions, or any
other heavyweight resource at module import time. Use a module-global
`_get_thing()` helper that idempotently constructs the singleton on
first call:

```python
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy-init the module-global httpx client."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    return _client


async def pypi_info(ctx: ToolContext, package_name: str) -> str:
    resp = await _get_client().get(...)
    ...
```

Three reasons this pattern matters:

1. **Import stays cheap.** Tool discovery imports every module in
   `tools/builtin/` at boot. A `boto3.client("s3")` at import time
   would block boot on network for every tool that touches S3, even
   for agents that don't declare the tool. With lazy-init, the
   construction cost only happens in processes that actually invoke
   the tool.
2. **Tests can substitute via `monkeypatch.setattr`.** The pattern
   `monkeypatch.setattr(pypi, "_get_client", lambda: fake)` works
   because the consumer reads the function name through the module —
   if the singleton were created at import, tests would have to patch
   the cached instance plus every callsite that captured it. See
   `tests/tools/builtin/test_web.py` for the canonical shape.
3. **Resource setup errors surface at the tool's first call** — where
   the operator sees them in the log line for that invocation — rather
   than at process boot, where they break unrelated agents.

`tools/builtin/fs.py`, `shell.py`, and `web.py` all use this pattern.
Copy the shape exactly.

## 9. Future — third-party plugins (deferred)

Today, calfcord only auto-discovers tools that live inside the repo at
`src/calfkit_organization/tools/builtin/`. There is no entry-point
loading, no `[project.entry-points."calfcord.tools"]` discovery, no
support for pip-installable third-party tool packages. If you want to
ship a closed-source or otherwise externally-distributed tool without
forking calfcord, file an issue describing the use case. The discovery
loader is small (see `src/calfkit_organization/tools/discovery.py`)
and entry-point loading is an additive change — we just haven't
needed it yet.
