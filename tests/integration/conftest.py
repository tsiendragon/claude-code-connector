"""
Integration test configuration.

Integration tests require a live tmux server and the Claude CLI to be
installed and authenticated.  They are skipped by default and must be
explicitly enabled:

    pytest --run-integration

These tests are NOT run in CI.  Run them locally before releasing.
"""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires tmux + claude CLI).",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if not config.getoption("--run-integration"):
        skip_marker = pytest.mark.skip(
            reason="Integration tests skipped. Use --run-integration to enable."
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip_marker)
