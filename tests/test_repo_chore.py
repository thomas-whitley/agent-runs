"""The repo chore executor, against a local bare repo standing in for GitHub
and a fake of GitHub's pull request API.

It clones, branches as agent/<run id>, lets the model rewrite whole files,
runs the repo's own test command, and on green pushes the branch and opens a
pull request. Three red attempts end the run failed with the diff and the
test output, and nothing is pushed. A branch already on the remote means a
worker got that far before it died, so the chore picks up from there.
"""

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from app.github import GitHubClient
from app.mercury_config import RepoConfig
from app.model import StubModel
from app.repo_chore import ChoreSetup, run_repo_chore
from app.runs import claim_run
from tests.github_fake import FakeGitHub

REPO = "owner/fixture"
WORKER = "worker-1"
TOKEN = "ghp_test_token_never_logged"
# The fixture's own test: it passes today, and fails until subtract exists
# once test_subtract.py is added.
CALC = "def add(a, b):\n    return a + b\n"
TEST_ADD = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
TEST_SUBTRACT = (
    "from calc import subtract\n\n\ndef test_subtract():\n    assert subtract(5, 3) == 2\n"
)
GOOD_CALC = CALC + "\n\ndef subtract(a, b):\n    return a - b\n"
BAD_CALC = CALC + "\n\ndef subtract(a, b):\n    return a + b\n"
TEST_COMMAND = "python -m pytest -q -p no:cacheprovider"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path) -> Path:
    """A bare repo at <tmp>/remotes/owner/fixture.git with calc.py and its test on main."""
    work = tmp_path / "seed"
    work.mkdir()
    git("init", "-q", "-b", "main", cwd=work)
    (work / "calc.py").write_text(CALC)
    (work / "test_calc.py").write_text(TEST_ADD)
    git("add", ".", cwd=work)
    git(
        "-c",
        "user.name=seed",
        "-c",
        "user.email=seed@example.com",
        "commit",
        "-qm",
        "seed",
        cwd=work,
    )
    bare = tmp_path / "remotes" / "owner" / "fixture.git"
    bare.parent.mkdir(parents=True)
    git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
    return bare


@pytest.fixture
def github():
    server = FakeGitHub()
    yield server
    server.close()


def setup(tmp_path, github, test_command: str = TEST_COMMAND) -> ChoreSetup:
    return ChoreSetup(
        repo=RepoConfig(name=REPO, test_command=test_command),
        clone_base=f"file://{tmp_path / 'remotes'}",
        github=GitHubClient(TOKEN, github.url),
        token=TOKEN,
        test_timeout_seconds=60,
    )


def chore_run(conn, instruction: str = "Add subtract to calc.py") -> str:
    run_id = str(
        conn.execute(
            "INSERT INTO runs (task, type, repo) VALUES (%s, 'repo_chore', %s) RETURNING id",
            (instruction, REPO),
        ).fetchone()[0]
    )
    assert claim_run(conn, run_id, WORKER)
    return run_id


def pick(*paths: str) -> str:
    return json.dumps({"read": list(paths)})


def edit(**files: str) -> str:
    return json.dumps(
        {"files": {name.replace("__", "."): body for name, body in files.items()}, "summary": "x"}
    )


def change(calc: str) -> str:
    return json.dumps(
        {"files": {"calc.py": calc, "test_subtract.py": TEST_SUBTRACT}, "summary": "Add subtract"}
    )


def remote_branches(bare: Path) -> list[str]:
    return git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=bare).split()


def done(conn, run_id: str) -> dict:
    return conn.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'done'", (run_id,)
    ).fetchone()[0]


def kinds(conn, run_id: str) -> list[str]:
    rows = conn.execute("SELECT kind FROM steps WHERE run_id = %s ORDER BY seq", (run_id,))
    return [row[0] for row in rows]


def test_a_green_change_is_pushed_to_its_branch_and_opened_as_a_pull_request(
    migrated_db, remote, github, tmp_path
):
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(GOOD_CALC)])

    result = run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    branch = f"agent/{run_id}"
    assert result.status == "succeeded"
    assert branch in remote_branches(remote)
    assert "def subtract" in git("show", f"{branch}:calc.py", cwd=remote)
    assert git("show", "main:calc.py", cwd=remote) == CALC.strip()
    [pull] = github.pulls
    assert (pull["head"], pull["base"]) == (branch, "main")
    assert done(migrated_db, run_id) == {
        "status": "succeeded",
        "pr_url": pull["html_url"],
    }
    assert kinds(migrated_db, run_id) == ["clone", "read", "edit", "test", "push", "pr", "done"]
    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("succeeded",)


