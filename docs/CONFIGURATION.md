# Configuration

Environment variables and paths that change how eda-agent behaves.

Workspace (used for IPC files between Python and Altium):

- Default: `%USERPROFILE%\EDA Agent\workspace\`
- Override: set `EDA_AGENT_WORKSPACE` environment variable
- The DelphiScript side reads the resolved path from `C:\ProgramData\eda-agent\workspace-path.txt`, which Python writes at startup and on every `install-scripts` run

UI automation (synthesised keyboard and mouse input, used for the parts of Altium with no scripting API):

- Default: **on**
- Disable: set `EDA_AGENT_UI_AUTOMATION=0` (also `false`, `no`, `off`). Anything else leaves it on, so a typo cannot silently disable it
- Read at call time, so it takes effect on the next event rather than the next restart
- See [UI automation synthesises real keyboard and mouse input](LIMITATIONS.md#ui-automation-synthesises-real-keyboard-and-mouse-input) for what it is for and what it can do

Activity log (rotating, size-capped, secrets redacted; in addition to the
server's existing stderr logging):

- Default: `%USERPROFILE%\EDA Agent\logs\eda_agent.log`
- Override the directory: set `EDA_AGENT_LOG_DIR`
- `EDA_AGENT_LOG_MAX_MB` (default 10): rotate a file at this size
- `EDA_AGENT_LOG_BACKUPS` (default 5): rotated files kept
- `EDA_AGENT_LOG_TOTAL_MAX_MB` (default 200): hard cap for the whole log
  directory; oldest rotated files are deleted first
- `EDA_AGENT_LOG_MIN_FREE_MB` (default 1024): below this free disk space,
  DEBUG/INFO logging is suspended and one CRITICAL entry is written
- Bearer tokens, OAuth tokens and distributor/Nexar API keys are redacted
  before a line is written, regardless of which code logged it

Altium 365 takes one of two routes, and sets no default between them:

| Route | Set | Token from |
|---|---|---|
| Workspace, direct | `EDA_AGENT_ALTIUM365_DOMAIN`, `EDA_AGENT_ALTIUM365_TOKEN` | the workspace's admin pages, only on plans that offer API tokens (lasts up to a year) |
| Nexar | `EDA_AGENT_NEXAR_APP_ID`, and `EDA_AGENT_NEXAR_CLIENT_SECRET` for a portal app | `eda-agent altium365-login` once; `altium365-logout` forgets it |

Setting the domain wins. A workspace token has no refresh grant, so an
expired one is reported and has to be replaced by hand rather than
renewed against an identity server that never issued it.

Part sourcing and the rest of the `eng_*` engineering tools have their
own environment variables (`EDA_AGENT_ALTIUM365_WORKSPACES`,
`EDA_AGENT_LTSPICE_EXE`, `EDA_AGENT_IPC_DIR`, and more) - see [Engineering tools](ENGINEERING_TOOLS.md) and
[Part sourcing](PART_SOURCING.md).

Coordinates throughout the API are in **mils** (1 mil = 0.0254 mm).
