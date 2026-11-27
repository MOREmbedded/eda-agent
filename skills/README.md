# Skills

Client skill files that ship with eda-agent. `.claude/` is gitignored (it
holds local client state), so the canonical source lives here and you copy
what you need into your client.

## autodesign

Drives an autonomous spec-to-board PCB design run with the eda-agent MCP
server. See [autodesign/SKILL.md](autodesign/SKILL.md).

**Claude Code**: copy it into your project (or user) skills directory:

```bash
mkdir -p .claude/skills/autodesign
cp skills/autodesign/SKILL.md .claude/skills/autodesign/SKILL.md
```

Then `/autodesign` is available in that project.

**Other clients**: the same protocol is available without any skill file:
call the `design_autonomy_guide` tool, or invoke the `autonomous_design` MCP
prompt. Both need the design harness, which the Altium and EasyEDA backends
register and the KiCad backend does not.
See [../docs/AUTONOMOUS_DESIGN.md](../docs/AUTONOMOUS_DESIGN.md).

## part-docs

Fetches a part's official documents (datasheet, package drawing, app
notes, STEP model) from the manufacturer's own site, unattended, into the
design workspace's `.eda` folder, with the `part_fetch_docs` tool or the
`eda-agent part-fetch-docs` command. See
[part-docs/SKILL.md](part-docs/SKILL.md).

```bash
mkdir -p ~/.claude/skills/part-docs
cp skills/part-docs/SKILL.md ~/.claude/skills/part-docs/SKILL.md
```