def test_the_model_sees_the_tree_then_the_files_it_asked_for(migrated_db, remote, github, tmp_path):
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(GOOD_CALC)])

    run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    first, second = model.prompts
    assert "Add subtract to calc.py" in first
    assert "calc.py" in first and "test_calc.py" in first
    assert "def add" not in first
    assert CALC in second


def test_red_tests_are_shown_to_the_model_and_it_tries_again(migrated_db, remote, github, tmp_path):
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(BAD_CALC), change(GOOD_CALC)])

    result = run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    assert result.status == "succeeded"
    retry = model.prompts[2]
    assert "assert 8 == 2" in retry
    assert "return a + b" in retry
    assert kinds(migrated_db, run_id).count("test") == 2


def test_three_red_attempts_fail_the_run_with_the_diff_and_push_nothing(
    migrated_db, remote, github, tmp_path
):
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(BAD_CALC)])

    result = run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    assert result.status == "failed"
    assert remote_branches(remote) == ["main"]
    assert github.pulls == []
    output = done(migrated_db, run_id)
    assert output["status"] == "failed"
    assert "+def subtract(a, b):" in output["diff"]
    assert "test_subtract.py" in output["diff"]
    assert "assert 8 == 2" in output["test_output"]
    assert kinds(migrated_db, run_id).count("edit") == 3


def test_a_path_outside_the_repo_is_not_written(migrated_db, remote, github, tmp_path):
    run_id = chore_run(migrated_db)
    escape = json.dumps({"files": {"../escaped.py": "x = 1\n", ".git/config": ""}, "summary": ""})
    model = StubModel(replies=[pick("calc.py"), escape, change(GOOD_CALC)])

    result = run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    assert result.status == "succeeded"
    assert not list(tmp_path.rglob("escaped.py"))
    assert "not allowed" in model.prompts[2]


def test_only_the_files_the_model_wrote_are_committed(migrated_db, remote, github, tmp_path):
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(GOOD_CALC)])
    litter = f"{TEST_COMMAND} && touch build-output.txt"

    run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github, litter), worker_id=WORKER)

    files = git("ls-tree", "--name-only", f"agent/{run_id}", cwd=remote).split()
    assert sorted(files) == ["calc.py", "test_calc.py", "test_subtract.py"]


def test_the_test_command_never_sees_a_secret(migrated_db, remote, github, tmp_path, monkeypatch):
    monkeypatch.setenv("MERCURY_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret@db/x")
    seen = tmp_path / "env.txt"
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(GOOD_CALC)])

    run_repo_chore(
        migrated_db,
        run_id,
        model,
        setup(tmp_path, github, f"env > {seen} && {TEST_COMMAND}"),
        worker_id=WORKER,
    )

    env = seen.read_text()
    assert TOKEN not in env
    assert "DATABASE_URL" not in env
    assert "PATH=" in env


def test_the_token_is_sent_to_github_and_never_stored_in_a_step(
    migrated_db, remote, github, tmp_path
):
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(GOOD_CALC)])

    run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    assert all(header == f"Bearer {TOKEN}" for header in github.authorizations)
    stored = migrated_db.execute(
        "SELECT string_agg(output::text, ' ') FROM steps WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    assert TOKEN not in stored


def test_a_branch_already_on_the_remote_is_picked_up_without_the_model(
    migrated_db, remote, github, tmp_path
):
    run_id = chore_run(migrated_db)
    # A first worker pushed the green branch and died before the pull request.
    work = tmp_path / "first-worker"
    git("clone", "-q", str(remote), str(work), cwd=tmp_path)
    git("checkout", "-qb", f"agent/{run_id}", cwd=work)
    (work / "calc.py").write_text(GOOD_CALC)
    git("-c", "user.name=a", "-c", "user.email=a@example.com", "commit", "-qam", "x", cwd=work)
    git("push", "-q", "origin", f"agent/{run_id}", cwd=work)
    model = StubModel(replies=["never read"])

    result = run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    assert result.status == "succeeded"
    assert model.prompts == []
    [pull] = github.pulls
    assert pull["head"] == f"agent/{run_id}"
    clone_step = migrated_db.execute(
        "SELECT output FROM steps WHERE run_id = %s AND kind = 'clone'", (run_id,)
    ).fetchone()[0]
    assert clone_step["resumed"] is True


def test_a_pull_request_already_open_is_not_opened_twice(migrated_db, remote, github, tmp_path):
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(GOOD_CALC)])
    run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)
    # The worker died after opening it and before closing the run.
    migrated_db.execute(
        "UPDATE runs SET status = 'running', finished_at = NULL WHERE id = %s", (run_id,)
    )

    run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    assert len(github.pulls) == 1


