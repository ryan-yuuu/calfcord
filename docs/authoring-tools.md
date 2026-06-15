# Authoring a calfcord Tool

How to add or change a tool that calfcord agents can invoke. The tool
surface is an **explicit, composed list** — there is no auto-discovery —
so where you make a change depends on *which kind* of tool it is. This
guide covers both kinds, then the `@agent_tool` contract every tool
obeys. For the architectural background and the rationale behind explicit
composition, see `src/calfcord/tools/__init__.py` and
`docs/adr/0005-adopt-calfkit-tools-explicit-composition.md`.

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
`@agent_tool` from `calfkit.nodes` that returns `str`**, wrapped into a
`ToolNodeDef`.

The surface is built by **composition, not discovery**. The complete
list of tools the deployment can host is the explicit `ALL_TOOLS` tuple
in `src/calfcord/tools/__init__.py`:

```python
ALL_TOOLS: tuple[ToolNodeDef, ...] = (
    terminal, process, read_file, write_file, patch, search_files,
    todo, execute_code, web_search, web_extract,  # vendored (calfkit-tools)
    web_fetch,                                     # vendored (separate subpackage)
    private_chat_tool,                             # first-party
)
```

Most of these are **vendored** from the `calfkit-tools` package (the
hermes terminal / process / file / search / todo / code-execution / web
nodes, plus an SSRF-safe `web_fetch`). Only `private_chat` is
**first-party** — it is agent-to-agent A2A over Discord and cannot be
vendored. `apply_deploy_filters` turns `ALL_TOOLS` into the name-keyed
`TOOL_REGISTRY` at boot, applying the operator-facing
`CALFCORD_TOOLS_INCLUDE` / `CALFCORD_TOOLS_ALIAS` transforms (see
[`distributed-deployment.md`](./distributed-deployment.md)).

This list is the **security boundary**: `terminal` and `execute_code`
run arbitrary code on the tools host, so what agents can reach is a
local, reviewable decision rather than an artifact of which package
version happens to be installed. That is why the hermes nodes are
imported *by name* and never spread from the package's published set,
and why entry-point plugin discovery was deliberately rejected (§ 9).

## 2. Which kind of tool are you adding?

There is no single "add a tool" workflow anymore — the path forks on
what the tool is.

### 2.1 Changing a common (vendored) tool — contribute upstream

The terminal, file, search, todo, code-execution, and web tools live in
the **`calfkit-tools` package** (the `calf-ai/calfkit-peripherals`
repo), not in calfcord. To add a new general-purpose tool to that set,
or to change the behaviour of an existing one, **contribute it
upstream**. Once the package publishes the node, adopt it in calfcord by:

1. Bumping the `calfkit-tools` dependency (`uv add calfkit-tools@<ver>`).
2. Importing the new node by name in `src/calfcord/tools/__init__.py`
   and adding it to the `ALL_TOOLS` tuple.
3. Updating the `EXPECTED_TOOLS` set in `tests/tools/test_registry.py`.

That third step matters: a **drift-guard test** in `test_registry.py`
fails CI whenever the package publishes a hermes tool that calfcord
neither exposes in `ALL_TOOLS` nor lists in `_EXCLUDED_HERMES_NODES`. So
a dependency bump that adds a new upstream tool forces a deliberate
adopt-or-exclude decision — nothing reaches agents without a reviewable
edit.

### 2.2 Adding a first-party calfcord tool

Some tools cannot be vendored because they need calfcord's own internals
— `private_chat`, for instance, needs the A2A client and the Discord
guild plumbing. Those live as modules under `src/calfcord/tools/`.

The workflow is two explicit steps:

**Step 1 — Write the module under `src/calfcord/tools/`** with a
`ToolNodeDef` at module scope. A worked example, a `pypi_info` tool that
fetches package metadata from PyPI's public JSON API:

