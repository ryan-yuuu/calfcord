# Multi-Agent Organization on Discord — Design Document

A design for running a personal life or company as an organization of AI agents operating inside a Discord server, coordinating via Calfkit's Kafka-native streaming substrate.

---

## 1. Core concept

The user is the CEO. Agents are employees. Discord is the office.

Each agent has a defined role, a scope of authority, and the ability to participate in any conversation it's summoned to. The user invokes agents with slash commands (`/scheduler`, `/finance`, etc.) and can stack commands to loop multiple agents into the same conversation. Once summoned into a thread, an agent stays subscribed until dismissed — natural conversation continues without slashes. Agents can also message each other directly, and can proactively surface work to the user when something needs attention.

The organization is legible because the org chart *is* the architecture. Adding a capability means hiring an agent (and registering its slash command). Removing one means firing it. There is no menu of tools to learn — there are coworkers to talk to.

---

## 2. Design principles

1. **Topic over identity.** Channels are organized by what you're working on, not by who you're talking to. Agents are summoned into the conversation, not visited in their own room.
2. **Slash to summon, conversation to continue.** Slash commands invoke agents and modify thread membership. Within a thread, no slashes are needed — agents stay subscribed until explicitly dismissed.
3. **Default to visibility.** "Private from other agents" does not mean "private from the user." The user can see everything; restricted channels exist only to keep views focused, not to hide traffic.
4. **Threads are tasks.** A thread scopes a single piece of work and the agents required for it. Memory, context, and approvals all attach to the thread.
5. **Different communication patterns get different channels, not different participants.** Topic work goes in topic channels. Negotiation goes in coordination. Shared state goes in pub/sub. Telemetry goes on the control plane. Proactive agent surfacing goes in `#agent-pings`.
6. **Authority is per-agent, per-action.** Read, draft, send-with-approval, send-autonomously. Configured granularly, audited uniformly.
7. **Everything is a stream.** Every Discord channel maps to a Kafka topic. Calfkit consumes the firehose, agents subscribe to what they need, and replay is free.
8. **Minimum sufficient context.** Each agent loads only the slice of state its role requires. Context-window cost is a first-class design constraint.

---

## 3. Discord topology

### 3.1 Server structure

One Discord server hosts the organization. Channels are organized into four categories based on communication pattern, not by which agent owns them.

```
🏛  ORG (system)
   #announcements         — broadcast from user to all agents
   #standup               — agents post status; user reads at will
   #coordination          — agents negotiate ownership of incoming work
   #control-plane         — telemetry: agent health, costs, reputation
   #facts                 — pub/sub of state changes (user facts, company facts)

📨  USER-FACING
   #agent-pings           — agents post proactive work for the user here
   #chat                  — catch-all for ad-hoc requests; slash-summon agents in-thread

📋  TOPIC CHANNELS (created as needed)
   #trip-planning
   #finances
   #calfkit-launch
   #health
   #spanish
   ... (one channel per persistent topic or project)

🤝  AGENT RELATIONSHIPS
   #a2a-scheduler-finance
   #a2a-devrel-eng
   ... (one channel per agent pair; created on first contact between
        two agents. Primary surface for all agent-to-agent work.)
```

The user is in every channel. Each agent is in `#announcements`, `#coordination`, `#facts`, `#control-plane`, `#agent-pings`, and any thread it has been summoned to. Agents are not bound to any single channel — they're summoned into work via slash commands.

### 3.2 The slash-command model

Slash commands are the universal invocation primitive.

- **`/scheduler <message>`** — summon Scheduler into the current channel (creates a new thread for the work) or the current thread (joins the existing thread).
- **`/scheduler /finance <message>`** — stacked invocation. Both agents are summoned into the same thread. The first-mentioned agent takes lead unless the message explicitly assigns ownership.
- **`/team trip <message>`** — invoke a predefined group (e.g., `trip` = Scheduler + Finance + Concierge). Teams are user-configurable shortcuts for common collaborations.
- **`/leave scheduler`** (inside a thread) — dismiss Scheduler from this thread. Other agents continue.
- **`/handoff scheduler finance`** (inside a thread) — Scheduler explicitly hands the thread off to Finance; Scheduler stays subscribed but Finance takes lead.
- **`/agents`** — list all available agents with descriptions.

