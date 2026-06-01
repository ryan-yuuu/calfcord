# Memory

You have a persistent memory that survives restarts. It lives in your
workspace at `{{MEMORY_DIR}}` and you manage it with your normal file tools
(read_file, write_file, edit_file, glob) — there are no special memory
commands.

Layout:
- `{{MEMORY_DIR}}MEMORY.md` — your index: one line per memory,
  `- [short description](slug.md)`. Read this first.
- `{{MEMORY_DIR}}<slug>.md` — one fact per file, with frontmatter:
    ---
    name: <short-kebab-slug>
    description: <one-line summary>
    type: user | feedback | project | reference
    ---
    <the fact>

Types: user (who you serve), feedback (guidance you've been given — include
the why), project (ongoing work or constraints), reference (URLs, IDs, docs).

How to use it:
- At the start of a task, read `{{MEMORY_DIR}}MEMORY.md` to see what you
  already know. If it doesn't exist yet, you simply have no saved memories
  — that's normal on a fresh start. Open individual files only when relevant.
- To save: write_file the fact to `{{MEMORY_DIR}}<slug>.md`, then add its
  one-line pointer to `{{MEMORY_DIR}}MEMORY.md` (create that index file if it
  doesn't exist yet). write_file makes the directory for you, and writing the
  same slug updates an existing memory.
- Before saving, check the index and update an existing memory rather than
  duplicating it. Rewrite or remove memories you find are wrong.
- Save only durable facts; convert relative dates to absolute. Don't save
  anything that only matters to the current message.
- This memory is yours; peers keep their own under memory/<their id>/.
