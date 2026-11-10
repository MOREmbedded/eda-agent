# Token usage of the EDA tools

Every tool call the AI makes costs tokens twice: the **arguments** it
writes (output tokens) and the **result** it reads back (input tokens,
and again on every later turn while the result stays in the
conversation). eda-agent counts both for each call, so you can see which
tools eat the context.

## What is counted

The server never learns the real token counts, so they are **estimated**
from what it sends and receives (`eda_agent/diag/usage.py`):

- text: characters / 4, rounded up. The larger of the text copy and the
  structured copy of a result is used, since a client reads one of them;
- images: shrunk to fit 1568 px on the long edge, then width x height / 750;
- time: from the call reaching the server to the result leaving it.

Expect the estimate to be within a few tens of percent. It is for
comparing tools and sessions, not for billing.

A call through a family tool is named after the member it reached:
`pcb_calc/impedance`, `tool_invoke/pcb_get_nets`. A call that raised, or answered `"ok": false`, counts
as failed.

## Where it is shown

- **Status window in Altium, Stats tab**: this session by tool, plus
  today, the whole workspace and the active profile. Read from
  `tool_usage.txt` in the bridge folder, which the server rewrites after
  each call.
- **Dashboard, Usage view** (key 9): every workspace with records, per
  tool, per day (last 30 days as bars), per session and per profile, for
  all time or the last 1, 7 or 30 days.

## Where it is kept

`<workspace>/.eda-agent/usage/calls-YYYY-MM.jsonl`, one JSON line per
call. The workspace is the folder the AI client started the MCP server
in (`EDA_AGENT_USAGE_DIR` overrides it). The folder carries its own
`.gitignore`, so records never reach a repository. The workspaces that
have records are listed in `~/.eda-agent/usage_workspaces.json` for the
dashboard.

## Switching it off

`usage_stats` is `on` by default. Turn it off with **Token stats** on
the Config tab of the status window, or per profile with
`"usage_stats": "off"` in the profile's `preferences.json` (dashboard,
Workspace view, Settings); the profile wins over the panel. Setting
`EDA_AGENT_USAGE_OFF=1` in the AI client's config also turns it off.
Off, nothing is counted or written and the Stats tab says so.
