"""The sandbox is a subprocess with a timeout and no network. Nothing more."""

import time

from app.sandbox import verify

PASSING_TEST = """
from solution import add

def test_add():
    assert add(2, 3) == 5
"""


def test_a_correct_solution_passes():
    result = verify("def add(a, b):\n    return a + b\n", PASSING_TEST)

    assert result.passed is True
    assert result.timed_out is False


def test_a_wrong_solution_fails_and_the_output_explains_why():
    result = verify("def add(a, b):\n    return a * b\n", PASSING_TEST)

    assert result.passed is False
    assert result.timed_out is False
    assert "add" in result.output


def test_a_solution_that_does_not_import_fails_rather_than_raising():
    result = verify("this is not python\n", PASSING_TEST)

    assert result.passed is False
    assert result.timed_out is False


def test_a_solution_that_hangs_is_killed_at_the_timeout():
    started = time.monotonic()
    result = verify(
        "def add(a, b):\n    while True:\n        pass\n", PASSING_TEST, timeout_seconds=2
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.passed is False
    assert elapsed < 20, "the subprocess outlived its timeout"


def test_the_sandbox_cannot_open_a_socket():
    reaching_out = """
import socket

def test_network():
    socket.create_connection(("example.com", 80), timeout=2)
"""
    result = verify("def add(a, b):\n    return a + b\n", reaching_out)

    assert result.passed is False
    assert "network" in result.output.lower()


def test_the_sandbox_cannot_reach_the_raw_socket_module():
    """Patching socket.socket alone is bypassed by importing _socket directly."""
    going_under = """
import _socket

def test_raw_socket():
    _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
"""
    result = verify("def add(a, b):\n    return a + b\n", going_under)

    assert result.passed is False
    assert "network" in result.output.lower()


def test_the_sandbox_does_not_inherit_the_parent_environment(monkeypatch):
    """A key in the worker's environment must not reach generated code."""
    monkeypatch.setenv("MODEL_API_KEY", "a-secret-that-must-not-leak")

    reading_env = """
import os

def test_env():
    assert "MODEL_API_KEY" not in os.environ, "the key reached the sandbox"
"""
    result = verify("def add(a, b):\n    return a + b\n", reading_env)

    assert result.passed is True
