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
