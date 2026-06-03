# Install & run calfcord (no Docker)

Run calfcord directly on a machine with a single command — no Docker, and
nothing to set up first. This is the native alternative to the
[Docker quick start](../README.md#quick-start); if you just want to try calfcord
on one machine, Docker is simpler. Reach for this when you don't want Docker or
you're running calfcord across several machines.

## 1. Install

```bash
curl -fsSL https://raw.githubusercontent.com/ryan-yuuu/calfcord/main/scripts/install.sh | bash
```

You don't need Python, Docker, or git installed first — the installer handles
everything. When it finishes, **restart your shell** (or open a new terminal)
so the `calfcord` command is on your `PATH`.

## 2. Configure

calfcord reads its settings from `~/.calfcord/config/.env`. Open it and fill in
your Discord bot token and an LLM API key (the file is commented; the full list
is in [`configuration.md`](configuration.md)):

```bash
$EDITOR ~/.calfcord/config/.env
```

calfcord's processes talk to each other through a **Kafka broker**, so you need
one running (e.g. a Redpanda or Kafka instance) and must point calfcord at it.
Use the same broker on every machine:

```bash
calfcord self set-broker my-broker-host:9092
```

## 3. Run

Start any calfcord process with `calfcord <name>`:

```bash
calfcord calfkit-bridge     # the Discord gateway
calfcord calfkit-agent      # runs your agents
calfcord calfkit-router     # routes un-mentioned messages
calfcord calfkit-tools      # tools + the agent-to-agent channel
```

On one machine you'll usually run all four. To spread them across machines,
install calfcord on each, point them all at the **same** broker (step 2), and
run only the processes that machine should handle — see
[`distributed-deployment.md`](distributed-deployment.md).

## 4. Keep it up to date

```bash
calfcord self version     # show what's installed
calfcord self status      # check whether a newer version is available
calfcord self update      # upgrade to the latest
calfcord self rollback    # undo the last update
```

## Uninstall

```bash
rm -rf ~/.calfcord
```

Then remove the `# calfcord` line the installer added to your shell profile
(`~/.zshrc`, `~/.bashrc`, or `~/.bash_profile`).

---

**Pin a version:** set `CALFCORD_REF` to a branch or commit before installing,
e.g. `… | CALFCORD_REF=v1.2 bash`. `calfcord self update` then stays on that
ref.

**Want to develop calfcord?** Don't use the installer — clone the repo and use
the standard `uv` workflow so your edits are live. See
[`CONTRIBUTING.md`](../CONTRIBUTING.md).
