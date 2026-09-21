"""Run the agent's candidate code against the supplied pytest file.

The candidate runs in a subprocess with a timeout, a scrubbed environment so no
key reaches it, and Python's socket layers disabled. That is a demo guard, not
isolation: the worker container itself does have a network, because it calls the
model and Postgres, and a determined escape from a Python level patch is not
hard. It is enough to stop generated code wandering off by accident.
"""

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 10.0

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

        try:
            completed = subprocess.run(
                command,
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
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


def _as_text(stream: bytes | str | None) -> str:
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", "replace")
    return stream
