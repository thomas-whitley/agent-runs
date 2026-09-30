"""The repo_chore executor, per docs/mercury.md.

A chore clones the repo into a temporary directory, branches as
agent/<run id>, lets the model rewrite whole files, and runs the repo's own
test command. Green pushes the branch and opens a pull request. Three red
attempts end the run failed, with the diff and the test output in its done
event, and nothing leaves the machine. The runner never merges and never
pushes to anything but its own branch.

The branch reaches the remote only after the tests pass, so a branch already
there means a worker got that far and then died. A takeover then skips the
model and the tests and only makes sure the pull request exists.

The test command runs in a subprocess with a timeout and an environment that
holds no secret, only PATH, a throwaway HOME and the locale. In the image the
worker is root, so the command runs as the unprivileged chore user instead,
which cannot read the worker's environment through /proc. It has network,
because a repo's tests need their dependencies, so it is not isolation. The
GitHub token reaches git only as an HTTP header in git's own environment, so
it is never written to .git/config, never in a URL and never in a step.
"""

import base64
import json
import logging
import os
import pwd
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import psycopg

from app.loop import (
    DEFAULT_MODEL_RETRY_ATTEMPTS,
    DEFAULT_MODEL_RETRY_BACKOFF_SECONDS,
    LoopResult,
    _complete_with_retry,
)
from app.mercury_config import RepoConfig
from app.model import Model
from app.runs import finish_run, heartbeat, record_step

logger = logging.getLogger("agent_runs.repo_chore")

MAX_ATTEMPTS = 3
MAX_TREE_ENTRIES = 500
MAX_FILES_READ = 10
MAX_FILE_CHARS = 20_000
MAX_OUTPUT_CHARS = 4_000
MAX_DIFF_CHARS = 20_000
HEARTBEAT_SECONDS = 30.0
GIT_TIMEOUT_SECONDS = 120.0
GITHUB_TIMEOUT_SECONDS = 20.0
AUTHOR = ("mercury", "mercury@users.noreply.github.com")
# Created in the Dockerfile. The test command runs as this user when the
# worker is root, as it is in the image.
CHORE_USER = "chore"

SYSTEM = """You change a git repository to carry out one instruction from its owner. \
Answer with a single JSON object and nothing else."""


class ChoreError(Exception):
    """git or GitHub refused. The message never carries the token."""


class LostLease(Exception):
    """Another worker owns the run now, so this one stops writing."""