def test_a_chore_stops_at_its_token_budget(migrated_db, remote, github, tmp_path):
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(BAD_CALC)], tokens_per_reply=30_000)

    result = run_repo_chore(
        migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER, token_budget=50_000
    )

    assert result.status == "budget_exhausted"
    assert remote_branches(remote) == ["main"]
    assert done(migrated_db, run_id)["status"] == "budget_exhausted"


def worker_settings(tmp_path, github, monkeypatch):
    from app.config import load_settings

    monkeypatch.setenv("DATABASE_URL", migrated_url(tmp_path))
    monkeypatch.setenv("WORKER_ID", WORKER)
    monkeypatch.setenv("GITHUB_API_URL", github.url)
    monkeypatch.setenv("GITHUB_CLONE_BASE", f"file://{tmp_path / 'remotes'}")
    monkeypatch.setenv("MERCURY_GITHUB_TOKEN", TOKEN)
    return load_settings()


def migrated_url(tmp_path) -> str:
    return tmp_path.joinpath(".db-url").read_text()


def test_the_worker_runs_a_claimed_chore(
    migrated_db, clean_db, remote, github, tmp_path, monkeypatch
):
    from app.worker import process_run

    tmp_path.joinpath(".db-url").write_text(clean_db)
    settings = worker_settings(tmp_path, github, monkeypatch)
    run_id = chore_run(migrated_db)
    model = StubModel(replies=[pick("calc.py"), change(GOOD_CALC)])

    result = process_run(
        migrated_db,
        run_id,
        settings,
        model_builder=lambda settings, provider: model,
        repos=(RepoConfig(name=REPO, test_command=TEST_COMMAND),),
    )

    assert result.status == "succeeded"
    assert len(github.pulls) == 1


def test_the_worker_refuses_a_chore_whose_repo_left_the_config(
    migrated_db, clean_db, remote, github, tmp_path, monkeypatch
):
    from app.worker import process_run

    tmp_path.joinpath(".db-url").write_text(clean_db)
    settings = worker_settings(tmp_path, github, monkeypatch)
    run_id = chore_run(migrated_db)

    result = process_run(
        migrated_db,
        run_id,
        settings,
        model_builder=lambda settings, provider: StubModel(replies=["{}"]),
        repos=(),
    )

    assert result is None
    status = migrated_db.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
    assert status == ("refused",)
    assert remote_branches(remote) == ["main"]


# Open it anyway: a failed chore offers a button, and pressing it creates a
# run that reapplies the failed diff and opens the pull request regardless.

CHAT = 42


def failed_chore(conn, remote, github, tmp_path) -> str:
    run_id = chore_run(conn)
    model = StubModel(replies=[pick("calc.py"), change(BAD_CALC)])
    assert (
        run_repo_chore(conn, run_id, model, setup(tmp_path, github), worker_id=WORKER).status
        == "failed"
    )
    return run_id


def anyway_run(conn, source_run_id: str) -> str:
    run_id = str(
        conn.execute(
            "INSERT INTO runs (task, type, repo, source_run_id) "
            "SELECT task, 'repo_chore', repo, id FROM runs WHERE id = %s RETURNING id",
            (source_run_id,),
        ).fetchone()[0]
    )
    assert claim_run(conn, run_id, WORKER)
    return run_id


def test_open_anyway_pushes_the_failed_diff_and_says_the_tests_failed(
    migrated_db, remote, github, tmp_path
):
    failed = failed_chore(migrated_db, remote, github, tmp_path)
    run_id = anyway_run(migrated_db, failed)
    model = StubModel(replies=["never read"])

    result = run_repo_chore(migrated_db, run_id, model, setup(tmp_path, github), worker_id=WORKER)

    assert result.status == "succeeded"
    assert model.prompts == []
    branch = f"agent/{run_id}"
    assert "return a + b" in git("show", f"{branch}:calc.py", cwd=remote)
    assert "test_subtract.py" in git("ls-tree", "--name-only", branch, cwd=remote).split()
    [pull] = github.pulls
    assert pull["head"] == branch
    assert "failed" in pull["body"]
    assert failed in pull["body"]
    assert kinds(migrated_db, run_id) == ["clone", "apply", "push", "pr", "done"]


