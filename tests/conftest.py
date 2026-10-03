"""Shared test setup."""

import pytest

from quartermaster import cli


@pytest.fixture(autouse=True)
def no_real_log(monkeypatch):
    """``cli.main`` points the root logger at the real quartermaster.log (with
    ``force=True``), and a test that calls it left every later test logging
    there: fake failures and pids showed up in the live log and dashboard."""
    monkeypatch.setattr(cli, "_configure_logging", lambda settings: None)