Once an agent is in a thread, the user (and any other agents in the thread) can address it with plain messages or by `@agent` mention. No slash needed after invocation.

Slash autocomplete (Discord-native) surfaces the available agents with descriptions, which doubles as onboarding for the roster.

### 3.3 Where things happen

| You want to… | Where it goes |
|---|---|
| Ask an agent for something one-off | `/agent <request>` in `#chat` — creates a thread |
| Work on an ongoing topic (NYC trip, Calfkit launch) | A topic channel; slash-summon agents into threads inside it |
| Loop in another agent mid-conversation | `/otheragent <question>` inside the thread |
| See what agents have surfaced for you proactively | `#agent-pings` |
| Catch up on what agents did today | `#standup` |
| Broadcast something to all agents | `#announcements` |
| Investigate a problem (cost spike, agent misbehaving) | `#control-plane` |

### 3.4 Topic channels

The user creates topic channels as work emerges. Examples:

- **Persistent topics:** `#finances`, `#health`, `#spanish`, `#dating`, `#calfkit-eng`, `#calfkit-gtm`.
- **Projects:** `#yc-application`, `#calfkit-launch`, `#tax-season-2026`. Created when work starts, archived when done.

Topic channels are conversational spaces. Each thread inside them is a specific task, with whichever agents are needed summoned in. The channel name signals what the threads are about; the threads carry the actual work.

The catch-all `#chat` exists for requests that don't belong to any topic. Most ad-hoc work starts there.

### 3.5 The `#agent-pings` channel

This is where agents post **proactive** messages — things you didn't ask for.

- "Your accountant just emailed about the K-1 deadline." (from Inbox)
- "Perf regression on commit abc123, –12% throughput on benchmark X." (from Eng Lead)
- "New arXiv paper on FiLM conditioning for LOB models — relevant to your BINCTABL work." (from Researcher)
- "AWS bill jumped 40% this week. Drilling down." (from Operations)

Format: each ping is a single message with the agent's name, a short headline, and (if you tap into it) a thread underneath for follow-up. If you reply in the thread, the originating agent is automatically subscribed to it; you can summon others with slash if needed.

Why one channel rather than many: volume is moderate (tens of pings per day, not hundreds), and a single sorted-by-time view is easier to triage than N per-agent channels. If volume from any single agent gets noisy, that's a signal to tune its triggering thresholds, not to subdivide.

### 3.6 Relationship channels — the primary agent-to-agent surface

All agent-to-agent work happens in relationship channels. When agent A needs agent B to do something:

- **If `#a2a-a-b` exists:** A posts there, starting a thread for the specific task.
- **If it doesn't yet:** A creates the relationship channel (programmatically, with permissions scoped to A, B, and the user), then posts.

Channel naming is deterministic: `a2a-{x}-{y}` with agent names sorted alphabetically, so any pair has exactly one canonical channel regardless of who initiates contact.