def test_a_failed_chore_from_telegram_offers_open_anyway(
    migrated_db, clean_db, remote, github, fake_telegram, tmp_path, monkeypatch
):
    from app.worker import process_run

    tmp_path.joinpath(".db-url").write_text(clean_db)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_API_URL", fake_telegram.url)
    settings = worker_settings(tmp_path, github, monkeypatch)
    run_id = chore_run(migrated_db)
    migrated_db.execute(
        "UPDATE runs SET telegram_chat_id = %s, telegram_message_id = 7 WHERE id = %s",
        (CHAT, run_id),
    )
    model = StubModel(replies=[pick("calc.py"), change(BAD_CALC)])

    process_run(
        migrated_db,
        run_id,
        settings,
        model_builder=lambda settings, provider: model,
        repos=(RepoConfig(name=REPO, test_command=TEST_COMMAND),),
    )

    [question] = fake_telegram.sent()
    [[button, _]] = question["reply_markup"]["inline_keyboard"]
    assert button["text"] == "Open it anyway"
    row = migrated_db.execute("SELECT action, run_id FROM approvals").fetchone()
    assert row == ("open_anyway", uuid.UUID(run_id))


def test_a_succeeded_chore_offers_nothing(
    migrated_db, clean_db, remote, github, fake_telegram, tmp_path, monkeypatch
):
    from app.worker import process_run

    tmp_path.joinpath(".db-url").write_text(clean_db)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_API_URL", fake_telegram.url)
    settings = worker_settings(tmp_path, github, monkeypatch)
    run_id = chore_run(migrated_db)
    migrated_db.execute(
        "UPDATE runs SET telegram_chat_id = %s, telegram_message_id = 7 WHERE id = %s",
        (CHAT, run_id),
    )
    model = StubModel(replies=[pick("calc.py"), change(GOOD_CALC)])

    process_run(
        migrated_db,
        run_id,
        settings,
        model_builder=lambda settings, provider: model,
        repos=(RepoConfig(name=REPO, test_command=TEST_COMMAND),),
    )

    assert fake_telegram.sent() == []
    assert migrated_db.execute("SELECT count(*) FROM approvals").fetchone() == (0,)


# In the image the worker is root, and a test command running as the worker's
# user could read its keys from /proc/<pid>/environ. So as root the command
# runs as the unprivileged chore user, and with no such user it does not run.


class _FakeProcess:
    returncode = 0
    pid = 1

    def communicate(self, timeout=None):
        return "ok", None


def test_as_root_the_test_command_runs_as_the_chore_user(tmp_path, monkeypatch):
    import pwd

    from app import repo_chore

    clone = tmp_path / "repo"
    clone.mkdir()
    (clone / "calc.py").write_text(CALC)
    chowned, popen_kwargs = [], {}
    monkeypatch.setattr(repo_chore.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        repo_chore.pwd,
        "getpwnam",
        lambda name: pwd.struct_passwd((name, "x", 4321, 4321, "", "/", "/bin/sh")),
    )
    monkeypatch.setattr(
        repo_chore.os, "chown", lambda path, uid, gid, **kwargs: chowned.append((str(path), uid))
    )

    def fake_popen(args, **kwargs):
        popen_kwargs.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(repo_chore.subprocess, "Popen", fake_popen)
    chore = setup(tmp_path, github=type("G", (), {"url": "http://unused"})())

    passed, _, _ = repo_chore._run_tests(clone, chore, tmp_path)

    assert passed
    assert (popen_kwargs["user"], popen_kwargs["group"]) == (4321, 4321)
    assert popen_kwargs["extra_groups"] == []
    assert (str(clone / "calc.py"), 4321) in chowned
    assert (str(tmp_path / "home"), 4321) in chowned


def test_as_root_with_no_chore_user_the_tests_do_not_run(tmp_path, monkeypatch):
    from app import repo_chore

    clone = tmp_path / "repo"
    clone.mkdir()

    def no_such_user(name):
        raise KeyError(name)

    monkeypatch.setattr(repo_chore.os, "geteuid", lambda: 0)
    monkeypatch.setattr(repo_chore.pwd, "getpwnam", no_such_user)
    monkeypatch.setattr(repo_chore.subprocess, "Popen", lambda *a, **k: pytest.fail("ran as root"))
    chore = setup(tmp_path, github=type("G", (), {"url": "http://unused"})())

    with pytest.raises(repo_chore.ChoreError, match="chore user"):
        repo_chore._run_tests(clone, chore, tmp_path)
