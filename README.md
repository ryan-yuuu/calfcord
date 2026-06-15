# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/calfcord/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                           |    Stmts |     Miss |   Branch |   BrPart |   Cover |   Missing |
|----------------------------------------------- | -------: | -------: | -------: | -------: | ------: | --------: |
| src/calfcord/\_atomic.py                       |       19 |        0 |        2 |        0 |    100% |           |
| src/calfcord/\_provisioning.py                 |       25 |        0 |        6 |        0 |    100% |           |
| src/calfcord/\_worker\_runtime.py              |       41 |        0 |       14 |        0 |    100% |           |
| src/calfcord/agents/definition.py              |      107 |        0 |       32 |        0 |    100% |           |
| src/calfcord/agents/factory.py                 |      127 |        2 |       34 |        2 |     98% |  186, 200 |
| src/calfcord/agents/gates.py                   |       28 |        0 |        8 |        0 |    100% |           |
| src/calfcord/agents/identifier.py              |       11 |        0 |        0 |        0 |    100% |           |
| src/calfcord/agents/loader.py                  |       45 |        2 |       20 |        2 |     94% |   44, 135 |
| src/calfcord/agents/md\_writer.py              |       78 |        3 |       14 |        3 |     93% |91-\>94, 162, 272-\>278, 278-\>exit, 285-289 |
| src/calfcord/agents/memory.py                  |       41 |        0 |        8 |        0 |    100% |           |
| src/calfcord/agents/peer\_roster.py            |       18 |        0 |        8 |        0 |    100% |           |
| src/calfcord/agents/phonebook.py               |       42 |        0 |        8 |        0 |    100% |           |
| src/calfcord/agents/routing.py                 |       23 |        1 |        6 |        1 |     93% |       121 |
| src/calfcord/agents/runner.py                  |      203 |       37 |       42 |        2 |     83% |260-261, 343-344, 581-631, 635-646, 650 |
| src/calfcord/agents/state.py                   |       75 |        5 |       10 |        1 |     93% |138-140, 146-\>153, 169-170 |
| src/calfcord/agents/thinking.py                |       25 |        4 |       12 |        2 |     84% |101-105, 114-118 |
| src/calfcord/ambient\_routing.py               |       17 |        0 |        2 |        0 |    100% |           |
| src/calfcord/bridge/egress.py                  |       79 |        3 |       18 |        0 |     97% |   193-199 |
| src/calfcord/bridge/gateway.py                 |      311 |       31 |       56 |        7 |     89% |318-322, 326-327, 393-394, 423, 425, 428, 450-455, 651, 664-685, 739-740, 754, 767, 809, 1009-\>1014, 1056 |
| src/calfcord/bridge/history.py                 |      202 |        5 |       62 |        5 |     96% |282, 443-\>445, 455-\>457, 477-\>479, 721-728, 781 |
| src/calfcord/bridge/ingress.py                 |      211 |       11 |       70 |        7 |     94% |157-\>159, 641-645, 690, 765, 777-789, 862-863, 883, 912 |
| src/calfcord/bridge/normalizer.py              |       89 |        2 |       22 |        2 |     96% |   83, 288 |
| src/calfcord/bridge/outbox.py                  |      165 |        7 |       40 |        3 |     95% |572, 601, 623-632, 800-807 |
| src/calfcord/bridge/pending\_wires.py          |       62 |        0 |       12 |        1 |     99% | 270-\>272 |
| src/calfcord/bridge/registry.py                |       92 |        0 |       24 |        0 |    100% |           |
| src/calfcord/bridge/slash.py                   |      164 |       37 |       18 |        0 |     77% |96-97, 134, 166, 194-195, 277-278, 340-341, 375-378, 388-389, 402-406, 411-418, 430-465 |
| src/calfcord/bridge/steps.py                   |      273 |       12 |      118 |       11 |     94% |252, 375-\>370, 388-\>383, 390-\>381, 465-475, 483-484, 517, 548-\>543, 550-\>541, 567, 609-610, 716, 760 |
| src/calfcord/bridge/steps\_state.py            |       57 |        0 |       16 |        0 |    100% |           |
| src/calfcord/bridge/steps\_toggle.py           |       62 |        0 |        8 |        0 |    100% |           |
| src/calfcord/bridge/synthesized.py             |       31 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/transcripts.py             |      104 |        3 |       14 |        0 |     97% |   172-176 |
| src/calfcord/bridge/wire.py                    |       40 |        0 |        6 |        0 |    100% |           |
| src/calfcord/cli/\_agents.py                   |       87 |        5 |       14 |        1 |     94% |123-124, 170-172, 258-\>260 |
| src/calfcord/cli/\_envfile.py                  |       55 |        2 |       32 |        2 |     95% |   75, 141 |
| src/calfcord/cli/\_fields.py                   |       80 |        0 |       30 |        0 |    100% |           |
| src/calfcord/cli/\_mcp.py                      |        8 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/\_prompts.py                  |       33 |       12 |        0 |        0 |     64% |81-84, 91-93, 96-101, 104-106, 109-114 |
| src/calfcord/cli/\_providers.py                |      137 |        6 |       42 |        4 |     94% |215-216, 221-222, 224, 295-\>299, 357, 393-\>395 |
| src/calfcord/cli/agent\_create.py              |       36 |        0 |        4 |        0 |    100% |           |
| src/calfcord/cli/agent\_edit.py                |      132 |       16 |       38 |        3 |     89% |86-90, 116-120, 144-146, 242, 250-251, 280, 307-311 |
| src/calfcord/cli/agent\_inspect.py             |       71 |        2 |       20 |        2 |     96% |   75, 138 |
| src/calfcord/cli/agent\_lifecycle.py           |      113 |        2 |       38 |        1 |     98% |270-271, 285-\>287 |
| src/calfcord/cli/agent\_tools.py               |       89 |        9 |       26 |        1 |     91% |56, 139-144, 223-228 |
| src/calfcord/cli/deploy.py                     |       86 |        1 |       30 |        2 |     97% |484-\>489, 493 |
| src/calfcord/cli/discord\_discovery.py         |      183 |       17 |       48 |        8 |     89% |179-180, 204-206, 225, 327, 331, 438-\>436, 463-464, 483, 490, 498-499, 507-509 |
| src/calfcord/cli/doctor.py                     |      195 |        5 |       74 |        0 |     98% |   106-110 |
| src/calfcord/cli/explain.py                    |       17 |        0 |        2 |        0 |    100% |           |
| src/calfcord/cli/init.py                       |      231 |        0 |       46 |        2 |     99% |386-\>388, 500-\>exit |
| src/calfcord/cli/logs.py                       |       66 |        0 |       24 |        0 |    100% |           |
| src/calfcord/cli/main.py                       |      334 |       15 |      150 |        7 |     95% |321-323, 471, 489, 514, 632, 790-792, 836-837, 854-855, 868 |
| src/calfcord/cli/mcp\_admin.py                 |      182 |       16 |       74 |        4 |     91% |92, 98-\>100, 199, 277-279, 304-318 |
| src/calfcord/cli/router\_config.py             |       80 |        0 |       18 |        0 |    100% |           |
| src/calfcord/cli/setup\_state.py               |       45 |        0 |        4 |        0 |    100% |           |
| src/calfcord/control\_plane/builders.py        |        9 |        0 |        0 |        0 |    100% |           |
| src/calfcord/control\_plane/definition\_ref.py |       10 |        0 |        2 |        0 |    100% |           |
| src/calfcord/control\_plane/first\_reply.py    |       43 |        1 |        8 |        0 |     98% |       200 |
| src/calfcord/control\_plane/probe.py           |       39 |        0 |       10 |        1 |     98% |   69-\>64 |
| src/calfcord/control\_plane/publish.py         |       19 |        0 |        0 |        0 |    100% |           |
| src/calfcord/control\_plane/schema.py          |       43 |        0 |        0 |        0 |    100% |           |
| src/calfcord/control\_plane/sink.py            |       42 |        4 |        8 |        1 |     90% |112, 134-136 |
| src/calfcord/control\_plane/state\_consumer.py |       35 |        3 |       10 |        1 |     91% |94, 112-113 |
| src/calfcord/control\_plane/topics.py          |        6 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/avatar.py                 |        3 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/messages.py               |       18 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/persona.py                |      153 |       80 |       40 |        0 |     41% |247-250, 261-263, 266-267, 275, 279-285, 289-294, 347-393, 416-420, 440-444, 448-479, 484-491 |
| src/calfcord/discord/receiver.py               |       48 |       28 |        4 |        0 |     38% |43-45, 51-54, 61-62, 65-84, 92-94, 98-100, 107-108, 111-112, 115 |
| src/calfcord/discord/retry\_feedback.py        |       60 |        0 |       28 |        3 |     97% |253-\>278, 256-\>258, 274-\>276 |
| src/calfcord/discord/sender.py                 |       45 |       28 |       10 |        0 |     31% |36-37, 40-41, 49, 53-60, 64-68, 81-85, 112-131 |
| src/calfcord/discord/settings.py               |       13 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/typing.py                 |       39 |        3 |        6 |        0 |     93% |     80-85 |
| src/calfcord/health/check.py                   |       38 |        0 |        6 |        0 |    100% |           |
| src/calfcord/health/heartbeat.py               |       37 |        0 |        2 |        0 |    100% |           |
| src/calfcord/health/refresher.py               |       28 |        0 |        2 |        0 |    100% |           |
| src/calfcord/mcp/agent\_select.py              |       15 |        0 |        6 |        0 |    100% |           |
| src/calfcord/mcp/capability\_read.py           |       21 |        4 |        0 |        0 |     81% |     61-65 |
| src/calfcord/mcp/config.py                     |      132 |        2 |       60 |        1 |     98% |  149, 207 |
| src/calfcord/mcp/config\_write.py              |       44 |        1 |       14 |        0 |     98% |        82 |
| src/calfcord/mcp/runner.py                     |       39 |       13 |        2 |        1 |     66% |68-79, 83-92, 96 |
| src/calfcord/mcp/selector.py                   |       37 |        0 |       14 |        0 |    100% |           |
| src/calfcord/packaging/\_build.py              |       53 |        3 |       12 |        2 |     92% |97, 152-153 |
| src/calfcord/packaging/cli\_agents.py          |       40 |        1 |       10 |        1 |     96% |       104 |
| src/calfcord/packaging/cli\_tools.py           |       76 |        1 |       28 |        1 |     98% |       273 |
| src/calfcord/packaging/dockerfile.py           |       35 |        0 |        6 |        0 |    100% |           |
| src/calfcord/providers/codex/cli.py            |      151 |       79 |       40 |        1 |     43% |78-99, 103-110, 114-133, 143-153, 187-188, 222-246, 250 |
| src/calfcord/providers/codex/factory\_hook.py  |       10 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/codex/jwt.py            |       25 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/codex/model\_client.py  |       98 |        1 |       26 |        3 |     97% |189, 361-\>357, 374-\>377 |
| src/calfcord/providers/codex/prompt\_cache.py  |      122 |       14 |       24 |        6 |     86% |108-110, 126-\>132, 178-179, 188, 192, 198, 200-\>190, 217-\>exit, 220-221, 241-245 |
| src/calfcord/providers/codex/prompts.py        |      223 |       14 |       60 |        7 |     93% |207, 211-\>exit, 213, 216-217, 263-264, 314, 342-343, 348, 354, 377-378, 555 |
| src/calfcord/providers/codex/token\_store.py   |       44 |        0 |        2 |        0 |    100% |           |
| src/calfcord/router/config.py                  |        9 |        0 |        0 |        0 |    100% |           |
| src/calfcord/router/definition.py              |       27 |        0 |        0 |        0 |    100% |           |
| src/calfcord/router/fanout.py                  |       55 |        1 |       10 |        1 |     97% |       119 |
| src/calfcord/router/prompt.py                  |       49 |        0 |        8 |        0 |    100% |           |
| src/calfcord/router/roster.py                  |       12 |        0 |        2 |        0 |    100% |           |
| src/calfcord/router/runner.py                  |       59 |       21 |        6 |        2 |     65% |81-85, 118-\>120, 158-185, 189-200, 204 |
| src/calfcord/supervisor/\_workspace.py         |       21 |        0 |        6 |        0 |    100% |           |
| src/calfcord/supervisor/client.py              |       59 |        0 |        2 |        0 |    100% |           |
| src/calfcord/supervisor/component.py           |       44 |        0 |       12 |        1 |     98% |   72-\>71 |
| src/calfcord/supervisor/compose.py             |       61 |        0 |       14 |        0 |    100% |           |
| src/calfcord/supervisor/lifecycle.py           |      194 |        0 |       38 |        1 |     99% | 115-\>120 |
| src/calfcord/supervisor/mcp\_roster.py         |      110 |        2 |       34 |        2 |     97% |  160, 187 |
| src/calfcord/supervisor/roster.py              |      187 |        0 |       56 |        0 |    100% |           |
| src/calfcord/tools/deploy\_filters.py          |       93 |        0 |       48 |        0 |    100% |           |
| src/calfcord/tools/private\_chat.py            |      296 |        4 |       70 |        3 |     98% |1299-1306, 1361-1372 |
| src/calfcord/tools/runner.py                   |       60 |        1 |        6 |        1 |     97% |       198 |
| src/calfcord/topics.py                         |       11 |        0 |        0 |        0 |    100% |           |
| **TOTAL**                                      | **8367** |  **582** | **2180** |  **126** | **92%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/ryan-yuuu/calfcord/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/calfcord/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ryan-yuuu/calfcord/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/calfcord/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fryan-yuuu%2Fcalfcord%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/calfcord/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.