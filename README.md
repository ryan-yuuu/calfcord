# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                          |    Stmts |     Miss |   Branch |   BrPart |   Cover |   Missing |
|---------------------------------------------- | -------: | -------: | -------: | -------: | ------: | --------: |
| src/calfcord/\_atomic.py                      |       19 |        0 |        2 |        0 |    100% |           |
| src/calfcord/\_provisioning.py                |       19 |        0 |        6 |        0 |    100% |           |
| src/calfcord/\_worker\_runtime.py             |       41 |        0 |       14 |        0 |    100% |           |
| src/calfcord/agents/definition.py             |       86 |        0 |       20 |        0 |    100% |           |
| src/calfcord/agents/factory.py                |      107 |        2 |       34 |        2 |     97% |  147, 161 |
| src/calfcord/agents/identifier.py             |        7 |        0 |        0 |        0 |    100% |           |
| src/calfcord/agents/loader.py                 |       43 |        1 |       18 |        1 |     97% |       127 |
| src/calfcord/agents/md\_writer.py             |       78 |        3 |       14 |        3 |     93% |89-\>92, 160, 270-\>276, 276-\>exit, 283-287 |
| src/calfcord/agents/memory.py                 |       53 |        0 |       10 |        0 |    100% |           |
| src/calfcord/agents/runner.py                 |       93 |       20 |       18 |        1 |     81% |226-256, 260-269, 273 |
| src/calfcord/agents/thinking.py               |       41 |        4 |       22 |        2 |     90% |103-107, 116-120 |
| src/calfcord/bridge/a2a\_dispatch.py          |       57 |        0 |        6 |        0 |    100% |           |
| src/calfcord/bridge/a2a\_project.py           |       68 |        1 |       16 |        1 |     98% |       147 |
| src/calfcord/bridge/egress.py                 |       79 |        3 |       18 |        0 |     97% |   185-191 |
| src/calfcord/bridge/gateway.py                |      226 |       51 |       42 |        2 |     74% |90-91, 96-97, 240-241, 245-246, 326-328, 382, 401, 415-517, 521 |
| src/calfcord/bridge/history.py                |      244 |        7 |       74 |        5 |     96% |384-\>386, 396-\>398, 418-\>420, 662-669, 722, 839-\>841, 935-946 |
| src/calfcord/bridge/mention\_handler.py       |      130 |        0 |       24 |        0 |    100% |           |
| src/calfcord/bridge/normalizer.py             |       43 |        0 |        6 |        0 |    100% |           |
| src/calfcord/bridge/overrides.py              |       23 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/persona\_resolve.py       |        5 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/progress.py               |       82 |        2 |       20 |        2 |     96% |  223, 267 |
| src/calfcord/bridge/reply\_poster.py          |      104 |        7 |       16 |        1 |     93% |65-67, 187-188, 312-313 |
| src/calfcord/bridge/roster.py                 |       29 |        0 |        0 |        0 |    100% |           |
| src/calfcord/bridge/slash.py                  |       98 |        6 |       16 |        0 |     95% |74, 97, 185-186, 224-225 |
| src/calfcord/bridge/step\_events.py           |       48 |        3 |       12 |        1 |     93% | 64-65, 67 |
| src/calfcord/bridge/steps\_render.py          |      160 |        9 |       70 |        8 |     93% |198-199, 218-221, 241, 318-\>313, 329-\>326, 331-\>326, 333-\>324, 365, 416, 451-452 |
| src/calfcord/bridge/steps\_toggle.py          |       62 |        0 |        8 |        0 |    100% |           |
| src/calfcord/bridge/transcripts.py            |      132 |        3 |       16 |        0 |     98% |   176-180 |
| src/calfcord/bridge/wire.py                   |       40 |        0 |        6 |        0 |    100% |           |
| src/calfcord/cli/\_agents.py                  |       85 |        5 |       14 |        1 |     94% |123-124, 163-165, 249-\>251 |
| src/calfcord/cli/\_envfile.py                 |       55 |        2 |       32 |        2 |     95% |   75, 141 |
| src/calfcord/cli/\_fields.py                  |       70 |        0 |       26 |        0 |    100% |           |
| src/calfcord/cli/\_mcp.py                     |        8 |        0 |        0 |        0 |    100% |           |
| src/calfcord/cli/\_prompts.py                 |       33 |       12 |        0 |        0 |     64% |81-84, 91-93, 96-101, 104-106, 109-114 |
| src/calfcord/cli/\_providers.py               |      137 |        6 |       42 |        4 |     94% |215-216, 221-222, 224, 295-\>299, 357, 393-\>395 |
| src/calfcord/cli/agent\_create.py             |       36 |        0 |        4 |        0 |    100% |           |
| src/calfcord/cli/agent\_edit.py               |      125 |       15 |       34 |        2 |     89% |86-90, 116-120, 144-146, 239-240, 269, 296-300 |
| src/calfcord/cli/agent\_inspect.py            |       71 |        2 |       20 |        2 |     96% |   75, 137 |
| src/calfcord/cli/agent\_lifecycle.py          |      105 |        2 |       32 |        1 |     98% |242-243, 257-\>259 |
| src/calfcord/cli/agent\_tools.py              |       89 |        9 |       26 |        1 |     91% |56, 139-144, 223-228 |
| src/calfcord/cli/deploy.py                    |       86 |        1 |       30 |        2 |     97% |480-\>485, 489 |
| src/calfcord/cli/discord\_discovery.py        |      187 |       17 |       48 |        8 |     89% |204-205, 229-231, 250, 352, 356, 463-\>461, 488-489, 508, 515, 523-524, 532-534 |
| src/calfcord/cli/doctor.py                    |      162 |        5 |       64 |        0 |     98% |    99-103 |
| src/calfcord/cli/explain.py                   |       17 |        0 |        2 |        0 |    100% |           |
| src/calfcord/cli/init.py                      |      267 |        0 |       58 |        2 |     99% |469-\>471, 581-\>exit |
| src/calfcord/cli/logs.py                      |       66 |        0 |       24 |        0 |    100% |           |
| src/calfcord/cli/main.py                      |      335 |       12 |      138 |        6 |     96% |435, 455, 481, 596, 774-776, 818-819, 836-837, 850 |
| src/calfcord/cli/mcp\_admin.py                |      182 |       16 |       74 |        4 |     91% |92, 98-\>100, 199, 277-279, 304-318 |
| src/calfcord/cli/setup\_state.py              |       45 |        0 |        4 |        0 |    100% |           |
| src/calfcord/cli/tool\_aliases.py             |       60 |        0 |        8 |        0 |    100% |           |
| src/calfcord/discord/avatar.py                |        3 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/messages.py              |       18 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/persona.py               |      155 |       76 |       40 |        0 |     44% |261-263, 266-267, 275, 279-285, 289-294, 347-393, 416-420, 440-444, 469-500, 505-512 |
| src/calfcord/discord/receiver.py              |       47 |       27 |        4 |        0 |     39% |43-45, 55-57, 64-65, 68-87, 95-97, 101-103, 110-111, 114-115, 118 |
| src/calfcord/discord/retry\_feedback.py       |       60 |        0 |       28 |        3 |     97% |253-\>278, 256-\>258, 274-\>276 |
| src/calfcord/discord/sender.py                |       45 |       28 |       10 |        0 |     31% |36-37, 40-41, 49, 53-60, 64-68, 81-85, 112-131 |
| src/calfcord/discord/settings.py              |       13 |        0 |        0 |        0 |    100% |           |
| src/calfcord/discord/typing.py                |       39 |        3 |        6 |        0 |     93% |     82-87 |
| src/calfcord/health/check.py                  |       38 |        0 |        6 |        0 |    100% |           |
| src/calfcord/health/heartbeat.py              |       37 |        0 |        2 |        0 |    100% |           |
| src/calfcord/health/refresher.py              |       28 |        0 |        2 |        0 |    100% |           |
| src/calfcord/mcp/agent\_select.py             |       15 |        0 |        6 |        0 |    100% |           |
| src/calfcord/mcp/capability\_read.py          |       21 |        4 |        0 |        0 |     81% |     61-65 |
| src/calfcord/mcp/config.py                    |      132 |        2 |       60 |        1 |     98% |  149, 207 |
| src/calfcord/mcp/config\_write.py             |       44 |        1 |       14 |        0 |     98% |        82 |
| src/calfcord/mcp/runner.py                    |       39 |       13 |        2 |        1 |     66% |68-79, 83-92, 96 |
| src/calfcord/mcp/selector.py                  |       37 |        0 |       14 |        0 |    100% |           |
| src/calfcord/providers/codex/\_paths.py       |        6 |        0 |        0 |        0 |    100% |           |
| src/calfcord/providers/codex/cli.py           |      151 |       79 |       40 |        1 |     43% |79-100, 104-111, 115-134, 144-154, 188-189, 223-247, 251 |
| src/calfcord/providers/codex/factory\_hook.py |       10 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/codex/jwt.py           |       25 |        0 |        2 |        0 |    100% |           |
| src/calfcord/providers/codex/model\_client.py |       98 |        1 |       26 |        3 |     97% |189, 361-\>357, 374-\>377 |
| src/calfcord/providers/codex/prompt\_cache.py |      122 |       14 |       24 |        6 |     86% |111-113, 129-\>135, 181-182, 191, 195, 201, 203-\>193, 220-\>exit, 223-224, 244-248 |
| src/calfcord/providers/codex/prompts.py       |      223 |       14 |       60 |        7 |     93% |207, 211-\>exit, 213, 216-217, 263-264, 314, 342-343, 348, 354, 377-378, 555 |
| src/calfcord/providers/codex/token\_store.py  |       44 |        0 |        2 |        0 |    100% |           |
| src/calfcord/supervisor/\_workspace.py        |       21 |        0 |        6 |        0 |    100% |           |
| src/calfcord/supervisor/client.py             |       59 |        0 |        2 |        0 |    100% |           |
| src/calfcord/supervisor/component.py          |       44 |        0 |       12 |        1 |     98% |   73-\>72 |
| src/calfcord/supervisor/compose.py            |       81 |        0 |       24 |        1 |     99% | 125-\>127 |
| src/calfcord/supervisor/lifecycle.py          |      220 |        0 |       50 |        1 |     99% | 189-\>194 |
| src/calfcord/supervisor/mcp\_roster.py        |      110 |        2 |       34 |        2 |     97% |  160, 187 |
| src/calfcord/supervisor/roster.py             |      195 |        0 |       56 |        0 |    100% |           |
| src/calfcord/tools/deploy\_filters.py         |      114 |        0 |       62 |        0 |    100% |           |
| src/calfcord/tools/runner.py                  |       56 |        1 |        6 |        1 |     97% |       169 |
| **TOTAL**                                     | **6788** |  **491** | **1790** |   **92** | **92%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/ryan-yuuu/agent-disco/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ryan-yuuu/agent-disco/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fryan-yuuu%2Fagent-disco%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/ryan-yuuu/agent-disco/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.