```python
# src/calfcord/tools/pypi.py
"""``pypi_info`` — look up a package's metadata on PyPI.

Thin wrapper around the public ``https://pypi.org/pypi/<name>/json``
endpoint. No API key. We surface the bits an LLM typically wants
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
    """Lazy-init the module-global ``httpx.AsyncClient`` (see § 8)."""
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
        A short markdown summary. On a missing package (HTTP 404) or
        network failure, an ``"error: ..."`` string the calling LLM can
        adapt to.
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

    info = (resp.json().get("info")) or {}
    name = info.get("name") or package_name
    version = info.get("version") or "(unknown)"
    summary = info.get("summary") or "(no summary)"
    return f"**{name}** — {version}\n\n{summary}"


# Call ``agent_tool`` as a function (not as the ``@agent_tool`` decorator
# on the def) so ``pypi_info`` stays directly importable for unit tests.
pypi_info_tool: ToolNodeDef = agent_tool(pypi_info)
```

The schema name the LLM sees comes from the wrapped function's
`__name__` (so `pypi_info`, not `pypi_info_tool`). Subscribe/publish
topics derive from the same name: `tool.pypi_info.input` and
`tool.pypi_info.output`. The function's docstring becomes the tool's
description in the LLM-facing advertisement — write the docstring for
the LLM, not just for humans.

**Step 2 — Add it to `ALL_TOOLS`.** This is the step that did not exist
under the old auto-discovery model and is mandatory now:

```python
# src/calfcord/tools/__init__.py
from calfcord.tools.pypi import pypi_info_tool

ALL_TOOLS: tuple[ToolNodeDef, ...] = (
    terminal, process, read_file, write_file, patch, search_files,
    todo, execute_code, web_search, web_extract,
    web_fetch,
    private_chat_tool,
    pypi_info_tool,   # ← your new first-party tool
)
```

Restart `calfkit-tools` (`calfcord tools stop && calfcord tools start`,
or `uv run calfkit-tools` in dev). The runner logs each registration:

```
INFO calfcord.tools: registered tool=pypi_info
```

Agents opt into the tool by listing its name in the agent's `.md`
frontmatter `tools:` array (or via `calfcord agent tools <name>`). Agent
boot resolves each name against `TOOL_REGISTRY`; unknown names fail fast
with a "known tools: ..." error.

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
  use the tool, not just *what* it does. `private_chat`'s docstring is
  the canonical example of explicit "when not to use" and "writing good
  content" guidance.
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

The fields that matter to tool authors:

- **`ctx.agent_name: str | None`** — the calling agent's id, populated
  by calfkit from the inbound `x-calf-emitter` Kafka header. It is
  unspoofable (the LLM cannot set it). `None` means dispatch was
  bypassed; that's an infra bug. `private_chat` uses it to look up the
  caller in the phonebook, and the vendored stateful tools use it to key
  per-agent state (§ 5).
- **`ctx.deps: dict[str, Any]`** — the per-call deps the bridge
  populates on every publish (a bare dict; read keys as
  `ctx.deps["discord"]`). The two it always sets are `"discord"` (the
  originating `WireMessage` dict) and `"phonebook"` (the canonical
  roster of registered agents); memory-enabled deployments also seed a
  memory-prompt template, and `private_chat` forwards `"caller_agent_id"`
  on A2A hops. Most tools don't need any of these; `private_chat` is the
  only first-party tool that does.
- **`ctx.resources: dict[str, Any]`** — node- and worker-scoped
  lifecycle resources (calfkit 0.6.0+). This is how `private_chat`
  reaches its Discord connection and the process-wide calfkit `Client`
  without module globals (§ 8).
- **`ctx.correlation_id: str`** — the Kafka audit-trail id (it mirrors
  `run_id`). Put it in error log lines so operators can grep across the
  trail.

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

The `"error: "` prefix is the convention every tool uses.

### Infrastructure bugs

Missing required env var, malformed deps from the bridge, calfkit
dispatch bypassed, a required resource not built — anything an operator
(not the LLM) needs to see. **Raise `RuntimeError` with full context**:

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
through a single helper keeps operator triage uniform — read the
`_raise_infra` definition in `src/calfcord/tools/private_chat.py` and
copy the shape.

The boundary rule: if a competent human operator could fix the
condition by editing config / re-deploying / restoring a service, it's
infra and you raise. If a competent LLM could fix the condition by
re-issuing the call with different arguments, it's recoverable and you
return `"error: ..."`. When in doubt, prefer to raise — a noisy
`RuntimeError` is easier to triage than a silent LLM loop.

