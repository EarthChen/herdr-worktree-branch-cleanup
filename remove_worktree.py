"""Interactive action: remove a worktree and let the user decide what happens to its branch.

`open`  launches the popup (action entry point).
`ask`   runs inside the popup, collects the choice, hands it to a detached worker.
`apply` runs detached: removing the worktree closes its workspace, which kills the popup.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from keys import read_key
from cleanup_branch import (Config, Removal, consume_handled, delete_branch, load_config,
                            mark_handled, skip_reason)

PLUGIN_ID = "worktree-branch-cleanup"
ENTRYPOINT = "remove"
CHOICES = {"w": "worktree", "b": "branch", "f": "force"}

Herdr = Callable[..., dict]


class HerdrError(Exception):
    pass


class NotRemovable(Exception):
    pass


@dataclass(frozen=True)
class Target:
    workspace_id: str
    path: str
    branch: str | None
    repo_root: str
    is_detached: bool
    is_bare: bool


def herdr_cli(*args: str) -> dict:
    proc = subprocess.run([os.environ.get("HERDR_BIN_PATH", "herdr"), *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        try:
            message = json.loads(proc.stderr)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = proc.stderr.strip() or f"herdr exited with {proc.returncode}"
        raise HerdrError(message)
    return json.loads(proc.stdout)["result"]


def resolve_target(herdr: Herdr, workspace_id: str) -> Target:
    info = herdr("workspace", "get", workspace_id)["workspace"]
    wt_info = info.get("worktree") or {}
    if not wt_info.get("is_linked_worktree"):
        raise NotRemovable("this workspace is not a linked worktree")
    for wt in herdr("worktree", "list", "--workspace", workspace_id)["worktrees"]:
        if wt.get("open_workspace_id") == workspace_id:
            return Target(workspace_id, wt["path"], wt.get("branch"), wt_info["repo_root"],
                          bool(wt["is_detached"]), bool(wt["is_bare"]))
    raise NotRemovable("no worktree is open in this workspace")


def choose(read: Callable[[str], str], write: Callable[..., None], branch: str | None) -> str:
    write(f"Remove this worktree?  branch: {branch or '(none)'}")
    write("  [w] worktree only")
    write("  [b] worktree + delete branch (git branch -d, keeps unmerged)")
    write("  [f] worktree + force delete branch (git branch -D)")
    write("  [q] / Esc  cancel")
    write("Press a key.")
    return CHOICES.get(read("").strip(), "cancel")


def execute(choice: str, target: Target, herdr: Herdr, config: Config,
            state_dir: Path | None = None) -> list[str]:
    if choice == "cancel":
        return ["cancelled"]
    # The removal fires the worktree.removed hook; the marker tells it this flow already asked.
    if state_dir:
        mark_handled(state_dir, target.workspace_id)
    try:
        herdr("worktree", "remove", "--workspace", target.workspace_id)
    except HerdrError as exc:
        if state_dir:
            consume_handled(state_dir, target.workspace_id)  # no event will fire
        # Keep the branch: the worktree is still alive and needs it.
        return [f"worktree not removed: {exc}"]
    report = [f"removed worktree {target.path}"]
    if choice == "worktree":
        return report
    removal = Removal(target.repo_root, target.branch, True, target.is_bare, target.is_detached)
    reason = skip_reason(removal, config.protected)
    if reason:
        return report + [f"branch kept: {reason}"]
    result = delete_branch(target.repo_root, target.branch, force=(choice == "force"))
    if result.returncode != 0:
        return report + [f"branch {target.branch!r} kept: {result.stderr.strip().splitlines()[0]}"]
    return report + [f"deleted branch {target.branch!r}"]


def notify(title: str, lines: list[str]) -> None:
    subprocess.run([os.environ.get("HERDR_BIN_PATH", "herdr"), "notification", "show", title,
                    "--body", "\n".join(lines)], capture_output=True)


def workspace_id_from(env: dict[str, str]) -> str:
    ctx = json.loads(env.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    ws = ctx.get("workspace_id") or env.get("HERDR_WORKSPACE_ID")
    if not ws:
        raise NotRemovable("no workspace in the action context")
    return ws


def cmd_open(env: dict[str, str]) -> int:
    ws = workspace_id_from(env)
    return subprocess.run([
        env.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open",
        "--plugin", PLUGIN_ID, "--entrypoint", ENTRYPOINT, "--placement", "popup",
        "--width", "70", "--height", "12",
        "--env", f"PATH={env.get('PATH', '')}", "--env", f"TARGET_WORKSPACE_ID={ws}", "--focus",
    ]).returncode


def cmd_ask(env: dict[str, str]) -> int:
    ws = env["TARGET_WORKSPACE_ID"]
    try:
        target = resolve_target(herdr_cli, ws)
    except (NotRemovable, HerdrError) as exc:
        print(f"cannot remove: {exc}")
        print("press any key to close")
        read_key()
        return 1
    choice = choose(lambda _: read_key(), print, target.branch)
    if choice == "cancel":
        return 0
    subprocess.Popen([sys.executable, str(Path(__file__)), "apply", choice, ws],
                     start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return 0


def cmd_apply(env: dict[str, str], choice: str, ws: str) -> int:
    config = load_config(Path(env.get("HERDR_PLUGIN_CONFIG_DIR", ".")))
    state_dir = Path(env["HERDR_PLUGIN_STATE_DIR"]) if env.get("HERDR_PLUGIN_STATE_DIR") else None
    try:
        report = execute(choice, resolve_target(herdr_cli, ws), herdr_cli, config, state_dir)
    except (NotRemovable, HerdrError) as exc:
        report = [f"failed: {exc}"]
    notify("Remove worktree", report)
    return 0


def main(argv: list[str], env: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else env
    mode = argv[0] if argv else ""
    if mode == "open":
        return cmd_open(env)
    if mode == "ask":
        return cmd_ask(env)
    if mode == "apply" and len(argv) == 3:
        return cmd_apply(env, argv[1], argv[2])
    print("usage: remove_worktree.py open | ask | apply <choice> <workspace_id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
