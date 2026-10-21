"""Herdr `worktree.removed` hook: delete the branch that the worktree had checked out."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from keys import read_key

DEFAULT_PROTECTED = frozenset({"main", "master", "develop", "trunk"})
PLUGIN_ID = "worktree-branch-cleanup"


@dataclass(frozen=True)
class Config:
    ask: bool = True
    auto: bool = False
    force: bool = False
    protected: frozenset[str] = DEFAULT_PROTECTED


@dataclass(frozen=True)
class Removal:
    repo_root: str | None
    branch: str | None
    is_linked: bool
    is_bare: bool
    is_detached: bool
    workspace_id: str | None = None


def load_config(config_dir: Path) -> Config:
    path = config_dir / "config.toml"
    if not path.is_file():
        return Config()
    raw = tomllib.loads(path.read_text())
    return Config(
        ask=bool(raw.get("ask", True)),
        auto=bool(raw.get("auto", False)),
        force=bool(raw.get("force", False)),
        protected=frozenset(raw.get("protected", DEFAULT_PROTECTED)),
    )


def parse_removal(event_json: str | None, context_json: str | None) -> Removal:
    if not event_json:
        raise ValueError("HERDR_PLUGIN_EVENT_JSON is not set")
    data = json.loads(event_json)["data"]
    worktree = data["worktree"]
    # workspace is null when herdr already closed it; the invocation context
    # still carries the repository root in that case.
    ws_wt = (data.get("workspace") or {}).get("worktree") or {}
    ctx_wt = (json.loads(context_json).get("worktree") if context_json else None) or {}
    return Removal(
        repo_root=ws_wt.get("repo_root") or ctx_wt.get("repo_root"),
        branch=worktree.get("branch"),
        is_linked=bool(worktree["is_linked_worktree"]),
        is_bare=bool(worktree["is_bare"]),
        is_detached=bool(worktree["is_detached"]),
        workspace_id=data.get("workspace_id"),
    )


def skip_reason(removal: Removal, protected: frozenset[str]) -> str | None:
    if not removal.is_linked:
        return "not a linked worktree"
    if removal.is_bare or removal.is_detached or not removal.branch:
        return "worktree has no branch checked out"
    if removal.branch in protected:
        return f"branch {removal.branch!r} is protected"
    if not removal.repo_root:
        return "repository root is unknown"
    return None


def delete_branch(repo_root: str, branch: str, *, force: bool) -> subprocess.CompletedProcess:
    flag = "-D" if force else "-d"
    return subprocess.run(
        ["git", "-C", repo_root, "branch", flag, "--", branch],
        capture_output=True, text=True,
    )


def mark_handled(state_dir: Path, workspace_id: str) -> None:
    (state_dir / f"handled-{workspace_id}").touch()


def consume_handled(state_dir: Path, workspace_id: str) -> bool:
    marker = state_dir / f"handled-{workspace_id}"
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def prompt_delete(removal: Removal, config: Config,
                  read: Callable[[str], str], write: Callable[..., None]) -> bool:
    """Ask and act. Return True when the user must read the output before the popup closes."""
    write(f"Worktree removed. Delete branch {removal.branch!r} too?")
    write("  [y] delete (git branch -d, keeps unmerged)")
    write("  [f] force delete (git branch -D)")
    write("  [n] / Esc  keep branch")
    write("Press a key.")
    answer = read("").strip().lower()
    if answer not in ("y", "f"):
        write("branch kept")
        return False
    result = delete_branch(removal.repo_root, removal.branch, force=(answer == "f"))
    if result.returncode != 0:
        write(f"branch kept: {result.stderr.strip().splitlines()[0]}")
        return True
    write(f"deleted branch {removal.branch!r}")
    return False


def open_branch_popup(removal: Removal, env: dict[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    result = subprocess.run([
        env.get("HERDR_BIN_PATH", "herdr"), "plugin", "pane", "open",
        "--plugin", PLUGIN_ID, "--entrypoint", "branch", "--placement", "popup",
        "--width", "70", "--height", "10",
        "--env", f"PATH={env.get('PATH', '')}",
        "--env", f"BRANCH_REPO_ROOT={removal.repo_root}",
        "--env", f"BRANCH_NAME={removal.branch}", "--focus",
    ], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"cleanup-branch: could not open prompt: {result.stderr.strip()}", file=sys.stderr)


def cmd_prompt(env: dict[str, str]) -> int:
    removal = Removal(env["BRANCH_REPO_ROOT"], env["BRANCH_NAME"], True, False, False)
    config = load_config(Path(env.get("HERDR_PLUGIN_CONFIG_DIR", ".")))
    if prompt_delete(removal, config, lambda _: read_key(), print):
        print("press any key to close")
        read_key()
    return 0


def main(env: dict[str, str] | None = None,
         open_popup: Callable[[Removal], None] = open_branch_popup) -> int:
    env = os.environ if env is None else env
    try:
        removal = parse_removal(env.get("HERDR_PLUGIN_EVENT_JSON"), env.get("HERDR_PLUGIN_CONTEXT_JSON"))
        config = load_config(Path(env.get("HERDR_PLUGIN_CONFIG_DIR", ".")))
    except (ValueError, KeyError, json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"cleanup-branch: invalid input: {exc!r}", file=sys.stderr)
        return 1
    state_dir = env.get("HERDR_PLUGIN_STATE_DIR")
    if state_dir and removal.workspace_id and consume_handled(Path(state_dir), removal.workspace_id):
        print("cleanup-branch: skipped: handled by the remove-worktree action")
        return 0
    if not (config.auto or config.ask):
        print("cleanup-branch: skipped: auto and ask are disabled")
        return 0
    reason = skip_reason(removal, config.protected)
    if reason:
        print(f"cleanup-branch: skipped: {reason}")
        return 0
    if not config.auto:
        open_popup(removal)
        return 0
    result = delete_branch(removal.repo_root, removal.branch, force=config.force)
    if result.returncode != 0:
        # Typical cause: unmerged commits. Keep the branch and surface git's message.
        print(f"cleanup-branch: kept {removal.branch!r}: {result.stderr.strip()}", file=sys.stderr)
        return result.returncode
    print(f"cleanup-branch: {result.stdout.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(cmd_prompt(dict(os.environ)) if sys.argv[1:] == ["prompt"] else main())