## 5. Multi-tenancy: per-agent state isolation

The `calfkit-tools` process is shared by every agent, so any tool that
holds **per-call session state** must key it by the calling agent so one
agent cannot read or disturb another's.

The vendored stateful nodes (`terminal`, `process`, the in-flight file
edits, `execute_code`, `todo`) already do this. They derive a session
key:

```python
session_key = f"{agent_name}:{deps.get('session_id', 'default')}"
```

where `agent_name` comes from `ctx.agent_name` (the unspoofable
`x-calf-emitter` header). A call with no `agent_name` **fails closed** —
the node raises rather than merging the caller into a shared bucket. So
each agent gets its own shell session, working directory,
files-in-flight, and todo list out of the box, with no cross-agent leak.
This is verified end-to-end in `tests/tools/test_multitenancy.py`
against the *composed registry* node, so a wiring change that dropped
isolation fails CI.

Scope is **agent-lifetime** by default: `session_id` is left unset
(calfcord wires no `deps["session_id"]`), so an agent's tool state
persists across all of its turns and resets only on a tools-process
restart. Finer per-conversation scope (one session per Discord thread,
say) is a documented future option — it would wire a thread/channel id
into `deps["session_id"]` — and is not enabled today.

If you write a first-party tool that holds session state, follow the
same pattern: read `ctx.agent_name`, fail closed when it is `None`, and
key your state by the session key. A purely stateless tool (like
`pypi_info`) needs none of this.

A practical consequence covered in [`security.md`](./security.md) § 1.1:
because this state is in-memory, a stateful tool is correct at **one
tools-process replica**. Pin stateful tools to a single host with
`CALFCORD_TOOLS_INCLUDE` rather than running two replicas on the same
`tool.<name>.input` topic.

## 6. Security model

**Tools run inside the `calfkit-tools` process with that process's full
host access.** No per-tool sandbox, no syscall filter, no per-call
permission grant. Treat the `calfkit-tools` process as a trusted
collaborator with the same blast radius as Claude Code running on a
laptop — it can read every file the process user can read, run every
binary on `$PATH`, open every socket. The vendored `terminal` and
`execute_code` tools lean directly into this — `terminal` runs arbitrary
shell commands and `execute_code` runs arbitrary Python — and new tools
inherit the same trust whether they want it or not.

The org-level "trusted shared workspace" disposition is documented in
[`security.md`](./security.md). Your tool's responsibility within that
model is the call-level disposition:

- **Validate every LLM-supplied argument before using it.** The LLM is
  an untrusted string source. Type annotations give you JSON-encoded
  shape; they give you nothing about content. Treat every `str`
  argument as if it could be a prompt-injection payload or an
  attacker-crafted path.
- **Never pass an LLM argument directly to `subprocess.run` with
  `shell=True`.** Even with `shell=False`, validate the argv. The
  `terminal` and `execute_code` tools exist precisely so that no other
  tool needs to invent its own subprocess/eval pipeline — if you find
  yourself reaching for `subprocess`, ask first whether the workflow
  belongs as a command the LLM composes through `terminal`.
- **Validate URLs before fetching.** Reject `file://` / `gopher://` /
  unknown schemes if your tool only intends to fetch HTTP. Validate
  hostnames if your tool only intends to talk to a specific upstream.
  The `pypi_info` example above is safe because the URL template is
  fixed and only the path segment is templated — `package_name` cannot
  redirect the request to a different host.
- **Validate filesystem paths.** If your tool only operates on a fixed
  subdirectory, reject paths that escape it via `..` or absolute
  prefixes. (The vendored file tools intentionally do not bound the
  workspace — that's the trusted-workspace contract; a more restrictive
  first-party tool should do better.)
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

Tests live under `tests/tools/` — one module per tool, e.g. a tool at
`src/calfcord/tools/pypi.py` gets tests at `tests/tools/test_pypi.py`.
Canonical references already in the tree:

- **`tests/tools/test_private_chat.py`** — exercises the first-party
  `private_chat` tool by calling its bare async function with a
  hand-built `ToolContext`, mocking the Discord/A2A resources. Use this
  shape when your tool reads `ctx` fields or resources.