class GitHubClient:
    """The two pull request calls a chore makes, with the token as a header."""

    def __init__(self, token: str | None, api_url: str = "https://api.github.com") -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")

    def find_pull(self, repo: str, branch: str) -> str | None:
        owner = repo.split("/", 1)[0]
        pulls = self._call("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}")
        return pulls[0]["html_url"] if pulls else None

    def open_pull(self, repo: str, branch: str, base: str, title: str, body: str) -> str:
        pull = self._call(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        return pull["html_url"]

    def _call(self, method: str, path: str, payload: dict | None = None):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "mercury"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self._api_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            raise ChoreError(f"GitHub {method} {path.split('?')[0]}: HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise ChoreError(f"GitHub {method}: {type(error).__name__}") from None


@dataclass(frozen=True)
class ChoreSetup:
    repo: RepoConfig
    clone_base: str  # https://github.com, or file:///... in the tests
    github: GitHubClient
    token: str | None
    test_timeout_seconds: float
    # A second connection for the heartbeat while a slow test command runs.
    # None in tests that do not need it.
    heartbeat_database_url: str | None = None


def run_repo_chore(
    conn: psycopg.Connection,
    run_id: str,
    model: Model,
    setup: ChoreSetup,
    *,
    worker_id: str | None = None,
    token_budget: int = 50_000,
    on_step: Callable[[], bool] | None = None,
    retry_attempts: int = DEFAULT_MODEL_RETRY_ATTEMPTS,
    retry_backoff_seconds: float = DEFAULT_MODEL_RETRY_BACKOFF_SECONDS,
) -> LoopResult:
    instruction, tokens_used, source_run_id = conn.execute(
        "SELECT task, tokens_used, source_run_id FROM runs WHERE id = %s", (run_id,)
    ).fetchone()
    # Open it anyway: the diff of the failed run this one was created from.
    source = None
    if source_run_id is not None:
        row = conn.execute(
            "SELECT output FROM steps WHERE run_id = %s AND kind = 'done'", (source_run_id,)
        ).fetchone()
        source = (str(source_run_id), (row[0] or {}).get("diff", "") if row else "")
    # A takeover continues the step numbering rather than colliding with it.
    seq = conn.execute(
        "SELECT coalesce(max(seq), 0) FROM steps WHERE run_id = %s", (run_id,)
    ).fetchone()[0]
    fields = {"run_id": run_id, "worker_id": worker_id}
    state = {"seq": seq, "tokens": tokens_used, "attempts": 0}

    def write(kind: str, output: dict, tokens: int = 0) -> None:
        state["seq"] += 1
        if not record_step(
            conn, run_id, state["seq"], kind, output=output, tokens=tokens, worker_id=worker_id
        ):
            raise LostLease
        if on_step is not None and on_step() is False:
            raise LostLease

    def close(status: str, output: dict) -> LoopResult:
        write("done", {"status": status, **output})
        if not finish_run(conn, run_id, status, state["tokens"], worker_id=worker_id):
            return LoopResult(
                status="lost", attempts=state["attempts"], tokens_used=state["tokens"]
            )
        logger.info("repo chore %s ended %s", run_id, status, extra=fields)
        return LoopResult(status=status, attempts=state["attempts"], tokens_used=state["tokens"])

    def ask(prompt: str) -> dict | None:
        reply = _complete_with_retry(model, SYSTEM, prompt, retry_attempts, retry_backoff_seconds)
        state["tokens"] += reply.tokens
        state["last_tokens"] = reply.tokens
        return _parse(reply.text)

    workdir = Path(tempfile.mkdtemp(prefix="chore-"))
    keepalive = _Keepalive(setup.heartbeat_database_url, run_id, worker_id)
    try:
        with keepalive:
            if source is not None:
                return _open_anyway(setup, run_id, instruction, workdir, write, close, source)
            return _run(setup, run_id, instruction, workdir, write, close, ask, state, token_budget)
    except LostLease:
        logger.warning("repo chore %s: lease lost, stopping", run_id, extra=fields)
        return LoopResult(status="lost", attempts=state["attempts"], tokens_used=state["tokens"])
    except RuntimeError as error:
        # The model never answered, after its retries.
        logger.error("repo chore %s: %s", run_id, error, extra=fields)
        return close("error", {"reason": "the model did not answer"})
    except ChoreError as error:
        logger.error("repo chore %s: %s", run_id, error, extra=fields)
        return close("error", {"reason": str(error)})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _run(setup, run_id, instruction, workdir, write, close, ask, state, token_budget):
    git = _Git(workdir, setup)
    branch = f"agent/{run_id}"
    url = f"{setup.clone_base.rstrip('/')}/{setup.repo.name}.git"
    base = git.default_branch(url)
    clone = workdir / "repo"

    if git.remote_has_branch(url, branch):
        git.run("clone", "-q", "--branch", branch, url, str(clone), cwd=workdir)
        write("clone", {"resumed": True, "base": base})
        return _open_pull(setup, run_id, instruction, branch, base, write, close)

    git.run("clone", "-q", url, str(clone), cwd=workdir)
    git.run("checkout", "-q", "-b", branch, cwd=clone)
    write("clone", {"resumed": False, "base": base})

    tree = git.run("ls-files", cwd=clone).splitlines()
    listing = "\n".join(tree[:MAX_TREE_ENTRIES])
    if len(tree) > MAX_TREE_ENTRIES:
        listing += f"\n... and {len(tree) - MAX_TREE_ENTRIES} more"
    wanted = ask(
        f"Instruction:\n{instruction}\n\nFiles in the repository:\n{listing}\n\n"
        f'Reply {{"read": ["path", ...]}} naming up to {MAX_FILES_READ} files you need to see.'
    )
    paths = [p for p in (wanted or {}).get("read") or [] if isinstance(p, str)]
    shown = _read_files(clone, paths[:MAX_FILES_READ])
    write("read", {"files": sorted(shown)}, tokens=state["last_tokens"])
    if state["tokens"] >= token_budget:
        return close("budget_exhausted", {"reason": "token budget spent"})

    files_block = "\n\n".join(f"=== {path} ===\n{body}" for path, body in shown.items())
    prompt = (
        f"Instruction:\n{instruction}\n\nFiles in the repository:\n{listing}\n\n"
        f"Contents:\n{files_block or '(none)'}\n\n"
        'Reply {"files": {"path": "the full new contents"}, "summary": "one line"}, '
        "including only files you change or create."
    )
    written: set[str] = set()
    test_output = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        state["attempts"] = attempt
        reply = ask(prompt)
        files = (reply or {}).get("files")
        refused = _refused_paths(files)
        if not isinstance(files, dict) or not files or refused:
            problem = (
                f"These paths are not allowed: {', '.join(refused)}. Stay inside the repository "
                "and out of .git."
                if refused
                else "Your reply was not the JSON asked for."
            )
            write(
                "edit", {"attempt": attempt, "files": [], "problem": problem}, state["last_tokens"]
            )
            if state["tokens"] >= token_budget:
                return close("budget_exhausted", {"reason": "token budget spent"})
            prompt = f"{prompt}\n\nYour last reply could not be used. {problem}"
            continue

        for path, body in files.items():
            target = clone / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body if isinstance(body, str) else "")
            written.add(path)
        write("edit", {"attempt": attempt, "files": sorted(files)}, state["last_tokens"])
        if state["tokens"] >= token_budget:
            return close("budget_exhausted", {"reason": "token budget spent"})

        passed, exit_code, test_output = _run_tests(clone, setup, workdir)
        write("test", {"attempt": attempt, "passed": passed, "exit_code": exit_code})
        if passed:
            break
        current = "\n\n".join(
            f"=== {path} ===\n{(clone / path).read_text()}" for path in sorted(written)
        )
        prompt = (
            f"Instruction:\n{instruction}\n\nFiles in the repository:\n{listing}\n\n"
            f"Your change so far:\n{current}\n\n"
            f"`{setup.repo.test_command}` failed. Its output ended:\n{test_output}\n\n"
            'Reply {"files": {"path": "the full new contents"}, "summary": "one line"} '
            "with the corrected files."
        )
    else:
        git.run("add", "--", *sorted(written), cwd=clone) if written else None
        diff = git.run("diff", "--cached", cwd=clone) if written else ""
        return close(
            "failed",
            {
                "reason": f"tests still failing after {MAX_ATTEMPTS} attempts",
                "diff": diff[:MAX_DIFF_CHARS],
                "test_output": test_output,
            },
        )

    git.run("add", "--", *sorted(written), cwd=clone)
    git.run(
        "-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}",
        "commit", "-q", "-m", _title(instruction), cwd=clone,
    )  # fmt: skip
    git.run("push", "-q", "origin", branch, cwd=clone, remote=True)
    write("push", {"branch": branch})
    return _open_pull(setup, run_id, instruction, branch, base, write, close)


def _open_anyway(setup, run_id, instruction, workdir, write, close, source) -> LoopResult:
    """Reapply the failed run's diff on this run's own branch and open the
    pull request, saying plainly that the tests failed. No model call."""
    source_run_id, diff = source
    git = _Git(workdir, setup)
    branch = f"agent/{run_id}"
    url = f"{setup.clone_base.rstrip('/')}/{setup.repo.name}.git"
    base = git.default_branch(url)
    clone = workdir / "repo"
    note = (
        f"`{setup.repo.test_command}` failed on this change in Mercury run `{source_run_id}`. "
        "It was opened anyway on request, from that run's diff."
    )

    if git.remote_has_branch(url, branch):
        git.run("clone", "-q", "--branch", branch, url, str(clone), cwd=workdir)
        write("clone", {"resumed": True, "base": base})
        return _open_pull(setup, run_id, instruction, branch, base, write, close, note)

    if not diff.strip():
        raise ChoreError(f"run {source_run_id} left no diff to open")
    git.run("clone", "-q", url, str(clone), cwd=workdir)
    git.run("checkout", "-q", "-b", branch, cwd=clone)
    write("clone", {"resumed": False, "base": base})
    patch = workdir / "change.diff"
    patch.write_text(diff if diff.endswith("\n") else diff + "\n")
    try:
        git.run("apply", "--index", str(patch), cwd=clone)
    except ChoreError:
        raise ChoreError(f"the diff from run {source_run_id} no longer applies") from None
    files = git.run("diff", "--cached", "--name-only", cwd=clone).splitlines()
    write("apply", {"source_run": source_run_id, "files": files})
    git.run(
        "-c", f"user.name={AUTHOR[0]}", "-c", f"user.email={AUTHOR[1]}",
        "commit", "-q", "-m", _title(instruction), cwd=clone,
    )  # fmt: skip
    git.run("push", "-q", "origin", branch, cwd=clone, remote=True)
    write("push", {"branch": branch})
    return _open_pull(setup, run_id, instruction, branch, base, write, close, note)


def _open_pull(setup, run_id, instruction, branch, base, write, close, note=None) -> LoopResult:
    url = setup.github.find_pull(setup.repo.name, branch)
    if url is None:
        note = note or f"`{setup.repo.test_command}` passed."
        body = (
            f"{instruction}\n\nOpened by Mercury run `{run_id}`. {note} "
            "It is never merged by the runner."
        )
        url = setup.github.open_pull(setup.repo.name, branch, base, _title(instruction), body)
    write("pr", {"url": url})
    return close("succeeded", {"pr_url": url})


def _title(instruction: str) -> str:
    first = instruction.strip().splitlines()[0] if instruction.strip() else "Repo chore"
    return first[:72]


_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _parse(text: str) -> dict | None:
    match = _OBJECT.search(text)
    if match is None:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _refused_paths(files) -> list[str]:
    if not isinstance(files, dict):
        return []
    refused = []
    for path in files:
        parts = PurePosixPath(path).parts if isinstance(path, str) else ()
        if (
            not parts
            or PurePosixPath(path).is_absolute()
            or ".." in parts
            or parts[0] == ".git"
            or "\\" in path
        ):
            refused.append(str(path))
    return refused


def _read_files(clone: Path, paths: list[str]) -> dict[str, str]:
    shown = {}
    for path in paths:
        if _refused_paths({path: ""}):
            continue
        target = clone / path
        if not target.is_file():
            continue
        try:
            shown[path] = target.read_text()[:MAX_FILE_CHARS]
        except UnicodeDecodeError:
            continue
    return shown


def _scrubbed_env(home: Path) -> dict[str, str]:
    """PATH, a throwaway HOME and the locale. Nothing else, so no secret.

    No bytecode either: an attempt that rewrites a file to the same size in
    the same second would otherwise run the last attempt's cached .pyc."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("LANG", "LC_ALL"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _chore_ids() -> tuple[int, int] | None:
    """The user the test command runs as. None when the worker is not root,
    as in the tests, where it cannot switch users and runs as itself. As root
    with no chore user it refuses, rather than run the repo's code as root."""
    if os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(CHORE_USER)
    except KeyError:
        raise ChoreError(f"the worker is root and there is no {CHORE_USER} user") from None
    return entry.pw_uid, entry.pw_gid


def _give_to(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)


def _run_tests(clone: Path, setup: ChoreSetup, workdir: Path) -> tuple[bool, int | None, str]:
    home = workdir / "home"
    home.mkdir(exist_ok=True)
    ids = _chore_ids()
    switch: dict = {}
    if ids is not None:
        uid, gid = ids
        _give_to(clone, uid, gid)
        _give_to(home, uid, gid)
        # mkdtemp makes the workdir 0700. The chore user has to pass through it.
        workdir.chmod(0o711)
        switch = {"user": uid, "group": gid, "extra_groups": []}
    process = subprocess.Popen(
        ["sh", "-c", setup.repo.test_command],
        cwd=clone,
        env=_scrubbed_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        **switch,
    )
    try:
        output, _ = process.communicate(timeout=setup.test_timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output, _ = process.communicate()
        tail = (output or "")[-MAX_OUTPUT_CHARS:]
        return False, None, f"{tail}\n(timed out after {setup.test_timeout_seconds:.0f} s)"
    return process.returncode == 0, process.returncode, (output or "")[-MAX_OUTPUT_CHARS:]


class _Git:
    def __init__(self, workdir: Path, setup: ChoreSetup) -> None:
        home = workdir / "git-home"
        home.mkdir(exist_ok=True)
        # The clone belongs to the chore user once the tests have run, and git
        # running as root refuses a repo another user owns unless told not to.
        self._env = {
            **_scrubbed_env(home),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "*",
        }
        self._remote_env = dict(self._env)
        if setup.token and setup.clone_base.startswith("https://"):
            basic = base64.b64encode(f"x-access-token:{setup.token}".encode()).decode()
            self._remote_env.update(
                {
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_1": "http.extraheader",
                    "GIT_CONFIG_VALUE_1": f"AUTHORIZATION: basic {basic}",
                }
            )

    def run(self, *args: str, cwd: Path, remote: bool = False) -> str:
        # Every command gets the header env when it may reach the remote. It
        # lives only in this process's environment, never in a file.
        env = self._remote_env if remote or args[0] in ("clone", "ls-remote") else self._env
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise ChoreError(f"git {args[0]} timed out") from None
        if result.returncode != 0:
            raise ChoreError(f"git {args[0]} failed: {result.stderr.strip()[-300:]}")
        return result.stdout.strip()

    def remote_has_branch(self, url: str, branch: str) -> bool:
        return bool(self.run("ls-remote", "--heads", url, branch, cwd=Path(".")))

    def default_branch(self, url: str) -> str:
        out = self.run("ls-remote", "--symref", url, "HEAD", cwd=Path("."))
        match = re.search(r"ref: refs/heads/(\S+)\s+HEAD", out)
        return match[1] if match else "main"


class _Keepalive:
    """Heartbeats the run every 30 seconds on its own connection, so a test
    command that runs for minutes does not let the two minute lease lapse."""

    def __init__(self, database_url: str | None, run_id: str, worker_id: str | None) -> None:
        self._url, self._run_id, self._worker_id = database_url, run_id, worker_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if self._url and self._worker_id:
            self._thread = threading.Thread(target=self._beat, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _beat(self) -> None:
        try:
            with psycopg.connect(self._url, autocommit=True) as conn:
                while not self._stop.wait(HEARTBEAT_SECONDS):
                    if not heartbeat(conn, self._run_id, self._worker_id):
                        return
        except psycopg.Error as error:
            logger.error("heartbeat connection failed: %s", error)
