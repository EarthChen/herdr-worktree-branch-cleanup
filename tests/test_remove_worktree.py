import subprocess
from pathlib import Path

import pytest

import remove_worktree as rw
from cleanup_branch import Config

WS = "w9"


class FakeHerdr:
    """Records CLI calls and replays canned JSON results."""

    def __init__(self, repo_root, branch="feat-x", linked=True, detached=False, remove_error=None):
        self.calls = []
        self.repo_root, self.branch, self.linked = repo_root, branch, linked
        self.detached, self.remove_error = detached, remove_error

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("workspace", "get"):
            return {"workspace": {"label": "feat-x", "worktree": {
                "repo_root": self.repo_root, "checkout_path": "/wt/feat-x",
                "is_linked_worktree": self.linked}}}
        if args[:2] == ("worktree", "list"):
            return {"worktrees": [
                {"path": "/repo", "branch": "main", "open_workspace_id": None,
                 "is_detached": False, "is_bare": False, "is_linked_worktree": False, "label": "r"},
                {"path": "/wt/feat-x", "branch": self.branch, "open_workspace_id": WS,
                 "is_detached": self.detached, "is_bare": False,
                 "is_linked_worktree": self.linked, "label": "r"}]}
        if args[:2] == ("worktree", "remove"):
            if self.remove_error:
                raise rw.HerdrError(self.remove_error)
            return {"type": "worktree_removed"}
        raise AssertionError(args)

    def removed(self):
        return any(c[:2] == ("worktree", "remove") for c in self.calls)


def git(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i")
    git(r, "branch", "feat-x")
    return r


def branches(repo):
    return git(repo, "branch", "--format=%(refname:short)").split()


# --- resolve_target ------------------------------------------------------

def test_resolve_target_picks_the_worktree_open_in_the_workspace(repo):
    t = rw.resolve_target(FakeHerdr(str(repo)), WS)
    assert (t.branch, t.path, t.repo_root) == ("feat-x", "/wt/feat-x", str(repo))


def test_resolve_target_refuses_the_primary_checkout(repo):
    # Removing the main checkout through this action must be impossible.
    with pytest.raises(rw.NotRemovable):
        rw.resolve_target(FakeHerdr(str(repo), linked=False), WS)


# --- choose --------------------------------------------------------------

@pytest.mark.parametrize("answer,expected", [
    ("w", "worktree"), ("b", "branch"), ("f", "force"), ("q", "cancel"),
    ("", "cancel"), ("\x1b", "cancel"), ("B", "cancel"), ("zzz", "cancel"),
])
def test_choose_maps_keys_and_defaults_to_cancel(answer, expected):
    # An unrecognised answer must never destroy anything.
    assert rw.choose(lambda _: answer, lambda *_: None, "feat-x") == expected


# --- execute -------------------------------------------------------------

def run(repo, choice, **kw):
    herdr = FakeHerdr(str(repo), **kw)
    target = rw.resolve_target(herdr, WS)
    return herdr, rw.execute(choice, target, herdr, Config())


def test_cancel_touches_nothing(repo):
    herdr, _ = run(repo, "cancel")
    assert not herdr.removed() and "feat-x" in branches(repo)


def test_worktree_only_keeps_branch(repo):
    herdr, _ = run(repo, "worktree")
    assert herdr.removed() and "feat-x" in branches(repo)


def test_branch_choice_removes_worktree_then_deletes_merged_branch(repo):
    herdr, _ = run(repo, "branch")
    assert herdr.removed() and "feat-x" not in branches(repo)


def test_failed_worktree_removal_keeps_the_branch(repo):
    # Branch deletion after a failed removal would leave a live worktree without its branch.
    herdr, report = run(repo, "force", remove_error="worktree has uncommitted changes")
    assert "feat-x" in branches(repo)
    assert "uncommitted" in " ".join(report)


def test_protected_branch_is_never_deleted_even_when_chosen(repo):
    herdr, _ = run(repo, "force", branch="main")
    assert "main" in branches(repo)


def test_unmerged_branch_survives_safe_choice_and_is_reported(repo):
    git(repo, "checkout", "-q", "feat-x")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "w")
    git(repo, "checkout", "-q", "main")
    _, report = run(repo, "branch")
    assert "feat-x" in branches(repo)
    assert "not fully merged" in " ".join(report)


def test_force_choice_deletes_unmerged_branch(repo):
    git(repo, "checkout", "-q", "feat-x")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "w")
    git(repo, "checkout", "-q", "main")
    run(repo, "force")
    assert "feat-x" not in branches(repo)


def test_apply_marks_the_workspace_so_the_hook_stays_quiet(repo, tmp_path):
    # apply removes the worktree, which fires the hook; the marker must exist by then.
    import cleanup_branch as cb
    state = tmp_path / "state"
    state.mkdir()
    herdr = FakeHerdr(str(repo))
    rw.execute("worktree", rw.resolve_target(herdr, WS), herdr, Config(), state_dir=state)
    assert cb.consume_handled(state, WS) is True


def test_failed_removal_leaves_no_marker(repo, tmp_path):
    # No event fires on failure; a stale marker would silence the next real removal.
    import cleanup_branch as cb
    state = tmp_path / "state"
    state.mkdir()
    herdr = FakeHerdr(str(repo), remove_error="dirty")
    rw.execute("worktree", rw.resolve_target(herdr, WS), herdr, Config(), state_dir=state)
    assert cb.consume_handled(state, WS) is False
