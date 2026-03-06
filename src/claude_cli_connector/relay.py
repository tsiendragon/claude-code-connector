"""
relay.py
--------
Claude-to-Claude relay: orchestrate two Claude Code instances talking to
each other.

Two modes are supported:

  - **debate**:  Give each Claude a role / persona and let them discuss a
    topic for N rounds.
  - **collab**:  One Claude writes code (Developer), another reviews
    (Reviewer).  They iterate until the reviewer approves or max rounds.

Both modes work with the tmux and stream-json transports.

Example (Python API)::

    from claude_cli_connector.relay import (
        RelayOrchestrator, RelayConfig, RelayRole, RelayMode,
    )
    from claude_cli_connector.transport_base import TransportMode

    config = RelayConfig(
        mode=RelayMode.DEBATE,
        role_a=RelayRole("Optimist", "You are optimistic about AI."),
        role_b=RelayRole("Skeptic", "You are skeptical about AI."),
        initial_topic="Is AI beneficial for society?",
        max_rounds=3,
    )
    orch = RelayOrchestrator(config)
    result = asyncio.run(orch.run())
    for turn in result.transcript:
        print(f"[Round {turn.round_num}] {turn.speaker}: {turn.content[:120]}")

Example (CLI)::

    ccc relay debate "Is AI beneficial?" --role-a Optimist --role-b Skeptic
    ccc relay collab "Implement an LRU cache in Python" --rounds 3
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from claude_cli_connector.exceptions import TransportError
from claude_cli_connector.transport_base import TransportMode

logger = logging.getLogger(__name__)

# =========================================================================
# Data models
# =========================================================================


class RelayMode(str, Enum):
    """Relay conversation mode."""

    DEBATE = "debate"
    COLLAB = "collab"


@dataclass
class RelayRole:
    """Configuration for one Claude instance in a relay."""

    name: str
    system_prompt: str = ""
    model: str = ""


@dataclass
class RelayTurn:
    """One turn (one speaker's response) in the relay transcript."""

    round_num: int
    speaker: str
    content: str
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class RelayConfig:
    """Full configuration for a relay session."""

    mode: RelayMode
    role_a: RelayRole
    role_b: RelayRole

    # Debate-specific
    initial_topic: str = ""

    # Collab-specific
    task_description: str = ""

    # Shared
    max_rounds: int = 5
    round_timeout: float = 300.0
    transport_mode: TransportMode = TransportMode.STREAM_JSON
    cwd: str = "."
    command: str = "claude"
    allowed_tools: list[str] = field(default_factory=list)
    verbose: bool = True


@dataclass
class RelayResult:
    """Outcome of a completed relay session."""

    mode: str
    rounds_completed: int
    final_state: str  # "max_rounds", "approved", "error", "interrupted"
    transcript: list[RelayTurn]
    role_a_name: str
    role_b_name: str
    start_time: float
    end_time: float
    total_cost_usd: float = 0.0
    history_path: Optional[Path] = None


# =========================================================================
# Transport adapters (unified async interface)
# =========================================================================


class RelayAdapter(ABC):
    """Abstract adapter for transport-agnostic relay execution."""

    @abstractmethod
    async def send_and_wait(self, text: str, timeout: float) -> tuple[str, float]:
        """Send message and wait for response.

        Returns
        -------
        (response_text, cost_usd)
        """
        ...

    @abstractmethod
    def is_alive(self) -> bool: ...

    @abstractmethod
    def kill(self) -> None: ...

    @property
    @abstractmethod
    def adapter_name(self) -> str: ...


class StreamJsonRelayAdapter(RelayAdapter):
    """One-shot ``claude -p --output-format json`` adapter for relay use.

    Since ``claude -p`` is one-shot (the process exits after responding),
    this adapter spawns a **fresh process** for every ``send_and_wait``
    call.  It uses ``--output-format json`` (not ``stream-json``) for
    maximum compatibility — the complete JSON response is read once the
    process exits.
    """

    def __init__(
        self,
        *,
        name: str,
        cwd: str = ".",
        command: str = "claude",
        allowed_tools: list[str] | None = None,
        system_prompt: str = "",
        model: str = "",
        verbose: bool = True,
    ) -> None:
        self._name = name
        self._cwd = cwd
        self._command = command
        self._allowed_tools = allowed_tools or []
        self._system_prompt = system_prompt
        self._model = model
        self._verbose = verbose
        self._last_process: Any = None  # for kill()

    def _build_command(self) -> list[str]:
        """Build the ``claude -p --output-format json`` command."""
        cmd = [self._command, "-p", "--output-format", "json"]
        if self._allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self._allowed_tools)])
        if self._model:
            cmd.extend(["--model", self._model])
        if self._system_prompt:
            cmd.extend(["--append-system-prompt", self._system_prompt])
        return cmd

    async def send_and_wait(self, text: str, timeout: float) -> tuple[str, float]:
        import json as _json
        import subprocess as _sp

        cmd = self._build_command()
        logger.debug("RelayAdapter(%s): running %s", self._name, " ".join(cmd))

        proc = _sp.Popen(
            cmd,
            stdin=_sp.PIPE,
            stdout=_sp.PIPE,
            stderr=_sp.PIPE,
            cwd=self._cwd,
            text=True,
        )
        self._last_process = proc

        try:
            # Write prompt as plain text to stdin, then close (EOF)
            stdout, stderr = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: proc.communicate(input=text, timeout=timeout)
                ),
                timeout=timeout + 5,
            )
        except (asyncio.TimeoutError, _sp.TimeoutExpired):
            proc.kill()
            proc.wait(timeout=5)
            raise TransportError(f"RelayAdapter({self._name}): timeout after {timeout}s")

        if stderr and stderr.strip():
            logger.debug("RelayAdapter(%s) stderr: %s", self._name, stderr.strip()[:500])

        if proc.returncode != 0:
            logger.warning(
                "RelayAdapter(%s): claude exited with code %d. stderr: %s",
                self._name, proc.returncode, stderr.strip()[:500],
            )

        # Parse JSON output
        stdout = stdout.strip()
        if not stdout:
            logger.warning("RelayAdapter(%s): empty stdout", self._name)
            return "", 0.0

        try:
            data = _json.loads(stdout)
        except _json.JSONDecodeError:
            # Fallback: treat entire stdout as plain text
            logger.debug("RelayAdapter(%s): non-JSON output, using as plain text", self._name)
            return stdout, 0.0

        # Extract text from JSON response
        # Format: {"type": "result", "result": "...", "cost_usd": 0.01, ...}
        # Or:     {"role": "assistant", "content": [{"type": "text", "text": "..."}]}
        content = ""
        cost = 0.0

        if isinstance(data, dict):
            # Try "result" field first (standard --output-format json)
            if data.get("result"):
                content = data["result"]
            # Try content blocks
            elif data.get("content"):
                blocks = data["content"]
                if isinstance(blocks, list):
                    content = "\n".join(
                        b.get("text", "") for b in blocks if b.get("type") == "text"
                    )
                elif isinstance(blocks, str):
                    content = blocks
            cost = data.get("cost_usd", 0.0) or 0.0

        return content.strip(), cost

    def is_alive(self) -> bool:
        return self._last_process is not None and self._last_process.poll() is None

    def kill(self) -> None:
        if self._last_process is not None:
            try:
                self._last_process.kill()
                self._last_process.wait(timeout=5)
            except Exception:
                pass
            self._last_process = None

    @property
    def adapter_name(self) -> str:
        return self._name


