"""Tracing is configured from the environment, and its absence is not an error."""

from app.telemetry import configure_telemetry


def test_without_a_connection_string_tracing_is_off(monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    assert configure_telemetry(app=None) is False


def test_an_empty_connection_string_counts_as_off(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")

    assert configure_telemetry(app=None) is False


def test_the_app_starts_with_tracing_off(start_server, monkeypatch):
    """The local stack has no Azure Monitor, and must not care."""
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    import httpx2

    base_url = start_server()

    assert httpx2.get(f"{base_url}/health").status_code == 200
