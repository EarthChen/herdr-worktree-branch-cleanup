import json
import subprocess
from pathlib import Path

import pytest

import cleanup_branch as cb
from cleanup_branch import Config


def event_json(*, branch="feat-x", linked=True, bare=False, detached=False,
               repo_root="/repo", workspace=True, forced=False):
    data = {
        "type": "worktree_removed",
        "workspace_id": "w1",
        "workspace": None,
        "worktree": {
            "path": "/wt/feat-x", "branch": branch, "is_bare": bare,
            "is_detached": detached, "is_prunable": False,
            "is_linked_worktree": linked, "label": "repo",
        },
        "forced": forced,
    }
    if workspace:
        data["workspace"] = {"workspace_id": "w1", "worktree": {
            "repo_root": repo_root, "checkout_path": "/wt/feat-x",
            "is_linked_worktree": linked, "repo_key": "k", "repo_name": "repo"}}
    return json.dumps({"event": "worktree_removed", "data": data})


def context_json(repo_root="/ctx-repo"):
    return json.dumps({"worktree": {"repo_root": repo_root}})


# --- parse_removal -------------------------------------------------------

def test_parse_reads_branch_and_repo_root_from_event():
    r = cb.parse_removal(event_json(), None)
    assert (r.branch, r.repo_root) == ("feat-x", "/repo")


def test_parse_falls_back_to_context_when_workspace_is_null():
    # herdr sends workspace=null when the workspace is already gone;
    # the invocation context still carries repo_root.
    r = cb.parse_removal(event_json(workspace=False), context_json())
    assert r.repo_root == "/ctx-repo"


def test_parse_without_any_repo_root_leaves_it_unset():
    assert cb.parse_removal(event_json(workspace=False), None).repo_root is None


def test_parse_rejects_missing_event_payload():
    with pytest.raises(ValueError):
        cb.parse_removal(None, None)


# --- skip_reason ---------------------------------------------------------

PROTECTED = frozenset({"main"})


def reason(**kw):
    return cb.skip_reason(cb.parse_removal(event_json(**kw), None), PROTECTED)


def test_regular_linked_worktree_is_not_skipped():
    assert reason() is None


@pytest.mark.parametrize("kw", [
    {"branch": None}, {"detached": True}, {"bare": True}, {"linked": False},
    {"branch": "main"}, {"repo_root": None},
])
def test_unsafe_removals_are_skipped_with_a_reason(kw):
    # Primary checkout, detached HEAD and protected names must never lose a branch.
    assert reason(**kw)


# --- delete_branch (real git) -------------------------------------------

def git(repo: Path, *args: str):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
        "--allow-empty", "-m", "init")
    return r


def commit_on(repo: Path, branch: str):
    git(repo, "checkout", "-q", "-b", branch)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
        "--allow-empty", "-m", "work")
    git(repo, "checkout", "-q", "main")


def branches(repo: Path):
    return git(repo, "branch", "--format=%(refname:short)").split()


def test_merged_branch_is_deleted(repo):
    git(repo, "branch", "feat-x")
    assert cb.delete_branch(str(repo), "feat-x", force=False).returncode == 0
    assert "feat-x" not in branches(repo)


def test_unmerged_branch_survives_without_force(repo):
    # Unmerged work is the one thing a worktree removal must not destroy silently.
    commit_on(repo, "feat-x")
    assert cb.delete_branch(str(repo), "feat-x", force=False).returncode != 0
    assert "feat-x" in branches(repo)


def test_unmerged_branch_is_deleted_with_force(repo):
    commit_on(repo, "feat-x")
    assert cb.delete_branch(str(repo), "feat-x", force=True).returncode == 0
    assert "feat-x" not in branches(repo)


# --- load_config ---------------------------------------------------------

def test_config_defaults_when_file_is_absent(tmp_path):
    c = cb.load_config(tmp_path)
    assert c.force is False and "main" in c.protected


def test_config_file_overrides_defaults(tmp_path):
    (tmp_path / "config.toml").write_text('force = true\nprotected = ["release"]\n')
    c = cb.load_config(tmp_path)
    assert c.force is True and c.protected == frozenset({"release"})


# --- main: auto / ask ----------------------------------------------------

