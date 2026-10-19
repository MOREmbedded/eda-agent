# SPDX-License-Identifier: Apache-2.0
"""Bring an installation up to date, and say what is left for a human.

Updating eda-agent is four things that fail independently: the git
checkout, the installed Python package, the DelphiScript files copied
out to the scripts directory, and the copy Altium has COMPILED. Three
of those can be out of step with each other at once, and the symptom of
each is the same -- a tool that behaves like an older build -- so
working out which one is stale is most of the job.

WHAT THIS WILL NOT DO, because the limits shape the design:

**It cannot reload Altium's script project.** There is no scripting API
for it; the project already documents those dialogs as unreachable. So
the honest end state is "everything on disk is current, now reload the
script in Altium", not a hands-off update. That final step is reported
as an ACTION FOR YOU rather than quietly omitted.

**It does not touch your working tree unless asked.** ``git pull`` can
conflict, and a command called "update" silently rebasing someone's
uncommitted work is a bad trade. Being behind the remote is reported;
``--pull`` opts in.

**It does not reach the network unless asked.** Without ``--fetch`` the
comparison is against whatever your last fetch left behind, and the
report says so rather than presenting stale knowledge as current.

Exit codes are the useful part in a script: 0 when everything is
current, 1 when something needs a human, 2 on an error.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "Step",
    "gather_state",
    "plan",
    "run_update",
]


@dataclass
class Step:
    """One finding, and whose job it is.

    ``action`` splits what this command did from what is left for the
    reader. A step that reports a problem and does not say which of the
    two it is leaves them re-reading it to work that out.
    """

    name: str
    status: str          # "current" | "done" | "action" | "unknown"
    message: str
    action: str = ""     # what the human must do, when status is "action"
    details: dict = field(default_factory=dict)

    @property
    def needs_human(self) -> bool:
        return self.status == "action"


def _run_git(args: list[str], cwd: Path,
             timeout: float = 30.0) -> Optional[str]:
    """Stdout of a git command, or None if git is absent or it failed.

    None rather than an exception: not being in a checkout is an
    ordinary state for an installed copy, not an error to report.
    """
    try:
        completed = subprocess.run(
            ["git"] + args, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _repo_root() -> Optional[Path]:
    """The checkout this package is being run from, if it is one."""
    here = Path(__file__).resolve().parent
    top = _run_git(["rev-parse", "--show-toplevel"], here)
    return Path(top) if top else None


def _is_editable_install() -> bool:
    """Whether the importable package is the source tree itself.

    An editable install needs no reinstall after a pull; a wheel does,
    and the difference decides whether `git pull` was enough.
    """
    import eda_agent

    package = Path(eda_agent.__file__).resolve().parent
    root = _repo_root()
    if root is None:
        return False
    try:
        package.relative_to(root)
    except ValueError:
        return False
    return True


def gather_state(*, fetch: bool = False, compare_remote: str = "",
                 source_dir: str = "") -> dict[str, Any]:
    """Everything the decisions below are made from, read in one place.

    Separated from :func:`plan` so the decisions can be tested without
    git, Altium, or a network, which is what they would otherwise all
    need.
    """
    from eda_agent.cli import (get_bundled_scripts_path,
                               get_default_scripts_dest)
    from eda_agent.diag.health import _script_version

    root = _repo_root()
    state: dict[str, Any] = {
        "repo_root": str(root) if root else "",
        "editable": _is_editable_install() if root else False,
        "fetched": False,
        "behind": None,
        "ahead": None,
        "branch": "",
        "dirty": None,
    }

    if root is not None:
        state["branch"] = _run_git(["rev-parse", "--abbrev-ref", "HEAD"],
                                   root) or ""
        porcelain = _run_git(["status", "--porcelain"], root)
        state["dirty"] = bool(porcelain) if porcelain is not None else None
        if fetch:
            state["fetched"] = _run_git(["fetch", "--quiet"], root,
                                        timeout=60.0) is not None
        counts = _run_git(
            ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            root)
        if counts:
            parts = counts.split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                state["ahead"], state["behind"] = int(parts[0]), int(parts[1])

    # Other remotes are OPT-IN. The default question is "is my own fork
    # ahead of this checkout", and quietly reporting a third party's
    # commits every run turns the to-do list into something nobody
    # reads. Name one with compare_remote when you want it.
    if root is not None and compare_remote:
        state.update(_remote_state(root, compare_remote,
                                   state.get("branch") or "main",
                                   fetch=fetch))

    if source_dir:
        state.update(_source_dir_state(source_dir))

    bundled = Path(get_bundled_scripts_path()) / "Main.pas"
    deployed = get_default_scripts_dest() / "Main.pas"
    state["bundled_scripts"] = str(bundled.parent)
    state["deployed_scripts"] = str(deployed.parent)
    state["bundled_version"] = _script_version(bundled) if bundled.exists() \
        else None
    state["deployed_version"] = _script_version(deployed) \
        if deployed.exists() else None

    from eda_agent import __version__
    state["package_version"] = __version__
    return state


def _remote_state(root: Path, name: str, branch: str, *,
                  fetch: bool) -> dict[str, Any]:
    """How far behind a NAMED remote this checkout is.

    The caller names it rather than this guessing which of several
    remotes is "the real one". A checkout can have a fork, the project
    it was forked from, and a colleague's copy all configured at once,
    and picking between them silently is how someone ends up told about
    commits they never asked for.
    """
    out: dict[str, Any] = {"upstream_remote": "", "upstream_behind": None,
                           "upstream_ref": "", "upstream_version": ""}
    remotes = (_run_git(["remote"], root) or "").split()
    if name not in remotes:
        out["upstream_remote"] = name
        out["upstream_missing"] = sorted(remotes)
        return out
    out["upstream_remote"] = name

    if fetch:
        # --no-tags, and it matters. Fetching another remote normally
        # imports its TAGS into the local repo, and a fork checking the
        # project it came from then acquires that project's release
        # tags. That is how this repo quietly ended up holding a v0.7.0
        # it had never released, which broke a guard asserting the
        # declared version is ahead of every tag. Comparing commits
        # needs no tags at all.
        _run_git(["fetch", name, "--quiet", "--no-tags"], root,
                 timeout=60.0)

    # Prefer the same branch name, fall back to the usual default: a
    # fork's feature branch has no counterpart upstream, and comparing
    # against nothing would read as "up to date".
    for candidate in (f"{name}/{branch}", f"{name}/main",
                      f"{name}/master"):
        counts = _run_git(["rev-list", "--left-right", "--count",
                           f"HEAD...{candidate}"], root)
        if counts:
            parts = counts.split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                out["upstream_ref"] = candidate
                out["upstream_behind"] = int(parts[1])
                break
    if out["upstream_ref"]:
        out["upstream_version"] = _version_at(root, out["upstream_ref"])
    return out


def _version_at(root: Path, ref: str) -> str:
    """The package version declared at a git ref, or "" if unreadable."""
    import re

    text = _run_git(["show", f"{ref}:src/eda_agent/__init__.py"], root)
    if not text:
        return ""
    match = re.search(r"__version__\s*=\s*[\"']([^\"']+)", text)
    return match.group(1) if match else ""


def _source_dir_state(path: str) -> dict[str, Any]:
    """Read another checkout on disk as the thing to update FROM.

    For the case with no git and no network in it: a copy on a share, a
    release folder, a colleague's tree. The folder is read, never
    written to, and nothing about it is assumed beyond the two files
    that carry a version.
    """
    import re

    from eda_agent.diag.health import _script_version

    out: dict[str, Any] = {"source_dir": str(path), "source_ok": False,
                           "source_problem": "", "source_version": "",
                           "source_script_version": "",
                           "source_scripts": ""}
    root = Path(path).expanduser()
    try:
        root = root.resolve()
    except OSError as exc:
        out["source_problem"] = f"cannot resolve {path}: {exc}"
        return out
    if not root.is_dir():
        out["source_problem"] = f"{root} is not a directory"
        return out

    # A checkout, or a wheel's unpacked package: accept either layout
    # rather than insisting on the one this tree happens to use.
    for init in (root / "src" / "eda_agent" / "__init__.py",
                 root / "eda_agent" / "__init__.py"):
        if init.is_file():
            try:
                text = init.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r"__version__\s*=\s*[\"']([^\"']+)", text)
            out["source_version"] = match.group(1) if match else ""
            out["source_ok"] = True
            break
    if not out["source_ok"]:
        out["source_problem"] = (
            f"{root} does not look like an eda-agent checkout: no "
            f"src/eda_agent/__init__.py and no eda_agent/__init__.py")
        return out

    scripts = root / "scripts" / "altium"
    if (scripts / "Main.pas").is_file():
        out["source_scripts"] = str(scripts)
        out["source_script_version"] = _script_version(scripts / "Main.pas")
    return out


def altium_version() -> Optional[str]:
    """The SCRIPT_VERSION Altium has COMPILED, or None if it is not up.

    A plain ping, so unlike ``doctor`` this never saves a document. An
    update that silently saved someone's open work to check a version
    would be a worse trade than not checking.
    """
    import logging

    # The bridge logs every command at INFO, and the root logger sends
    # that to stdout here. One ping would then sit in the middle of the
    # report, and inside --json it would make the output unparseable.
    # The module logs under "eda_agent.bridge", not its own dotted
    # path, so silencing the longer name silences nothing.
    bridge_log = logging.getLogger("eda_agent.bridge")
    previous = bridge_log.level
    bridge_log.setLevel(logging.WARNING)
    try:
        from eda_agent.bridge import get_bridge

        reply = get_bridge().ping_with_version()
    except Exception:  # noqa: BLE001 - Altium being closed is normal here
        return None
    finally:
        bridge_log.setLevel(previous)
    if not isinstance(reply, dict):
        return None
    return str(reply.get("script_version") or "") or None


def plan(state: dict[str, Any], *, pull: bool = False,
         compiled_version: Optional[str] = None,
         scripts_deployed: bool = False,
         source_dir: str = "") -> list[Step]:
    """Turn gathered state into findings. Pure: no I/O, no side effects.

    ``scripts_deployed`` says whether the caller has already done the
    redeploy, so the step reports "done" rather than "action". The
    deciding and the doing stay apart, which is what makes every branch
    here testable.
    """
    steps: list[Step] = []

    # --- the checkout -------------------------------------------------
    if not state.get("repo_root"):
        steps.append(Step(
            "your remote", "unknown",
            "not running from a git checkout, so there is nothing to "
            "pull; this looks like an installed copy",
            action="Update it the way you installed it, e.g. "
                   "`python -m pip install --upgrade eda-agent`."))
    else:
        behind = state.get("behind")
        stale = "" if state.get("fetched") else \
            " (from your last fetch, which may be out of date; pass "\
            "--fetch to refresh)"
        if behind is None:
            steps.append(Step(
                "your remote", "unknown",
                f"on {state['branch'] or 'an unknown branch'} with no "
                f"tracking branch to compare against"))
        elif behind == 0:
            steps.append(Step(
                "your remote", "current",
                f"{state['branch']} is up to date with the remote you push to"
                + stale))
        elif pull:
            steps.append(Step(
                "your remote", "done",
                f"pulled {behind} commit(s) into {state['branch']}"))
        else:
            steps.append(Step(
                "your remote", "action",
                f"{state['branch']} is {behind} commit(s) behind the "
                f"remote you push to" + stale,
                action="Run `git pull`, or re-run this with --pull. It "
                       "is not done for you because a pull can conflict "
                       "and your tree is yours."))

        # --- the project this was forked from -------------------------
        # Separate from the step above, and the one most people mean by
        # "is there a new version". A fork's main tracks origin/main, so
        # "up to date" there only ever means "I have pushed my own
        # work"; the original project can be twenty commits ahead and
        # nothing above would say so.
        behind_upstream = state.get("upstream_behind")
        if state.get("upstream_remote") and behind_upstream is not None:
            ref = state.get("upstream_ref", "")
            their_version = state.get("upstream_version", "")
            mine = state.get("package_version", "")
            if behind_upstream == 0:
                steps.append(Step(
                    "original project", "current",
                    f"level with {ref}" + stale))
            else:
                version_note = ""
                if their_version and their_version != mine:
                    version_note = (f"; it declares {their_version} and "
                                    f"this is {mine}")
                steps.append(Step(
                    "original project", "action",
                    f"{behind_upstream} new commit(s) in {ref}"
                    + version_note + stale,
                    action=(
                        f"Review with `git log HEAD..{ref}`, then "
                        f"`git merge {ref}` when you want them. NOT done "
                        f"for you: this is a real merge, not a "
                        f"fast-forward, and with local commits of your "
                        f"own on top it can conflict."),
                    details={"behind": behind_upstream, "ref": ref,
                             "their_version": their_version,
                             "our_version": mine}))

        # Only a problem when a pull is actually in play. Flagging it
        # otherwise turns the ordinary state of a working checkout into
        # a to-do item, and a report that always has one is a report
        # nobody reads the to-do list of.
        if state.get("dirty") and (pull or (behind or 0) > 0):
            steps.append(Step(
                "working tree", "action",
                "you have uncommitted changes and a pull is needed",
                action="Commit or stash them first, so the update "
                       "cannot lose work. This command refuses to pull "
                       "onto a dirty tree."))
        elif state.get("dirty"):
            steps.append(Step(
                "working tree", "current",
                "uncommitted changes, which is fine since nothing "
                "needs pulling"))

    # --- a folder given as the source ---------------------------------
    if source_dir:
        if not state.get("source_ok"):
            steps.append(Step(
                "source folder", "action",
                state.get("source_problem", "unusable"),
                action="Point --from at an eda-agent checkout or an "
                       "unpacked copy of the package."))
        else:
            theirs = state.get("source_version", "")
            mine = state.get("package_version", "")
            note = (f"version {theirs}" if theirs else "no version found")
            if theirs and mine and theirs != mine:
                note += f", against {mine} installed here"
            steps.append(Step(
                "source folder", "current",
                f"{state['source_dir']}: {note}"))
            if theirs and mine and theirs != mine:
                steps.append(Step(
                    "python package", "action",
                    f"that folder carries {theirs} and {mine} is "
                    f"installed here",
                    action=f"Run `python -m pip install -e "
                           f"\"{state['source_dir']}\"` to run from it, "
                           f"or `python -m pip install "
                           f"\"{state['source_dir']}\"` for a copy. Not "
                           f"done for you: installing a package is a "
                           f"bigger change than redeploying a script."))

    # --- the installed package ----------------------------------------
    if source_dir:
        pass  # answered above, against the folder rather than this tree
    elif state.get("repo_root") and state.get("editable"):
        steps.append(Step(
            "python package", "current",
            f"editable install of {state['package_version']}; code "
            f"changes are live with no reinstall"))
    elif state.get("repo_root"):
        steps.append(Step(
            "python package", "action",
            f"{state['package_version']} is installed as a copy, not "
            f"from this tree",
            action="Run `python -m pip install -e .` so a pull takes "
                   "effect, or reinstall the wheel."))

    # --- the deployed DelphiScript ------------------------------------
    # With --from, the folder is the thing being updated FROM, so its
    # scripts are what "current" means. Comparing against this tree's
    # bundled copy instead would report the local checkout's state and
    # answer a question nobody asked.
    bundled = (state.get("source_script_version")
               if source_dir and state.get("source_ok")
               else state.get("bundled_version"))
    deployed = state.get("deployed_version")
    if source_dir and state.get("source_ok") and not bundled:
        steps.append(Step(
            "altium scripts", "unknown",
            f"{state['source_dir']} carries no scripts/altium/Main.pas, "
            f"so there is nothing to deploy from it"))
        bundled = None
    if bundled is None:
        if not (source_dir and state.get("source_ok")):
            steps.append(Step(
                "altium scripts", "unknown",
                f"no bundled Main.pas under "
                f"{state.get('bundled_scripts')}"))
    elif deployed is None:
        steps.append(Step(
            "altium scripts", "done" if scripts_deployed else "action",
            f"none were installed at {state.get('deployed_scripts')}"
            if not scripts_deployed else
            f"installed {bundled} to {state.get('deployed_scripts')}",
            action="" if scripts_deployed else
                   "Run `python -m eda_agent install-scripts`."))
    elif scripts_deployed:
        # Checked BEFORE the equality below, not after: run_update
        # re-reads the state once it has copied the files, so by now
        # the two versions DO match. Testing equality first reported a
        # redeploy as "nothing to do", which hides the one fact the
        # reader needs -- that the scripts just changed under a running
        # Altium, and it has to reload.
        steps.append(Step(
            "altium scripts", "done",
            f"redeployed to {bundled}"))
    elif deployed == bundled:
        steps.append(Step(
            "altium scripts", "current",
            f"deployed {deployed} matches this tree"))
    else:
        # Name the right source, and suggest the command that copies
        # from it. install-scripts always copies from THIS tree, so
        # advising it after --from would deploy the wrong files and
        # look like the update had failed.
        from_folder = bool(source_dir and state.get("source_ok"))
        where = (f"{state['source_dir']}'s" if from_folder
                 else "this tree's")
        fix = (f'Re-run without --check: `python -m eda_agent update '
               f'--from "{state["source_dir"]}"`.' if from_folder else
               "Run `python -m eda_agent install-scripts --force`.")
        steps.append(Step(
            "altium scripts", "action",
            f"deployed {deployed} differs from {where} {bundled}",
            action=fix))

    # --- the copy Altium compiled -------------------------------------
    # Last on purpose: it is the only step whose fix is a GUI action, so
    # it reads as the thing left over once the disk is in order.
    want = bundled if scripts_deployed or deployed is None else deployed
    if compiled_version is None:
        steps.append(Step(
            "altium session", "unknown",
            "Altium did not answer a ping, so nothing could be checked "
            "about what it has compiled",
            details={"expected": want or ""}))
    elif want and compiled_version == want:
        steps.append(Step(
            "altium session", "current",
            f"Altium has {compiled_version} compiled, matching the "
            f"deployed scripts"))
    else:
        steps.append(Step(
            "altium session", "action",
            f"Altium has {compiled_version} compiled but "
            f"{want} is on disk",
            action="Reload the script project in Altium: File > Run "
                   "Script, expand Altium_API > Dispatcher.pas, run "
                   "StartMCPServer. A .pas on disk is not compiled "
                   "until Altium reloads it, and there is no API to do "
                   "it from here.",
            details={"compiled": compiled_version, "on_disk": want or ""}))

    return steps


def format_report(steps: list[Step]) -> str:
    """One line per step, then the actions gathered at the end.

    Gathered rather than left inline because the whole point of running
    this is the answer to "what do I do now", and hunting for it among
    the passes is the thing a report should save you.
    """
    icons = {"current": "OK  ", "done": "DONE", "action": "TODO",
             "unknown": "??  "}
    lines = ["eda-agent update", "================", ""]
    for step in steps:
        lines.append(f"  [{icons.get(step.status, '??  ')}] "
                     f"{step.name}, {step.message}")

    todo = [s for s in steps if s.needs_human]
    if todo:
        lines.append("")
        lines.append(f"{len(todo)} thing(s) left for you:")
        for index, step in enumerate(todo, 1):
            lines.append(f"  {index}. {step.name}: {step.action}")
    else:
        lines.append("")
        lines.append("Everything is current. Nothing left to do.")
    return "\n".join(lines)


def _deploy_from(scripts_dir: str) -> int:
    """Copy another folder's Altium scripts over the deployed ones.

    Mirrors what install-scripts does, from a given directory rather
    than the bundled one. Files only, no recursion: the script project
    is flat, and walking into whatever else is in that folder would
    copy more than was asked for.
    """
    import shutil

    from eda_agent.cli import get_default_scripts_dest

    source = Path(scripts_dir)
    dest = get_default_scripts_dest()
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for item in sorted(source.iterdir()):
            if item.is_file():
                shutil.copy2(item, dest / item.name)
    except OSError:
        return 1
    return 0


def run_update(*, pull: bool = False, fetch: bool = False,
               check_only: bool = False, force_scripts: bool = False,
               compare_remote: str = "",
               source_dir: str = "") -> tuple[list[Step], int]:
    """Do the update, and return ``(steps, exit_code)``.

    Exit code 0 when everything is current, 1 when something is left
    for a human, 2 when a step this command owns actually failed.
    """
    state = gather_state(fetch=fetch, compare_remote=compare_remote,
                         source_dir=source_dir)

    pulled = False
    if pull and not check_only and state.get("repo_root"):
        if state.get("dirty"):
            # Refusing beats pulling onto a dirty tree and handing back
            # a conflict the caller did not ask for.
            pull = False
        else:
            pulled = _run_git(["pull", "--ff-only"],
                              Path(state["repo_root"]),
                              timeout=120.0) is not None
            if pulled:
                state = gather_state(fetch=False, compare_remote=compare_remote,
                                 source_dir=source_dir)

    deployed = False
    want_version = (state.get("source_script_version")
                    if source_dir and state.get("source_ok")
                    else state.get("bundled_version"))
    needs_deploy = (want_version
                    and state.get("deployed_version") != want_version)
    if (needs_deploy or force_scripts) and not check_only:
        import contextlib
        import io

        from eda_agent.cli import cmd_install_scripts

        # install-scripts prints its own one-time-setup blurb, which is
        # right when a human ran it directly and is noise in the middle
        # of this report -- it would separate the findings from the
        # to-do list they lead to. Captured, and shown only if the
        # redeploy failed, where it is the diagnosis.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            if source_dir and state.get("source_scripts"):
                code = _deploy_from(state["source_scripts"])
            else:
                code = cmd_install_scripts(dest=None, force=True)
        if code == 0:
            deployed = True
            state = gather_state(fetch=False, compare_remote=compare_remote,
                                 source_dir=source_dir)
        else:
            return ([Step("altium scripts", "action",
                          "the redeploy failed",
                          action="Run `python -m eda_agent "
                                 "install-scripts --force` and read the "
                                 "error.",
                          details={"output": captured.getvalue()[-800:]})],
                    2)

    steps = plan(state, pull=pulled, compiled_version=altium_version(),
                 scripts_deployed=deployed, source_dir=source_dir)
    return steps, (1 if any(s.needs_human for s in steps) else 0)