- **`tests/tools/test_multitenancy.py`** — drives the *composed registry*
  node's tool body to prove per-agent isolation. Use this shape when
  your tool holds session state (§ 5).
- **`tests/tools/test_registry.py`** — the surface drift-guard. Update
  its `EXPECTED_TOOLS` set when you add or drop a tool.

Build a `ToolContext` with a minimal helper at the top of the test
module:

```python
# tests/tools/test_pypi.py
from calfkit.models import ToolContext


def _ctx(agent: str = "alice") -> ToolContext:
    return ToolContext(
        deps={},
        run_id="c",        # exposed to the tool as ctx.correlation_id
        agent_name=agent,
    )
```

For the `pypi_info` example, mock the lazy client singleton:

```python
# tests/tools/test_pypi.py
from unittest.mock import MagicMock

import httpx
import pytest

from calfcord.tools import pypi


@pytest.fixture(autouse=True)
def _reset_singleton():
    pypi._client = None
    yield
    pypi._client = None


async def test_returns_summary_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "info": {"name": "requests", "version": "2.32.0", "summary": "HTTP for humans."},
    }

    async def fake_get(url: str) -> MagicMock:
        return resp

    fake = MagicMock()
    fake.get = fake_get
    monkeypatch.setattr(pypi, "_get_client", lambda: fake)

    result = await pypi.pypi_info(_ctx(), "requests")
    assert "requests" in result and "2.32.0" in result


async def test_404_returns_recoverable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(url: str) -> MagicMock:
        return MagicMock(status_code=404)

    fake = MagicMock()
    fake.get = fake_get
    monkeypatch.setattr(pypi, "_get_client", lambda: fake)

    result = await pypi.pypi_info(_ctx(), "definitely-not-a-real-package")
    assert result.startswith("error: ")
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

## 8. Heavy resources: lazy-init and lifecycle brackets

Don't construct HTTP clients, DB pools, subprocess sessions, or other
heavyweight resources at module import time. Agent processes import the
tool module solely for the `ToolNodeDef` schema and never run the body,
so importing must construct nothing. There are two patterns.

### Module-global lazy-init (simple, no teardown)

For a resource with no meaningful teardown (an `httpx` client, say), use
a module-global `_get_thing()` helper that idempotently constructs the
singleton on first call:

```python
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Lazy-init the module-global httpx client."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    return _client
```

This keeps import cheap, lets tests substitute via
`monkeypatch.setattr(pypi, "_get_client", lambda: fake)`, and surfaces
setup errors at the tool's first call (in that call's log line) rather
than at boot.

### Node-scoped `@resource` brackets (managed lifecycle)

For a resource that must be opened *and closed* — a Discord connection,
a connection pool — use calfkit's node-scoped `@resource` lifecycle
bracket instead. `private_chat` does this: `_a2a_resource` opens its
Discord connection at worker startup **only when the node is hosted**
and closes it at drain, and the tool body reaches it via `ctx.resources`
rather than a module global. This is the right pattern when the resource
has a lifecycle the worker should own; read `_a2a_resource` and
`_resources_from_ctx` in `src/calfcord/tools/private_chat.py` for the
shape.

## 9. Why not entry-point / third-party plugin discovery?

calfcord deliberately does **not** support `importlib.metadata`
entry-point loading or `[project.entry-points."calfcord.tools"]`
discovery, and it does not walk a directory looking for tool modules.
This is a security decision, not a missing feature: the tool surface is
the security boundary (§ 1), and entry-point discovery would let merely
*installing* a package arm a tool that runs arbitrary code on the tools
host. The surface must stay an explicit, code-reviewed list.

If you want to ship a closed-source or externally-distributed tool
without forking calfcord, the supported path is to contribute it to the
`calfkit-tools` package (§ 2.1) so it goes through the same explicit
adoption and drift-guard review as every other vendored tool. If that
doesn't fit your use case, file an issue describing it — but note that
any solution will preserve the explicit-allowlist property; see
`docs/adr/0005-adopt-calfkit-tools-explicit-composition.md` for the full
rationale.