Why channel-per-relationship (vs channel-per-task, vs writing into the other agent's inbox):

- **Persistent shared context.** Two agents that work together regularly accumulate preferences, naming conventions, and prior decisions. These live in one place, not scattered across topic threads.
- **Bounded channel count.** N agents produce at most N·(N−1)/2 relationship channels. For 20 agents that's 190, well within Discord's 500-channel cap. In practice most pairs never form, so the real count is much smaller.
- **No rate-limit pressure.** Channels are created rarely (on first contact between two agents), not per-task.
- **Maps to how humans use Slack/Discord.** A persistent DM between two coworkers, with threads for specific tasks inside it. Since Discord blocks bot-to-bot DMs at the API level, this is the closest legal equivalent.
- **Discoverable.** When debugging "what's Scheduler and Finance been doing together," there is exactly one place to look.

Within each relationship channel, agents use **threads** to scope individual tasks. A thread is the unit of context for one piece of work between the two agents; the parent channel is the unit of context for their ongoing working relationship.

The user has read access to all relationship channels and can drop in to observe, intervene, or correct. The user is not normally in the threads themselves but is in the parent channel.

### 3.7 Permission model

| Channel type | User | Agents |
|---|---|---|
| `#announcements` | Read/write | Read |
| `#standup` | Read | Read/write |
| `#coordination` | Read/write | Read/write |
| `#facts` | Read/write | Read/write (scoped subscriptions; see §6.2) |
| `#control-plane` | Read | Read/write |
| `#agent-pings` | Read/write | Write (any agent); Read (any agent, optional) |
| `#chat` and topic channels | Read/write | Threads gated by slash summons |
| Topic-channel threads | Read/write | Only summoned agents |
| Relationship channels | Read/write | Only the two participants |

Channel permissions are uniform across participants — anyone with access has full read/write. The slash-command layer governs *which agents are in a given thread*, not who can read it.

---

## 4. The agent roster

The starting roster for a solo founder running both personal life and a company. Each agent has: a role description, a slash command, an authority profile, and a set of fact subscriptions.

### 4.1 Personal-life agents

| Agent | Slash | Role | Subscribes to | Key authority |
|---|---|---|---|---|
| **Chief of Staff** | `/cos` | Dispatches incoming work, runs weekly review, escalates blockers | All channels, `#coordination`, `#facts` | Reassign tasks; cannot send external messages |
| **Scheduler** | `/scheduler` | Calendar mechanics, scheduling back-and-forth, prep docs | Calendar APIs, `#facts` | Auto-book internal time; approval for external invites |
| **Inbox** | `/inbox` | Triages email, drafts replies, flags founder-only messages | Gmail, `#facts` | Auto-archive obvious junk; draft only otherwise |
| **Finance** | `/finance` | Bookkeeping, expense categorization, recurring bills, tax prep | Bank/Plaid, receipts in email, `#facts` | Auto-pay recurring under $100; approval above |
| **Health** | `/health` | Workout planning, recovery, nutrition logging | Whoop/Apple Health, `#facts` | Advisory only |
| **Spanish** | `/spanish` | Vocabulary practice, content recommendations, drills | None external | None |
| **Concierge** | `/concierge` | Restaurants, gifts, travel, "find me X" | Web search, OpenTable/Resy, `#facts` | Draft bookings; approval to confirm |
| **CRM** | `/crm` | Tracks relationships, surfaces "you haven't talked to X in N weeks" | Contacts, calendar, message history, `#facts` | Surface-and-suggest only |
| **Librarian** | `/lib` | Reads saved articles/papers, maintains notes, retrieval | Read-later sources, notes store, `#facts` | None external |
| **Coach** | `/coach` | End-of-day check-in, weekly review, pattern surfacing | User journal entries, `#facts` | None external |

### 4.2 Calfkit (company) agents

| Agent | Slash | Role | Subscribes to | Key authority |
|---|---|---|---|---|
| **Chief of Staff** | `/cos` | Same role, company-flavored. Weekly metrics, founder updates | All channels, `#coordination`, `#facts` | Reassign tasks |
| **Eng Lead** | `/eng` | Watches GitHub, triages issues, reviews PRs, monitors CI, runs benchmarks | GitHub webhooks, CI, Sentry, `#facts` | Auto-merge dependabot; approval to push to main |
| **DevRel** | `/devrel` | Watches community, drafts answers, maintains FAQ | Discord, GitHub issues, Reddit/Twitter mentions, `#facts` | Draft replies; auto-post FAQ updates |
| **Sales** | `/sales` | Qualifies inbound, drafts follow-ups, maintains CRM | Email, calendar, CRM, `#facts` | Draft only; approval to send |
| **Marketing** | `/mkt` | Content repurposing, listens to relevant threads, drafts comments | RSS, Reddit/HN/Twitter firehose, `#facts` | Draft only |
| **Researcher** | `/res` | arXiv/SSRN/competitor blog watcher, daily/weekly digest | Paper feeds, competitor sites, `#facts` | Auto-publish digests to `#facts` |
| **Recruiter** | `/recruit` | Talent signal monitoring, outreach drafting | LinkedIn, GitHub, Twitter, `#facts` | Draft only |
| **Ops** | `/ops` | Vendor/subscription audit, contracts queue, cost anomaly alerts | Billing APIs, contracts store, `#facts` | Alert only |
| **IR** | `/ir` | Monthly update drafts from raw data, tracks follow-ups | GitHub, Stripe, CRM, calendar, `#facts` | Draft only |
| **Books** | `/books` | Bookkeeping, runway model, anomaly detection | Stripe, bank, payroll, `#facts` | Auto-categorize; alert on anomaly |

(Slash names overlap between personal and company in cases like `/cos` and `/finance`. If you run both in one server, prefix them: `/p-cos`, `/c-cos`, etc. See §11 for the personal/company isolation question.)

### 4.3 Predefined teams

Teams are user-configured shortcuts that summon a group of agents at once.

| Team slash | Members | Use case |
|---|---|---|
| `/trip` | Scheduler, Finance, Concierge | Travel planning |
| `/launch` | Eng, DevRel, Marketing | Product launches |
| `/quarterly` | Books, Finance, IR | Quarterly close + investor update |
| `/health-week` | Health, Scheduler, Coach | Weekly health review |

The user defines teams in a config file or via `/team-create` (a meta-command).

### 4.4 The Chief of Staff specifically

The Chief of Staff is the most important agent because it prevents the user from having to remember which agent does what. Its loop:

1. Subscribe to `#chat`, `#agent-pings`, `#coordination`, all topic channels, and `#facts`.
2. When the user posts something in `#chat` without a slash invocation, the Chief of Staff suggests routing: "This looks like work for /scheduler and /finance — summon them?" One click and they're in.
3. Run a daily standup post in `#standup` synthesizing what each agent did and what's blocked.
4. Run a weekly review post summarizing org output, costs, and reputation deltas.
5. Resolve coordination deadlocks (when agents in `#coordination` can't agree on ownership).

The Chief of Staff has no external authority. It only routes, summarizes, and escalates. This keeps it cheap and safe.

---

## 5. Communication patterns

Six distinct patterns. Each maps to a specific Discord construct.

### 5.1 Summon (user → agent)

User invokes an agent (or several) into a conversation.

- **In a channel:** `/agent <request>` creates a new thread with that agent as the initial member.
- **In an existing thread:** `/agent <question>` adds the agent to the current thread's membership.
- **Stacked or teamed:** `/agent1 /agent2 <request>` or `/team-name <request>`.
- After summon, conversation in the thread is plain-text. Agents in the thread receive every message; agents not in the thread do not.

### 5.2 Direct work request (agent → agent)

Agent A needs agent B to do something concrete.

- A posts in `#a2a-a-b`, starting a new thread named after the task.
- If the relationship channel doesn't exist yet, A creates it first, then posts.
- The thread holds the entire conversation about that specific task. The parent channel accumulates threads over time — one per task — building up the durable working history between the two agents.

The user can read all of this but doesn't normally need to. If A and B reach a decision the user should know about, they publish a fact to `#facts` rather than expecting the user to scroll the relationship channel.

Coordination questions ("should I take this or should you?") that come up *before* work has clearly landed with one agent go in `#coordination` instead — see §5.3.

### 5.3 Coordination / negotiation

"Who should take this?" or "I'm blocked on X."

- Goes in `#coordination`.
- Visible to all agents and the user.
- Time-boxed: the Chief of Staff resolves anything that hasn't converged within 30s.
- Should be short. If a coordination thread is escalating, the Chief of Staff suggests promoting it to a project topic channel.

### 5.4 Pub/sub of facts

State changes that many agents may care about.

- Goes in `#facts`.
- Append-only event log. Each fact is a structured message: `{type, entity, value, source, timestamp, confidence, scope}`.
- Agents subscribe with filters. Finance subscribes to anything tagged `tax` or `payment`; Scheduler subscribes to anything tagged `availability` or `travel`.
- Facts can supersede earlier facts. Conflicts surface to the user.

Examples: "User's accountant email is X." "Calfkit MRR is $Y as of date Z." "User is traveling May 14–21." "Eng flagged perf regression on commit abc123."

### 5.5 Proactive ping (agent → user)

Agent surfaces something the user didn't ask for.

- Goes in `#agent-pings`.
- One message per ping, headline-style, with the agent's name as sender.
- User can reply in a thread under the ping. The originating agent is auto-subscribed; the user can summon others with slash.

### 5.6 Broadcast & control plane

- **Broadcast:** user → all (`#announcements`) or agent status → user (`#standup`). Read-only for non-senders.
- **Control plane:** operational metadata about the org itself (`#control-plane`). Health, costs, reputation, errors.

---

## 6. Memory and state

Memory matters more under the slash-command model than under per-agent channels, because an agent's history with the user is no longer concentrated in a single channel. It's sharded across every thread the agent has been summoned to. The memory layer has to do real work.

### 6.1 Three tiers

1. **Facts stream (`#facts`).** Source of truth for org-wide state. Append-only. Replayable. Versioned by timestamp.
2. **Agent-local materialized views.** Each agent maintains its own view of the facts it cares about, updated as new facts arrive. This is what goes in the prompt context.
3. **Thread history.** The local context for an active task. When an agent is summoned into a thread (new or existing), it loads the thread history as context.

The user's identity facts (name, address, preferences), the company's identity facts (mission, customers, pricing), and operational state (current projects, open tasks) live as fact streams. Agents do not query a central database; they consume the stream and maintain their own views.

### 6.2 Privacy and scope

Facts are tagged with `scope`. Sensitive facts (medical, financial-sensitive, personal-relational) are scoped to specific agents. Finance does not see therapy notes. Health does not see the Calfkit cap table.

Scoping is enforced at publish time: the publishing agent tags the fact with allowed-consumers. The bus filters on delivery.

### 6.3 Cross-thread memory

Because conversation history is fragmented across threads, the agent must reconstruct relevant past context from facts, not from channel scrollback. Design implications:

- Agents publish a fact whenever they complete meaningful work ("Booked flight UA123 for May 15"). The next time the topic comes up — possibly in a different thread — the agent can recall it from its materialized view.
- For tasks where prior conversation matters (e.g., "schedule another haircut like last time"), the agent stores a compact summary in the fact stream when a thread closes.
- Relationship channels (§3.6) accumulate cross-thread memory between two agents that work together regularly.

### 6.4 Context-window economics

Every agent has a token budget per response. When loading context, agents pull:

1. The current thread.
2. The minimum sufficient slice of their materialized view of facts.
3. Recent `#facts` events tagged for their scope, since their last action.

Agents do not load "everything about the user." If an agent needs information it doesn't have, it asks — by summoning the relevant agent into the thread, or by querying `#facts`.

---

## 7. Authority and approval

### 7.1 Per-action authority profile

Every agent has an authority profile that specifies, for each action type, one of:

- **Read** — observe only.
- **Draft** — produce output, but it requires user approval before sending.
- **Send-with-approval** — send/act, but the user must approve via a Discord reaction within N minutes; otherwise auto-cancel.
- **Send-autonomously** — act without approval, with full audit logging.

Authority is fine-grained. Finance might be `send-autonomously` for recurring bills under $100, `send-with-approval` for one-offs under $1K, and `draft` for anything above $1K.

### 7.2 Approval UX

Approvals happen inline in the thread where the work originated. The agent posts the draft as a Discord message and awaits reactions:

- ✅ approve
- ❌ reject
- 💬 reply with changes

Time-boxed approvals use Discord's message-edit feature to update the countdown live.

For proactive work (an agent acting on its own initiative without user prompting), approvals happen in the `#agent-pings` thread the agent created.

### 7.3 Audit

Every action — message sent, fact published, external API call, thread joined or left — is logged to `#control-plane` and to the Kafka log. The user can replay any agent's history end-to-end. The Chief of Staff produces a weekly audit summary.

### 7.4 Reputation

Agents accumulate reputation based on user corrections. Each time the user overrides a draft, that agent's reputation in that action category decrements. Reputation flows into context: an agent with low reputation in an area errs toward `draft` even if its authority allows `send-autonomously`. Reputation deltas are published to `#control-plane`.

---

## 8. Failure modes and safeguards

### 8.1 Inter-agent loops

A asks B asks C asks A. Token spiral.

- Every request carries a `request_tree_id` and a `depth`. Default max depth is 4.
- Every request tree has a token budget. When exhausted, the Chief of Staff is paged.
- The Chief of Staff watches `#coordination`, relationship channels, and threads for cycle patterns and breaks them.

### 8.2 Hallucinated facts

An agent publishes a fact that is wrong.

- Facts are tagged with `source` and `confidence`.
- Conflicting facts surface to the user automatically.
- The user can mark a fact as wrong; this triggers a reputation decrement for the publisher and a search for downstream actions that depended on it.

### 8.3 Runaway costs

- Per-agent daily token budgets, enforced at the SDK level (Calfkit feature).
- Cost telemetry on `#control-plane`.
- Hard cutoff at 2x budget; soft alert at 1x.

### 8.4 Agent crash / lag

- Each agent's liveness is published to `#control-plane` every 60s.
- If an agent is down, the Chief of Staff routes its work to a fallback (usually itself in degraded mode) or queues it for restart.
- Kafka's at-least-once delivery means no work is lost; agents must be idempotent.

### 8.5 Cross-domain privacy leaks

- Fact scoping enforced at publish time (§6.2).
- Thread membership is the privacy primitive: an agent that hasn't been summoned to a thread does not see it.
- The Chief of Staff is the only agent with read access to all threads, and its outputs go only to the user.

### 8.6 Multi-agent invocation conflicts

When two or more agents are summoned simultaneously (`/scheduler /finance plan trip`):

- The first-mentioned agent takes lead unless the prompt explicitly assigns ownership.
- The lead agent posts an initial plan and explicitly delegates subtasks to the others via @-mention.
- If agents post conflicting suggestions, the Chief of Staff intervenes to mediate.
- If no lead is identifiable (e.g., `/team`), the first agent in the team definition is lead by default.

### 8.7 Stale thread subscriptions

If an agent stays subscribed to a thread indefinitely, threads accumulate ghost members.

- Threads auto-dismiss agents when they auto-archive (7 days of inactivity).
- Agents can self-dismiss when they declare their work in a thread complete ("All booked, leaving the thread.").
- The user can `/leave <agent>` to dismiss manually.

---

## 9. Phased rollout

Building all 20 agents on day one is the path to a broken system. Phasing:

### Phase 1 — Foundations (week 1–2)

Ship: **Chief of Staff, Scheduler, Inbox.**

Goal: get the org primitives right. Discord topology set up, slash commands working with autocomplete, thread membership protocol working (summon, leave, handoff), fact stream working, approval flows on Discord reactions, audit log to `#control-plane`, `#agent-pings` receiving proactive messages. Live in it.

Success criteria: you can `/scheduler` in `#chat` to book a haircut Thursday, the work routes correctly, drafts the email, gets your ✅ via reaction, and lands on your calendar. Inbox posts to `#agent-pings` when a real email needs your attention.

### Phase 2 — Domain expansion (week 3–4)

Add: **Finance, Researcher.**

Goal: exercise cross-domain coordination. Multi-agent summons (`/scheduler /finance`) get real use; the lead-and-delegate protocol gets stress-tested. Fact-scoping surfaces friction.

Success criteria: receipts arriving by email are auto-categorized by Finance without you involved; a new paper relevant to your BINCTABL work appears in `#agent-pings` without you asking; a `/scheduler /finance` thread plans an actual trip end-to-end.

### Phase 3 — Specialists (week 5+)

Add agents based on which parts of your week are still untouched. Each new agent should require *less* framework work than the last. If it doesn't, fix the framework first.

### Phase 4 — Productize

The framework — slash commands, thread membership, fact pub/sub, authority model, memory architecture — becomes the marquee Calfkit example. Other founders fork the org structure and adapt it.

---

## 10. Why this is the right dogfood for Calfkit

Every hard part of building this is what Calfkit sells:

- **Discord as a streaming event source** — Kafka-native consumption of channel messages, reactions, thread events, member events.
- **One long-running consumer per agent** — Calfkit's core abstraction. Each agent is a consumer subscribed to its slash command, its current threads, and the `#facts` filter for its scope.
- **Topic-per-channel + fact pub/sub** — partitioning and consumer groups doing exactly what they were built for.
- **Backpressure** — what happens when 200 emails hit the Inbox agent at once? Solved at the Kafka layer.
- **Replay** — "show me every action Finance took this week" is a topic scan, not a database query.
- **Failure isolation** — Marketing dies, Eng keeps working, the partition gets reassigned.
- **Thread-membership state** — fits cleanly as a Kafka-backed materialized view that any agent can query.

The pitch this writes:

> *Calfkit is the SDK for building agent organizations. Most agent frameworks treat agents as functions you call. Calfkit treats them as employees — long-running, subscribing to streams of work, coordinating with each other, and operating with the authority you grant them. We built our company on it. Here's the org chart.*

---

## 11. Open questions

1. **Personal vs company isolation.** One Discord server or two? One server keeps cognitive overhead low and lets shared agents (Scheduler, Finance) span both domains. Two servers cleanly separate data and reduce leak risk. Leaning toward two servers connected via a shared `#facts` topic with explicit cross-tags — worth testing.
2. **Slash command collisions.** If running personal and company in one server, prefix slashes (`/p-finance` vs `/c-finance`). If separate servers, no collision. The two-server approach is one more reason to favor it.
3. **Mobile UX.** Slash autocomplete works on Discord mobile but is slower than desktop. Worth measuring whether voice-to-slash is needed; for now, plain `@agent` mentions are an acceptable fallback when slash is awkward.
4. **Onboarding new agents.** What's the minimum spec to hire one? Role description, slash command, authority profile, fact-subscription filter, optional starter prompt. Target: under 20 lines of config.
5. **Reputation calibration.** Reputation is easy to design and hard to tune. Initial reputation, decay rate, recovery rate — all empirical.
6. **Chief of Staff redundancy.** Single point of routing failure. Need either redundancy or a clear "degraded mode" where agents pull from the unrouted queue themselves.
7. **`#agent-pings` volume.** If proactive volume gets noisy, do we subdivide (`#pings-eng`, `#pings-personal`) or filter by priority within one channel? Probably the latter; subdividing re-introduces the per-agent-channel problem.
8. **Multi-user version.** Can a team of humans plug into the same org? Probably yes, with per-human authority profiles, but defer.

---

## 12. The first thing to build this week

Stand up the Discord server with the topology in §3.1. Register three slash commands: `/cos`, `/scheduler`, `/inbox`. Implement the thread-membership protocol: a slash command in a channel creates a thread with the invoked agent; a slash command in a thread joins the agent to the thread; `/leave` removes them.

Wire one agent — the Chief of Staff — with the ability to: read all topic channels and `#agent-pings`, post in `#coordination` and `#standup`, and respond when summoned via `/cos`. Confirm Calfkit can consume the Discord event stream (including slash command interactions and thread events), publish to `#facts`, and route messages by thread membership.

That's the smallest possible end-to-end test of the architecture. Once it works — including a proactive ping landing in `#agent-pings` from a stubbed Inbox — every subsequent agent is incremental.