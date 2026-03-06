"""
Tests for relay.py — RelayOrchestrator, adapters, and helpers.

All transport calls are mocked so no Claude CLI or tmux is needed.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_cli_connector.relay import (
    RelayConfig,
    RelayMode,
    RelayOrchestrator,
    RelayResult,
    RelayRole,
    RelayTurn,
    StreamJsonRelayAdapter,
    TmuxRelayAdapter,
    _check_collab_approval,
    _format_turn_context,
)
from claude_cli_connector.transport_base import TransportMode


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def debate_config():
    return RelayConfig(
        mode=RelayMode.DEBATE,
        role_a=RelayRole("Optimist", "You are optimistic."),
        role_b=RelayRole("Skeptic", "You are skeptical."),
        initial_topic="Is AI beneficial?",
        max_rounds=3,
        round_timeout=10.0,
        transport_mode=TransportMode.STREAM_JSON,
    )


@pytest.fixture
def collab_config():
    return RelayConfig(
        mode=RelayMode.COLLAB,
        role_a=RelayRole("Developer", "Write clean code."),
        role_b=RelayRole("Reviewer", "Review code thoroughly."),
        task_description="Implement an LRU cache",
        max_rounds=3,
        round_timeout=10.0,
        transport_mode=TransportMode.STREAM_JSON,
    )


def _mock_adapter(name: str, responses: list[str]):
    """Create a mock RelayAdapter that returns responses in order."""
    adapter = AsyncMock()
    adapter.adapter_name = name
    adapter.is_alive = MagicMock(return_value=True)
    adapter.kill = MagicMock()

    call_count = {"n": 0}

    async def _send_and_wait(text, timeout):
        idx = min(call_count["n"], len(responses) - 1)
        call_count["n"] += 1
        return responses[idx], 0.001

    adapter.send_and_wait = _send_and_wait
    return adapter


# =========================================================================
# Data model tests
# =========================================================================


class TestRelayDataModels:
    def test_relay_mode_values(self):
        assert RelayMode.DEBATE == "debate"
        assert RelayMode.COLLAB == "collab"

    def test_relay_role_defaults(self):
        role = RelayRole("Alice")
        assert role.name == "Alice"
        assert role.system_prompt == ""
        assert role.model == ""

    def test_relay_config_debate(self, debate_config):
        assert debate_config.mode == RelayMode.DEBATE
        assert debate_config.role_a.name == "Optimist"
        assert debate_config.role_b.name == "Skeptic"
        assert debate_config.max_rounds == 3

    def test_relay_config_collab(self, collab_config):
        assert collab_config.mode == RelayMode.COLLAB
        assert collab_config.task_description == "Implement an LRU cache"

    def test_relay_turn(self):
        turn = RelayTurn(round_num=1, speaker="Alice", content="Hello")
        assert turn.round_num == 1
        assert turn.speaker == "Alice"
        assert turn.cost_usd == 0.0

    def test_relay_result(self):
        result = RelayResult(
            mode="debate",
            rounds_completed=3,
            final_state="max_rounds",
            transcript=[],
            role_a_name="A",
            role_b_name="B",
            start_time=100.0,
            end_time=200.0,
        )
        assert result.rounds_completed == 3
        assert result.total_cost_usd == 0.0


# =========================================================================
# Helper function tests
# =========================================================================


class TestHelpers:
    def test_format_turn_context(self):
        turn = RelayTurn(round_num=2, speaker="Alice", content="My argument is...")
        formatted = _format_turn_context(turn)
        assert "[Alice, Round 2]" in formatted
        assert "My argument is..." in formatted

    def test_check_collab_approval_lgtm(self):
        assert _check_collab_approval("LGTM, great work!") is True
        assert _check_collab_approval("This looks good to me.") is True
        assert _check_collab_approval("I approve this solution.") is True
        assert _check_collab_approval("Approved.") is True
        assert _check_collab_approval("Ship it!") is True

    def test_check_collab_approval_negative(self):
        assert _check_collab_approval("There are several issues.") is False
        assert _check_collab_approval("Please fix the edge cases.") is False
        assert _check_collab_approval("Not ready yet.") is False


# =========================================================================
# Adapter tests
# =========================================================================


class TestStreamJsonRelayAdapter:
    def test_adapter_name(self):
        mock_transport = MagicMock()
        mock_transport.name = "test-session"
        adapter = StreamJsonRelayAdapter(mock_transport)
        assert adapter.adapter_name == "test-session"

    def test_is_alive(self):
        mock_transport = MagicMock()
        mock_transport.is_alive.return_value = True
        adapter = StreamJsonRelayAdapter(mock_transport)
        assert adapter.is_alive() is True

    def test_kill(self):
        mock_transport = MagicMock()
        adapter = StreamJsonRelayAdapter(mock_transport)
        adapter.kill()
        mock_transport.kill.assert_called_once()


class TestTmuxRelayAdapter:
    def test_adapter_name(self):
        mock_session = MagicMock()
        mock_session.name = "tmux-session"
        adapter = TmuxRelayAdapter(mock_session)
        assert adapter.adapter_name == "tmux-session"

    def test_is_alive(self):
        mock_session = MagicMock()
        mock_session.is_alive.return_value = False
        adapter = TmuxRelayAdapter(mock_session)
        assert adapter.is_alive() is False


# =========================================================================
# Orchestrator tests (with mock adapters)
# =========================================================================


class TestRelayOrchestratorDebate:
    def test_debate_runs_correct_rounds(self, debate_config, tmp_path):
        orch = RelayOrchestrator(debate_config)
        orch.adapter_a = _mock_adapter("A", ["A says round 1", "A says round 2", "A says round 3"])
        orch.adapter_b = _mock_adapter("B", ["B says round 1", "B says round 2", "B says round 3"])

        # Override logger to use tmp_path
        from claude_cli_connector.history import ConversationLogger
        orch._relay_logger = ConversationLogger(
            session_name="test-debate", transport="relay", history_dir=tmp_path,
        )

        result = asyncio.run(orch.run())

        assert result.mode == "debate"
        assert result.rounds_completed == 3
        assert result.final_state == "max_rounds"
        # 3 rounds × 2 speakers = 6 turns
        assert len(result.transcript) == 6

    def test_debate_transcript_content(self, debate_config, tmp_path):
        orch = RelayOrchestrator(debate_config)
        orch.adapter_a = _mock_adapter("A", ["Opening argument"])
        orch.adapter_b = _mock_adapter("B", ["Counter argument"])

        from claude_cli_connector.history import ConversationLogger
        debate_config.max_rounds = 1
        orch._relay_logger = ConversationLogger(
            session_name="test-debate", transport="relay", history_dir=tmp_path,
        )

        result = asyncio.run(orch.run())
        assert result.transcript[0].speaker == "Optimist"
        assert result.transcript[0].content == "Opening argument"
        assert result.transcript[1].speaker == "Skeptic"
        assert result.transcript[1].content == "Counter argument"

    def test_debate_on_turn_callback(self, debate_config, tmp_path):
        orch = RelayOrchestrator(debate_config)
        debate_config.max_rounds = 1
        orch.adapter_a = _mock_adapter("A", ["resp A"])
        orch.adapter_b = _mock_adapter("B", ["resp B"])

        from claude_cli_connector.history import ConversationLogger
        orch._relay_logger = ConversationLogger(
            session_name="test-debate", transport="relay", history_dir=tmp_path,
        )

        turns_received = []
        result = asyncio.run(orch.run(on_turn=lambda t: turns_received.append(t)))
        assert len(turns_received) == 2

    def test_debate_cost_tracking(self, debate_config, tmp_path):
        orch = RelayOrchestrator(debate_config)
        debate_config.max_rounds = 1
        orch.adapter_a = _mock_adapter("A", ["resp"])
        orch.adapter_b = _mock_adapter("B", ["resp"])

        from claude_cli_connector.history import ConversationLogger
        orch._relay_logger = ConversationLogger(
            session_name="test-debate", transport="relay", history_dir=tmp_path,
        )

        result = asyncio.run(orch.run())
        # Each mock returns cost 0.001
        assert result.total_cost_usd == pytest.approx(0.002, abs=0.0001)


class TestRelayOrchestratorCollab:
    def test_collab_runs_until_max_rounds(self, collab_config, tmp_path):
        orch = RelayOrchestrator(collab_config)
        orch.adapter_a = _mock_adapter("Dev", ["solution v1", "solution v2", "solution v3"])
        orch.adapter_b = _mock_adapter("Rev", ["fix issue 1", "fix issue 2", "fix issue 3"])

        from claude_cli_connector.history import ConversationLogger
        orch._relay_logger = ConversationLogger(
            session_name="test-collab", transport="relay", history_dir=tmp_path,
        )

        result = asyncio.run(orch.run())
        assert result.rounds_completed == 3
        assert result.final_state == "max_rounds"
        assert len(result.transcript) == 6

    def test_collab_stops_on_approval(self, collab_config, tmp_path):
        orch = RelayOrchestrator(collab_config)
        orch.adapter_a = _mock_adapter("Dev", ["solution v1", "solution v2"])
        orch.adapter_b = _mock_adapter("Rev", ["fix issue 1", "LGTM, looks great!"])

        from claude_cli_connector.history import ConversationLogger
        orch._relay_logger = ConversationLogger(
            session_name="test-collab", transport="relay", history_dir=tmp_path,
        )

        result = asyncio.run(orch.run())
        assert result.rounds_completed == 2
        assert result.final_state == "approved"
        # Stopped at round 2 after reviewer said LGTM
        assert len(result.transcript) == 4  # 2 rounds × 2 speakers

    def test_collab_approved_first_round(self, collab_config, tmp_path):
        orch = RelayOrchestrator(collab_config)
        orch.adapter_a = _mock_adapter("Dev", ["perfect solution"])
        orch.adapter_b = _mock_adapter("Rev", ["Looks good to me! Ship it!"])

        from claude_cli_connector.history import ConversationLogger
        orch._relay_logger = ConversationLogger(
            session_name="test-collab", transport="relay", history_dir=tmp_path,
        )

        result = asyncio.run(orch.run())
        assert result.rounds_completed == 1
        assert result.final_state == "approved"
        assert len(result.transcript) == 2


class TestRelayOrchestratorEdgeCases:
    def test_determine_final_state_no_turns(self, debate_config):
        orch = RelayOrchestrator(debate_config)
        state = orch._determine_final_state([])
        assert state == "no_turns"

    def test_history_logging(self, debate_config, tmp_path):
        """Verify relay turns are written to history JSONL."""
        orch = RelayOrchestrator(debate_config)
        debate_config.max_rounds = 1
        orch.adapter_a = _mock_adapter("A", ["hello from A"])
        orch.adapter_b = _mock_adapter("B", ["hello from B"])

        from claude_cli_connector.history import ConversationLogger
        orch._relay_logger = ConversationLogger(
            session_name="test-debate", transport="relay", history_dir=tmp_path,
        )

        result = asyncio.run(orch.run())

        # Read back the JSONL file
        entries = orch._relay_logger.read()
        # Should have at least 2 relay_turn entries (logger injected before init)
        assert len(entries) >= 2
        event_types = [e.event_type for e in entries]
        assert event_types.count("relay_turn") >= 2

    def test_transports_killed_after_run(self, debate_config, tmp_path):
        """Verify adapters are killed after relay completes."""
        orch = RelayOrchestrator(debate_config)
        debate_config.max_rounds = 1
        adapter_a = _mock_adapter("A", ["resp"])
        adapter_b = _mock_adapter("B", ["resp"])
        orch.adapter_a = adapter_a
        orch.adapter_b = adapter_b

        from claude_cli_connector.history import ConversationLogger
        orch._relay_logger = ConversationLogger(
            session_name="test-debate", transport="relay", history_dir=tmp_path,
        )

        asyncio.run(orch.run())
        adapter_a.kill.assert_called_once()
        adapter_b.kill.assert_called_once()