class TmuxRelayAdapter(RelayAdapter):
    """Wraps :class:`ClaudeSession` for relay use (sync → async via executor)."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def send_and_wait(self, text: str, timeout: float) -> tuple[str, float]:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._session.send_and_wait(text, timeout=timeout),
        )
        return response, 0.0  # tmux mode doesn't report cost

    def is_alive(self) -> bool:
        return self._session.is_alive()

    def kill(self) -> None:
        self._session.kill()

    @property
    def adapter_name(self) -> str:
        return self._session.name


# =========================================================================
# Helper functions
# =========================================================================


def _format_turn_context(turn: RelayTurn) -> str:
    """Format a turn for inclusion in the other speaker's context."""
    return f"[{turn.speaker}, Round {turn.round_num}]\n{turn.content}"


def _check_collab_approval(text: str) -> bool:
    """Return True if the reviewer's response signals approval."""
    lower = text.lower()
    approval_signals = [
        "lgtm",
        "looks good to me",
        "looks good",
        "approved",
        "i approve",
        "ship it",
        "no further changes",
        "no issues found",
    ]
    return any(signal in lower for signal in approval_signals)


# =========================================================================
# Relay orchestrator
# =========================================================================


class RelayOrchestrator:
    """
    Manages two Claude instances in a relay conversation.

    Usage::

        orch = RelayOrchestrator(config)
        result = await orch.run()   # or asyncio.run(orch.run())
    """

    def __init__(self, config: RelayConfig) -> None:
        self.config = config
        self.adapter_a: Optional[RelayAdapter] = None
        self.adapter_b: Optional[RelayAdapter] = None
        self._relay_logger: Any = None  # ConversationLogger
        self._on_turn: Optional[Any] = None  # callback(RelayTurn)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, on_turn: Optional[Any] = None) -> RelayResult:
        """Start transports, execute the relay loop, and return results.

        Parameters
        ----------
        on_turn:
            Optional callback ``(RelayTurn) -> None`` called after each turn
            for live display.
        """
        self._on_turn = on_turn
        start = time.time()

        try:
            # Skip transport start if adapters were already injected (testing)
            if self.adapter_a is None or self.adapter_b is None:
                await self._start_transports()
            if self._relay_logger is None:
                self._init_logger()

            if self.config.mode == RelayMode.DEBATE:
                transcript = await self._run_debate()
            else:
                transcript = await self._run_collab()

            final_state = self._determine_final_state(transcript)

        except KeyboardInterrupt:
            transcript = getattr(self, "_transcript", [])
            final_state = "interrupted"
        except Exception as exc:
            logger.error("Relay error: %s", exc)
            transcript = getattr(self, "_transcript", [])
            final_state = f"error: {exc}"
        finally:
            await self._stop_transports()

        end = time.time()
        total_cost = sum(t.cost_usd for t in transcript)

        return RelayResult(
            mode=self.config.mode.value,
            rounds_completed=max((t.round_num for t in transcript), default=0),
            final_state=final_state,
            transcript=transcript,
            role_a_name=self.config.role_a.name,
            role_b_name=self.config.role_b.name,
            start_time=start,
            end_time=end,
            total_cost_usd=total_cost,
            history_path=(
                self._relay_logger._file_path
                if self._relay_logger and hasattr(self._relay_logger, "_file_path")
                else None
            ),
        )

    # ------------------------------------------------------------------
    # Transport lifecycle
    # ------------------------------------------------------------------

    async def _start_transports(self) -> None:
        if self.config.transport_mode == TransportMode.STREAM_JSON:
            await self._start_stream_json()
        elif self.config.transport_mode == TransportMode.TMUX:
            await self._start_tmux()
        else:
            raise TransportError(
                f"Relay does not support transport mode: {self.config.transport_mode}"
            )

    async def _start_stream_json(self) -> None:
        self.adapter_a = StreamJsonRelayAdapter(
            name=f"relay-{self.config.role_a.name}",
            cwd=self.config.cwd,
            command=self.config.command,
            allowed_tools=self.config.allowed_tools,
            system_prompt=self.config.role_a.system_prompt,
            model=self.config.role_a.model,
            verbose=self.config.verbose,
        )

        self.adapter_b = StreamJsonRelayAdapter(
            name=f"relay-{self.config.role_b.name}",
            cwd=self.config.cwd,
            command=self.config.command,
            allowed_tools=self.config.allowed_tools,
            system_prompt=self.config.role_b.system_prompt,
            model=self.config.role_b.model,
            verbose=self.config.verbose,
        )

    async def _start_tmux(self) -> None:
        from claude_cli_connector.session import ClaudeSession

        loop = asyncio.get_event_loop()

        def _sanitize_tmux_name(name: str) -> str:
            return name.replace(" ", "_").replace("/", "_")

        session_a = await loop.run_in_executor(
            None,
            lambda: ClaudeSession.create(
                name=f"relay-{_sanitize_tmux_name(self.config.role_a.name)}",
                cwd=self.config.cwd,
                command=self.config.command,
            ),
        )
        self.adapter_a = TmuxRelayAdapter(session_a)

        session_b = await loop.run_in_executor(
            None,
            lambda: ClaudeSession.create(
                name=f"relay-{_sanitize_tmux_name(self.config.role_b.name)}",
                cwd=self.config.cwd,
                command=self.config.command,
            ),
        )
        self.adapter_b = TmuxRelayAdapter(session_b)

    async def _stop_transports(self) -> None:
        for adapter in (self.adapter_a, self.adapter_b):
            if adapter is not None:
                try:
                    adapter.kill()
                except Exception as exc:
                    logger.warning("Relay: failed to kill adapter: %s", exc)

    # ------------------------------------------------------------------
    # Logger
    # ------------------------------------------------------------------

    def _init_logger(self) -> None:
        from claude_cli_connector.history import ConversationLogger

        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        session_name = f"relay-{self.config.mode.value}"
        def _sanitize(name: str) -> str:
            return name.replace(" ", "_").replace("/", "_")

        run_id = f"{_sanitize(self.config.role_a.name)}-vs-{_sanitize(self.config.role_b.name)}-{ts}"

        self._relay_logger = ConversationLogger(
            session_name=session_name,
            transport="relay",
            run_id=run_id,
        )

        # Log initial config
        self._relay_logger.log_event(
            role="system",
            content=(
                f"Relay started: mode={self.config.mode.value}, "
                f"role_a={self.config.role_a.name}, "
                f"role_b={self.config.role_b.name}, "
                f"max_rounds={self.config.max_rounds}"
            ),
            event_type="relay_start",
        )

    def _log_turn(self, turn: RelayTurn) -> None:
        if self._relay_logger:
            self._relay_logger.log_event(
                role="assistant",
                content=turn.content,
                event_type="relay_turn",
                metadata={
                    "round": turn.round_num,
                    "speaker": turn.speaker,
                    "cost_usd": turn.cost_usd,
                },
            )
        if self._on_turn:
            self._on_turn(turn)

    # ------------------------------------------------------------------
    # Debate mode
    # ------------------------------------------------------------------

    async def _run_debate(self) -> list[RelayTurn]:
        assert self.adapter_a is not None and self.adapter_b is not None
        cfg = self.config
        transcript: list[RelayTurn] = []
        self._transcript = transcript  # expose for error recovery

        topic = cfg.initial_topic
        role_a = cfg.role_a.name
        role_b = cfg.role_b.name
        timeout = cfg.round_timeout

        for rnd in range(1, cfg.max_rounds + 1):
            # --- Role A speaks ---
            # NOTE: Prompts are framed as analysis tasks rather than role-play
            # to work with both headless (-p) and interactive TUI modes.
            # Claude Code TUI rejects "You are X" role-play requests.
            if rnd == 1:
                prompt_a = (
                    f"Topic for structured comparison: {topic}\n\n"
                    f"Please present the strongest arguments from the "
                    f"\"{role_a}\" perspective. Be specific and substantive "
                    f"with concrete examples."
                )
            else:
                last_b = transcript[-1].content
                prompt_a = (
                    f"Topic: {topic}\n\n"
                    f"The opposing side (\"{role_b}\") argued:\n{last_b}\n\n"
                    f"Please respond from the \"{role_a}\" perspective. "
                    f"Address their points directly. "
                    f"Round {rnd} of {cfg.max_rounds}."
                )

            response_a, cost_a = await self.adapter_a.send_and_wait(prompt_a, timeout)
            turn_a = RelayTurn(
                round_num=rnd, speaker=role_a, content=response_a, cost_usd=cost_a,
            )
            transcript.append(turn_a)
            self._log_turn(turn_a)

            # --- Role B speaks ---
            if rnd == 1:
                prompt_b = (
                    f"Topic for structured comparison: {topic}\n\n"
                    f"The other side (\"{role_a}\") argued:\n{response_a}\n\n"
                    f"Please present counter-arguments from the "
                    f"\"{role_b}\" perspective. Be specific and substantive."
                )
            else:
                prompt_b = (
                    f"Topic: {topic}\n\n"
                    f"The opposing side (\"{role_a}\") argued:\n{response_a}\n\n"
                    f"Please respond from the \"{role_b}\" perspective. "
                    f"Address their points directly. "
                    f"Round {rnd} of {cfg.max_rounds}."
                )

            response_b, cost_b = await self.adapter_b.send_and_wait(prompt_b, timeout)
            turn_b = RelayTurn(
                round_num=rnd, speaker=role_b, content=response_b, cost_usd=cost_b,
            )
            transcript.append(turn_b)
            self._log_turn(turn_b)

        return transcript

    # ------------------------------------------------------------------
    # Collab mode
    # ------------------------------------------------------------------

    async def _run_collab(self) -> list[RelayTurn]:
        assert self.adapter_a is not None and self.adapter_b is not None
        cfg = self.config
        transcript: list[RelayTurn] = []
        self._transcript = transcript

        dev = cfg.role_a.name
        reviewer = cfg.role_b.name
        task = cfg.task_description
        timeout = cfg.round_timeout

        for rnd in range(1, cfg.max_rounds + 1):
            # --- Developer writes ---
            if rnd == 1:
                prompt_dev = (
                    f"Task: {task}\n\n"
                    f"Please implement a solution. Write clean, well-documented code."
                )
            else:
                last_feedback = transcript[-1].content
                prompt_dev = (
                    f"Task: {task}\n\n"
                    f"A code reviewer provided this feedback:\n{last_feedback}\n\n"
                    f"Please revise the implementation based on this feedback. "
                    f"Iteration {rnd} of {cfg.max_rounds}."
                )

            response_dev, cost_dev = await self.adapter_a.send_and_wait(prompt_dev, timeout)
            turn_dev = RelayTurn(
                round_num=rnd, speaker=dev, content=response_dev, cost_usd=cost_dev,
            )
            transcript.append(turn_dev)
            self._log_turn(turn_dev)

            # --- Reviewer reviews ---
            prompt_review = (
                f"Task: {task}\n\n"
                f"A developer submitted this solution (iteration {rnd}):\n{response_dev}\n\n"
                f"Please review the code. Point out bugs, suggest improvements, "
                f"and assess code quality.\n"
                f"If the solution is good enough, say \"LGTM\" or \"approved\"."
            )

            response_rev, cost_rev = await self.adapter_b.send_and_wait(prompt_review, timeout)
            turn_rev = RelayTurn(
                round_num=rnd, speaker=reviewer, content=response_rev, cost_usd=cost_rev,
            )
            transcript.append(turn_rev)
            self._log_turn(turn_rev)

            # Check if reviewer approved
            if _check_collab_approval(response_rev):
                logger.info("Relay: reviewer approved at round %d", rnd)
                if self._relay_logger:
                    self._relay_logger.log_event(
                        role="system",
                        content=f"Reviewer approved at round {rnd}",
                        event_type="relay_approved",
                    )
                break

        return transcript

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _determine_final_state(self, transcript: list[RelayTurn]) -> str:
        if not transcript:
            return "no_turns"

        rounds_done = max(t.round_num for t in transcript)

        # Collab: check if last reviewer turn approved
        if self.config.mode == RelayMode.COLLAB:
            reviewer_turns = [t for t in transcript if t.speaker == self.config.role_b.name]
            if reviewer_turns and _check_collab_approval(reviewer_turns[-1].content):
                return "approved"

        if rounds_done >= self.config.max_rounds:
            return "max_rounds"

        return "complete"