class Popup:
    """Records open_popup calls instead of touching herdr."""
    def __init__(self):
        self.calls = []

    def __call__(self, removal):
        self.calls.append(removal)


def hook_env(repo, tmp_path, config="", **ev):
    cfg, state = tmp_path / "cfg", tmp_path / "state"
    cfg.mkdir(exist_ok=True)
    state.mkdir(exist_ok=True)
    (cfg / "config.toml").write_text(config)
    return {"HERDR_PLUGIN_EVENT_JSON": event_json(repo_root=str(repo), **ev),
            "HERDR_PLUGIN_CONFIG_DIR": str(cfg), "HERDR_PLUGIN_STATE_DIR": str(state)}


def test_hook_asks_the_user_by_default_and_deletes_nothing_itself(repo, tmp_path):
    # Native removal gives no branch choice, so the hook asks right after it.
    git(repo, "branch", "feat-x")
    popup = Popup()
    assert cb.main(hook_env(repo, tmp_path), popup) == 0
    assert [r.branch for r in popup.calls] == ["feat-x"]
    assert "feat-x" in branches(repo)


def test_hook_is_silent_when_ask_and_auto_are_off(repo, tmp_path):
    popup = Popup()
    cb.main(hook_env(repo, tmp_path, "ask = false\n"), popup)
    assert popup.calls == []


def test_hook_does_not_ask_about_unsafe_removals(repo, tmp_path):
    # Asking about main or a detached worktree only invites a mistake.
    popup = Popup()
    cb.main(hook_env(repo, tmp_path, branch="main"), popup)
    cb.main(hook_env(repo, tmp_path, detached=True, branch=None), popup)
    assert popup.calls == []


def test_hook_deletes_without_asking_when_auto_is_enabled(repo, tmp_path):
    git(repo, "branch", "feat-x")
    popup = Popup()
    assert cb.main(hook_env(repo, tmp_path, "auto = true\n"), popup) == 0
    assert "feat-x" not in branches(repo) and popup.calls == []


def test_hook_skips_a_removal_the_action_already_handled(repo, tmp_path):
    # The remove-worktree action asked its own question; asking twice is noise.
    env = hook_env(repo, tmp_path)
    cb.mark_handled(Path(env["HERDR_PLUGIN_STATE_DIR"]), "w1")
    popup = Popup()
    cb.main(env, popup)
    assert popup.calls == []
    cb.main(env, popup)  # the marker is single-use
    assert len(popup.calls) == 1


# --- prompt_delete -------------------------------------------------------

def removal_for(repo):
    return cb.Removal(str(repo), "feat-x", True, False, False)


@pytest.mark.parametrize("answer,deleted", [("y", True), ("f", True), ("n", False), ("", False), ("zzz", False)])
def test_prompt_deletes_only_on_an_explicit_answer(repo, answer, deleted):
    git(repo, "branch", "feat-x")
    cb.prompt_delete(removal_for(repo), Config(), lambda _: answer, lambda *_: None)
    assert ("feat-x" not in branches(repo)) is deleted


def test_prompt_y_keeps_unmerged_branch_and_f_force_deletes_it(repo):
    commit_on(repo, "feat-x")
    cb.prompt_delete(removal_for(repo), cb.Config(), lambda _: "y", lambda *_: None)
    assert "feat-x" in branches(repo)
    cb.prompt_delete(removal_for(repo), cb.Config(), lambda _: "f", lambda *_: None)
    assert "feat-x" not in branches(repo)


# --- prompt_delete: when the popup may close -----------------------------

def test_prompt_reports_no_attention_needed_after_a_successful_delete(repo):
    git(repo, "branch", "feat-x")
    assert cb.prompt_delete(removal_for(repo), Config(), lambda _: "y", lambda *_: None) is False


def test_prompt_reports_no_attention_needed_when_the_user_keeps_the_branch(repo):
    git(repo, "branch", "feat-x")
    assert cb.prompt_delete(removal_for(repo), Config(), lambda _: "n", lambda *_: None) is False


def test_prompt_asks_for_attention_when_git_refuses(repo):
    # A refusal (unmerged work) is the one outcome the user must read before the popup closes.
    commit_on(repo, "feat-x")
    assert cb.prompt_delete(removal_for(repo), Config(), lambda _: "y", lambda *_: None) is True
