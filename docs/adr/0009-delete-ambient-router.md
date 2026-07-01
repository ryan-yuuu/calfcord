# Delete the ambient router; ambient messages go unanswered

**Status:** accepted

The ambient-router subsystem (a bespoke routing/history/fan-out process, ~3.5k
LOC) and the `/task` command are deleted. A message that is neither an
`@<agent>` mention nor an operator slash gets **no** automatic agent — there is
no auto-selection.

## Why

The router existed to pick an addressee and fan history out to agents. calfkit's
caller surface plus explicit `@mention` addressing make that layer redundant,
and silent auto-selection was low-value and surprising (users couldn't predict
which agent would answer). Explicit addressing is clearer and cheaper.

## Consequences

Users must `@mention` an agent (or use a slash); ambient channel chatter is
ignored. A distinct process type (`router`) is removed from the deploy topology.
