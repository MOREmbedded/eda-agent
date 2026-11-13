# Getting started

Install eda-agent, connect it to your MCP client and to Altium, and check that the two halves can talk.

## Requirements

- Python 3.11+
- An EDA tool, one of:
    - **Altium Designer** (recent versions, AD20+ preferred) - Windows only
    - **KiCad 10** with its API enabled (Preferences → Plugins → Enable KiCad API) and the board open in the PCB editor; nothing extra to install
    - **EasyEDA Pro**, through a browser extension you build and import into the editor. Note the connection runs the other way round: the editor dials out to this server, so nothing here can start it (see [`extensions/easyeda/README.md`](../extensions/easyeda/README.md))

The server picks a backend at startup (`EDA_AGENT_BACKEND`, default `altium`), so one install drives any of them. See [EDA backends](USING_THE_TOOLS.md#eda-backends).

## Installation

```bash
git clone https://github.com/MOREmbedded/eda-agent
cd eda-agent
python -m pip install -e .
```

Everything optional (pictures and plots, STEP models, the Rust wheels, the
RF toolbox and so on) installs with one more command, which shows its plan
and asks before it changes anything:

```bash
eda-agent install-deps --all
```

Name features to install only some (`eda-agent install-deps render
models3d`), and run `eda-agent features` to see them all. Programs such as
openEMS, LTspice and KiCad are not pip packages: the installer prints their
steps instead. See [Features and dependencies](FEATURES_AND_DEPENDENCIES.md).

Use `python -m pip` rather than a bare `pip`. It works even when pip's `Scripts` folder is not on your `PATH`, which is common when Python was installed for you by IT, and it guarantees the package goes into the same Python you will run it with.

Register the server with your MCP client. The binary is `eda-agent` and runs on stdio; consult your client's docs for how to add a local stdio-based server. Everywhere this README says `eda-agent`, `python -m eda_agent` does exactly the same thing and does not depend on `PATH`.

### Claude Code

```bash
claude mcp add altium eda-agent
```

Adds `eda-agent` as an MCP server named `altium` to your Claude Code project config. Use `-s user` to register it at the user level (available across every project):

```bash
claude mcp add -s user altium eda-agent
```

If `eda-agent` isn't on your `PATH` ("'eda-agent' is not recognized"), register it through Python instead. This needs no path at all:

```bash
claude mcp add -s user altium -- python -m eda_agent
```

The `--` matters: it tells `claude mcp add` that everything after it is the command to run, rather than options for `claude` itself.

To verify the connection: `/mcp` in a Claude Code session should list `altium` as connected.

### Other MCP clients

The server speaks standard MCP over stdio; any client that accepts a local stdio command will work. Invoke `eda-agent` (or `eda-agent serve`) as the subprocess, or `python -m eda_agent` if it is not on your `PATH`.

### Altium-side scripts

Drop the Altium script project somewhere you can find it:

```bash
eda-agent install-scripts
```

or, if `eda-agent` is not recognised:

```bash
python -m eda_agent install-scripts
```

Default destination: wherever Altium already loads the script project from (its Global Projects list, for every Altium version installed), or `%USERPROFILE%\EDA Agent\scripts\` the first time. Use `--dest PATH` to put it elsewhere; Altium still talks to the same workspace either way. `eda-agent health` reports a registration that points at a missing file.

Register the script as a Global Project in Altium (once):

1. **Preferences** (the gear icon at the top right; **DXP → Preferences** before AD18) **→ Scripting System → Global Projects** → **Install from file**
2. Select the `Altium_API.PrjScr` you just installed

From then on, every Altium startup compiles the script project and the polling loop is one click away:

1. **File → Run Script...**
2. Expand `Altium_API` → `Dispatcher.pas`, select **StartMCPServer**, click **Run**

The polling loop starts and your MCP client can drive Altium.

### Keeping it up to date

For as long as it runs, the server looks once an hour, in the background, whether this repository has new commits; the look is a cheap check that the branch has moved, and a full check follows only when it has. When there are new commits, the next tool reply says so and lists them, `app_ping` and `app_context` repeat it, and the assistant asks you before installing anything. A check that fails is reported once too. A copy installed with pip from this repository compares the commit it was installed from with the latest one; any other pip install compares its version with the one here. Either says how to upgrade. If you agree, `server_update(apply=True)` fast-forwards your clone, reinstalls when the dependencies changed, copies the Altium scripts again when they changed and restarts the polling loop. Then reconnect the server (`/mcp` in Claude Code) to run the new Python. From a terminal, the same thing is:

```bash
eda-agent update
```

Checks **your own remote** - the one you push to - and the three other
things that go stale independently, then fixes the one it safely can:

| | |
|---|---|
| your remote | reported if you are behind; **not pulled** unless you pass `--pull`, and refused outright on a dirty tree |
| the installed package | an editable install needs no reinstall; a copied one does, and a `git pull` into it changes nothing |
| the deployed DelphiScript files | **redeployed automatically** when they differ |
| the copy Altium has **compiled** | reported, and left to you |

A clone with uncommitted changes, commits of its own or another branch checked out is never touched; the update says why and stops. Set `EDA_AGENT_UPDATE_CHECK=0` to stop the background check from reaching the network.

**Other remotes are not checked** unless you ask. A checkout can have
your fork, the project it was forked from and a colleague's copy all
configured at once, and reporting someone else's commits every run
makes the to-do list something nobody reads:

```bash
eda-agent update --from upstream
```

That fetch passes `--no-tags`. Fetching another remote normally imports
its **release tags** too, so a fork checking the project it came from
quietly acquires that project's version tags - which is how this repo
briefly ended up holding a `v0.7.0` it had never released. Comparing
commits needs no tags.

### Updating from a local folder

No git and no network needed - point it at another eda-agent folder on
disk, a copy on a share, or a release directory:

```bash
eda-agent update --from D:\builds\eda-agent-0.7.1
```

It reads that folder's package version and `SCRIPT_VERSION`, compares
both against what you have, and **deploys its Altium scripts**. It will
not `pip install` for you - that is a bigger change than copying a
script, so it prints the exact command instead. Both a checkout
(`src/eda_agent/`) and an unpacked package (`eda_agent/`) are accepted.

`--from` takes a folder or a git remote name; anything that exists on
disk is treated as a folder.

### What it will not do

A `.pas` on disk is not compiled until Altium reloads the script
project, and there is **no API to do that**. So `update` finishes by
telling you to reload, rather than quietly leaving you on an old build.

`--check` changes nothing, `--fetch` refreshes remote refs so "up to
date" is current rather than from your last fetch, and `--json` gives
the steps as data. Exit code is 0 when everything is current, 1 when
something needs you, 2 on an error - so CI can branch on it without
parsing text.

The Altium check is a plain ping. Unlike `doctor` it never saves a
document, so it is safe to run mid-edit.

### Check that it worked

Two commands answer "is this set up correctly", so you find out here
rather than from a failing tool call later:

```bash
eda-agent health
```

Offline and instant: is the workspace directory writable, does the
pointer file agree with it, are the bundled scripts findable. Run it
any time; it never touches Altium.

```bash
eda-agent doctor
```

The full check, with Altium open and `StartMCPServer` running: everything
`health` covers, plus that Altium is running, that the polling loop
answers, that the deployed script version matches this source tree, and
a save round-trip. Every line is a pass, a warning or a fix instruction.

The common first failure is `workspace pointer matches config` reporting
two different paths. That means Python resolved one workspace while the
script was told another, which the next section explains.

## Workspace and profiles

Two separate ideas that are easy to confuse, because both are "somewhere
on disk that affects how this behaves". One is plumbing, the other is
your house style.

### The workspace: how Python and Altium talk

Altium's DelphiScript cannot be called directly from outside, so the two
sides pass files in a shared folder. Python writes `request_<id>.json`,
the polling loop inside Altium picks it up, acts, and writes
`response_<id>.json` back. That folder is the **workspace**, and it is
pure plumbing: nothing about your design lives there.

- Default: `%USERPROFILE%\EDA Agent\workspace\`
- Override with the `EDA_AGENT_WORKSPACE` environment variable
- DelphiScript cannot read environment variables, so Python also writes
  the resolved path to `C:\ProgramData\eda-agent\workspace-path.txt`.
  That pointer file is how the script finds the same folder.

**Both sides must agree.** If you set `EDA_AGENT_WORKSPACE` for your MCP
client but not in the terminal you later run `doctor` from, the two
resolve different folders and commands appear to vanish: one side is
posting letters to an address nobody reads. `eda-agent health` reports
exactly this as `workspace pointer matches config`. Either set the
variable the same way everywhere, or leave it unset and use the default.

One workspace per machine is normal. It is not per project, and there is
no reason to have several unless you deliberately run more than one
Altium.

### Profiles: your conventions, per company or project

A **profile** is the opposite kind of thing: no plumbing, just the rules
you would otherwise repeat on every call. Pad corner rounding, courtyard
and silkscreen policy, naming conventions, design rules, fab stackups,
schematic units and grid, project templates.

The motivating case is working for several companies on one machine. One
wants 1% pad rounding, another wants Altium's 25%; stating that on every
call is how it ends up inconsistent. Put it in a profile and the
authoring tools read it themselves, so `lib_add_footprint_pad` with no
`corner_radius` does the right thing for whoever you are working for
right now.

A profile is found in this order, first match winning:

1. `EDA_AGENT_PROFILE_DIR`, if set
2. a project-local `.eda-agent/profile/`, found by walking up from the
   working directory. Meant to be **committed** to that project's repo,
   so a team shares one house style
3. the centrally active profile, chosen with `eda-agent profile-use <name>`
   from `~/.eda-agent/profiles/`
4. none, in which case every tool keeps its built-in default

The project-local path is relative to the working directory, which
reaches the CLI but usually not the MCP server, since your client
launches it from wherever it likes. **For an MCP setup, use
`profile-use` or the environment variable.** `eda-agent profile-show`
always reports what actually applies from where you are standing, and
`eda-agent profile-validate` checks a profile before you rely on it.

A profile can also carry **templates**, which is the one place where
"your conventions" turns into actual files. A whole starter project goes
in `templates/<name>/` and is copied by
`eda-agent profile-new-project <name> <dest>`; a single sheet or board
goes in `templates/documents/` and is created, and opened in Altium, by
the `profile_new_document` tool. Both are plain copies: Altium has no
scripting API that applies a template, so a saved `.SchDoc` set up the
way you like it *is* the template. This is also the only way to pin a
new board's units and grid, since nothing can set those on a live PCB
document.

A profile's `design_rules.json` manages the board's Altium design rules:
`pcb_rules_plan` works out what the profile asks for (pure Python, no
Altium), `pcb_rules_diff` says how the open board differs, and
`pcb_rules_apply` closes the gap. Apply is idempotent, so running it on a
board that already matches writes nothing, and it never deletes: a rule
the profile does not define, a rule the schematic owns, and a rule kind
with no in-place writer each come back reported instead. The file holds
the synthesis targets rather than the rules, so every number stays traced
to a fab-profile field or a verified calculator.

The same three verbs manage the project's **ERC matrix**, which lives in
the `.PrjPcb` and so starts at Altium's defaults on every new project:
`proj_erc_plan`, `proj_erc_diff` and `proj_erc_apply` over the profile's
`erc_levels.json`. A set can `extends` another, so a company base policy
and a per-workspace narrowing of it stay two files rather than two
copies. A violation type the set does not name keeps the level it has,
and one this Altium version does not have is reported rather than
quietly skipped.

`profile_diff_all` and `profile_apply_all` run every section in one call.
A section the profile does not define is skipped rather than failed, and
one section failing does not hide the others: the design rules need a
board open and the ERC matrix needs a project, so a caller with only one
of them still gets the other's answer.

Full detail, and the format of each file, is in
[Profiles](PROFILES.md).

In short: the **workspace** is how the two halves of this server reach
each other and you mostly set it once and forget it; a **profile** is
what you want the output to look like, and is worth revisiting whenever
you change who you are working for.
