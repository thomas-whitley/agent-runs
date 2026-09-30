"""Run the agent's candidate code against the supplied pytest file.

The candidate runs in a subprocess with a timeout, a scrubbed environment, and
Python's socket layers disabled. The scrubbed environment covers the child's
own environment only. A child running as the worker's user could still read
the worker's keys from /proc/<pid>/environ, so in the image, where the worker
is root, the child runs as the unprivileged sandbox user instead.

That is a demo guard, not isolation: the worker container itself does have a
network, because it calls the model and Postgres, and a determined escape from
a Python level patch is not hard. It is enough to stop generated code wandering
off by accident.
"""

import os
import pwd
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 10.0
# Created in the Dockerfile. pytest runs as this user when the worker is root,
# as it is in the image.
SANDBOX_USER = "sandbox"

# Imported by the subprocess at startup, before any test code runs.
_SITECUSTOMIZE = """
import _socket
import socket

_MESSAGE = "network access is disabled in the verification sandbox"


# Both layers have to go. socket.socket is a Python class over _socket.socket,
# so patching only the former is bypassed by importing _socket directly. They
# stay classes because ssl.SSLSocket subclasses socket.socket at import time,
# and replacing it with a function breaks the standard library.
class _BlockedRawSocket(_socket.socket):
    def __init__(self, *args, **kwargs):
        raise OSError(_MESSAGE)


class _BlockedSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise OSError(_MESSAGE)


def _blocked(*args, **kwargs):
    raise OSError(_MESSAGE)


_socket.socket = _BlockedRawSocket
socket.socket = _BlockedSocket
socket.create_connection = _blocked
socket.getaddrinfo = _blocked
"""


class SandboxError(Exception):
    """The worker is root and has no sandbox user to run the test file as."""


@dataclass(frozen=True)
class VerifyResult:
    passed: bool
    output: str
    timed_out: bool


def verify(
    solution_code: str,
    test_code: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> VerifyResult:
    """Return whether pytest exits zero on the supplied test file."""
    with tempfile.TemporaryDirectory(prefix="agent-runs-verify-") as directory:
        workspace = Path(directory)
        (workspace / "solution.py").write_text(solution_code)
        (workspace / "test_solution.py").write_text(test_code)
        (workspace / "sitecustomize.py").write_text(_SITECUSTOMIZE)

        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "test_solution.py",
        ]
        environment = {
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(workspace),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": str(workspace),
        }
        switch: dict = {}
        ids = _sandbox_ids()
        if ids is not None:
            uid, gid = ids
            _give_to(workspace, uid, gid)
            switch = {"user": uid, "group": gid, "extra_groups": []}

        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                **switch,
            )
        except subprocess.TimeoutExpired as expired:
            return VerifyResult(
                passed=False,
                output=_as_text(expired.stdout)
                + _as_text(expired.stderr)
                + f"\nKilled after {timeout_seconds} seconds.",
                timed_out=True,
            )

        return VerifyResult(
            passed=completed.returncode == 0,
            output=completed.stdout + completed.stderr,
            timed_out=False,
        )


def _sandbox_ids() -> tuple[int, int] | None:
    """The user pytest runs as. None when the worker is not root, as in the
    tests, where it cannot switch users and runs as itself. As root with no
    sandbox user it refuses, rather than run the posted file as root."""
    if os.geteuid() != 0:
        return None
    try:
        entry = pwd.getpwnam(SANDBOX_USER)
    except KeyError:
        raise SandboxError(f"the worker is root and there is no {SANDBOX_USER} user") from None
    return entry.pw_uid, entry.pw_gid


def _give_to(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.chown(os.path.join(root, name), uid, gid, follow_symlinks=False)


def _as_text(stream: bytes | str | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return stream
