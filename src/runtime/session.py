"""Per-message session lifecycle and the agent-runner abstraction.

A `SamSession` is one wake-up: take an incoming Slack message, hand it to an
`AgentRunner`, and turn the runner's result into a `SessionResult` the daemon
can use to decide what reactions to add and whether to retry.

The `AgentRunner` Protocol is the swap point. The default implementation
`ClaudeCodeAgentRunner` shells out to the Claude Code CLI. Future runners
(smol-agents, OpenAI Agents, anything else with a CLI-or-API surface) can
implement the same protocol — `SamSession` doesn't care which one it gets.

Imports from .config and .prompts. Imported by .daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

from .config import (
    MAX_SESSION_SECONDS,
    SAM_MODEL,
    SAM_REPO,
    STDERR_TAIL_LINES,
    STUCK_TIMEOUT_SECONDS,
    SYNTHETIC_ERRORS_MAX,
    journal_path_for_today,
    log,
    redact_secrets,
)
from .prompts import (
    RETRY_SESSION_INTRO,
    RETRY_SESSION_OUTRO,
    assemble_system_prompt,
)

# -----------------------------------------------------------------------------
# Incoming message — the unit of work that wakes Sam up
# -----------------------------------------------------------------------------

def _format_file_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


@dataclass
class IncomingMessage:
    """A Slack message that woke Sam up — or a synthetic scheduled wake-up."""
    channel: str
    user: str
    text: str
    thread_ts: Optional[str]  # If part of a thread; else None
    event_ts: str
    files: list[dict] = field(default_factory=list)
    display_name: Optional[str] = None       # Slack display/real name, resolved by the daemon
    is_principal_operator: bool = False      # True iff `user` == SAM_OPERATOR_USER_ID
    retry_context: Optional[dict] = None  # Set on a one-shot retry session after a failed first attempt
    scheduled: bool = False  # True when synthesised by the daemon's scheduler (not a real Slack message)
    raw_event: dict = field(repr=False, default_factory=dict)

    def _sender_label(self) -> str:
        """Human-readable sender reference for the initial user message.

        Includes the resolved display name when the daemon was able to look
        it up, and explicitly flags principal-operator status so Sam can't
        infer-and-mislabel.
        """
        name_part = f"{self.display_name} " if self.display_name else ""
        principal_part = (
            "the principal operator"
            if self.is_principal_operator
            else "NOT the principal operator"
        )
        return f"{name_part}(<@{self.user}>, {principal_part})"

    def to_initial_user_message(self) -> str:
        """Format as the first user message into Sam's session."""
        if self.retry_context:
            return self._format_retry_message()
        if self.scheduled:
            return self._format_scheduled_message()

        thread_part = f"thread_ts={self.thread_ts}" if self.thread_ts else "no thread"
        body = (
            f"Slack message in channel {self.channel} from {self._sender_label()} ({thread_part}):\n\n"
            f"{self.text}\n\n"
        )
        if self.files:
            lines = ["## Attached files\n"]
            for f in self.files:
                name = f.get("name") or f.get("id") or "<unnamed>"
                mime = f.get("mimetype") or "unknown/unknown"
                size = _format_file_size(f.get("size"))
                url = f.get("url_private") or f.get("url_private_download") or ""
                lines.append(f"- `{name}` ({mime}, {size}) — {url}")
            lines.append(
                "\nFetch only if the content is relevant to the task. "
                "See `src/skills/slack-files.md` for the curl command and per-mimetype handling."
            )
            body += "\n".join(lines) + "\n\n"
        body += (
            f"Reply in Slack via the Web API. If this is in a thread, reply in-thread "
            f"(use thread_ts={self.thread_ts or self.event_ts})."
        )
        return body

    def _format_scheduled_message(self) -> str:
        """Initial user message for a daemon-synthesised scheduled wake-up.

        Distinct format so Sam doesn't try to reply to a non-existent
        Slack message. The directive lives in self.text (set by the
        scheduler from SCHEDULED_SKILL_TEMPLATE).
        """
        return self.text

    def _format_retry_message(self) -> str:
        """Initial user message for a one-shot retry after a failed session.

        Tells Sam: don't redo the task. Read what went wrong, post a single
        reply in the original thread explaining it, then exit.
        """
        ctx = self.retry_context or {}
        thread_target = self.thread_ts or self.event_ts
        thread_part = f"thread_ts={self.thread_ts}" if self.thread_ts else "no thread"

        if ctx.get("stuck"):
            failure_summary = "STUCK — no output for the stuck-detection window (the daemon killed the process)."
        elif ctx.get("timed_out"):
            failure_summary = "TIMED_OUT — exceeded the per-session wall-clock cap (the daemon killed the process)."
        else:
            failure_summary = f"Exited with non-zero code: {ctx.get('exit_code')}."

        lines = [
            RETRY_SESSION_INTRO.format(channel=self.channel, thread_target=thread_target),
            "",
            "## What the previous session was trying to handle",
            f"From {self._sender_label()} in channel {self.channel} ({thread_part}):",
            "",
            self.text,
        ]

        if self.files:
            lines.append("")
            lines.append("Attached files (from the original message):")
            for f in self.files:
                name = f.get("name") or f.get("id") or "<unnamed>"
                mime = f.get("mimetype") or "unknown/unknown"
                size = _format_file_size(f.get("size"))
                lines.append(f"- `{name}` ({mime}, {size})")

        lines.append("")
        lines.append("## Why the previous session failed")
        lines.append(failure_summary)

        synthetic_errors = ctx.get("synthetic_errors") or []
        if synthetic_errors:
            lines.append("")
            lines.append(
                "Synthetic error messages from Claude Code (these are the most "
                "load-bearing signal — Claude Code surfaces API errors and other "
                "internal failures here, NOT in stderr):"
            )
            lines.append("```")
            for e in synthetic_errors:
                lines.append(e)
            lines.append("```")

        stderr_tail = ctx.get("stderr_tail") or []
        if stderr_tail:
            lines.append("")
            lines.append(
                f"Stderr (last {len(stderr_tail)} lines from the failed session — "
                "treat as secondary signal; warnings here are often noise that "
                "appears in successful sessions too):"
            )
            lines.append("```")
            for s in stderr_tail:
                # Scrub env values — this stderr will be summarised back into Slack.
                lines.append(redact_secrets(s))
            lines.append("```")

        lines.append("")
        lines.append(RETRY_SESSION_OUTRO)
        return "\n".join(lines)

# -----------------------------------------------------------------------------
# Agent runner abstraction
# -----------------------------------------------------------------------------

@dataclass
class ToolUseRecord:
    """One tool call observed during a session.

    Runner-agnostic: any backend that has a tool-call concept can populate
    these. The daemon-level reactions logic inspects them to decide whether
    Opus was dispatched and whether non-Slack-housekeeping tool work happened.
    """
    name: str
    input: dict


@dataclass
class AgentRunRequest:
    """Everything an AgentRunner needs to run one session."""
    system_prompt: str
    initial_user_message: str
    allowed_tools: list[str]
    model: Optional[str] = None
    cwd: Optional[str] = None
    env: Optional[dict[str, str]] = None


@dataclass
class AgentRunResult:
    """Runner-agnostic result of one agent session."""
    exit_code: Optional[int]
    started_at: float                      # time.monotonic() at launch
    started_at_wall: float                 # time.time() at launch, for Slack history queries
    ended_at: float                        # time.monotonic() at end
    last_output_at: float
    stuck: bool
    timed_out: bool
    stderr_tail: list[str] = field(default_factory=list)
    synthetic_errors: list[str] = field(default_factory=list)
    tool_use_records: list[ToolUseRecord] = field(default_factory=list)


class AgentRunner(Protocol):
    """Swap point for the agentic framework Sam runs on.

    Default implementation: ClaudeCodeAgentRunner. Implementations should
    handle their own stuck/timeout detection and surface results in the
    common AgentRunResult shape so SamSession doesn't have to care which
    backend it got.
    """

    async def run(self, request: AgentRunRequest) -> AgentRunResult: ...


class ClaudeCodeAgentRunner:
    """Runs an agent session by spawning the Claude Code CLI subprocess.

    Reads stream-json on stdout, captures tool_use records, scrapes synthetic
    error messages (Claude-Code-specific signal Sam uses for retry-session
    briefing), and watches for stuck / timed-out conditions. Kills the
    subprocess if either tripwire fires.
    """

    def __init__(self) -> None:
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._last_output_at: float = 0.0

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        started_at = time.monotonic()
        started_at_wall = time.time()
        self._last_output_at = started_at

        stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        synthetic_errors: deque[str] = deque(maxlen=SYNTHETIC_ERRORS_MAX)
        tool_use_records: list[ToolUseRecord] = []

        user_input = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": request.initial_user_message}
        }) + "\n"

        cmd = [
            "claude",
            "-p",                                  # print/non-interactive
            "--verbose",                           # required by -p + stream-json output
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
        ]
        if request.model:
            cmd.extend(["--model", request.model])
        cmd.extend(["--system-prompt", request.system_prompt])
        cmd.extend(["--allowed-tools", ",".join(request.allowed_tools)])

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=request.env or os.environ.copy(),
            cwd=request.cwd,
        )

        assert self._proc.stdin is not None
        self._proc.stdin.write(user_input.encode())
        await self._proc.stdin.drain()
        self._proc.stdin.close()

        stuck = False
        timed_out = False

        async def read_stdout() -> None:
            assert self._proc and self._proc.stdout
            async for line in self._proc.stdout:
                self._last_output_at = time.monotonic()
                raw = line.decode().strip()
                if not raw:
                    continue
                log.debug("stdout: %s", raw[:200])
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "assistant":
                    continue
                msg = event.get("message") or {}
                content_blocks = msg.get("content") or []
                if msg.get("model") == "<synthetic>":
                    for block in content_blocks:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                synthetic_errors.append(text[:1000])
                                log.warning("synthetic error: %s", text[:200])
                    continue
                for block in content_blocks:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_use_records.append(ToolUseRecord(
                        name=block.get("name") or "",
                        input=block.get("input") or {},
                    ))

        async def read_stderr() -> None:
            assert self._proc and self._proc.stderr
            async for line in self._proc.stderr:
                self._last_output_at = time.monotonic()
                text = line.decode().rstrip()[:500]
                stderr_tail.append(text)
                log.warning("stderr: %s", text[:200])

        async def watch_stuck() -> None:
            nonlocal stuck, timed_out
            while self._proc and self._proc.returncode is None:
                await asyncio.sleep(30)
                elapsed = time.monotonic() - started_at
                idle = time.monotonic() - self._last_output_at
                if elapsed > MAX_SESSION_SECONDS:
                    log.warning("session exceeded max wall time, killing")
                    timed_out = True
                    self._kill()
                    return
                if idle > STUCK_TIMEOUT_SECONDS:
                    log.warning("session stuck (no output for %ds), killing", int(idle))
                    stuck = True
                    self._kill()
                    return

        await asyncio.gather(
            read_stdout(), read_stderr(), watch_stuck(),
            return_exceptions=True,
        )

        exit_code = await self._proc.wait()
        ended_at = time.monotonic()

        return AgentRunResult(
            exit_code=exit_code,
            started_at=started_at,
            started_at_wall=started_at_wall,
            ended_at=ended_at,
            last_output_at=self._last_output_at,
            stuck=stuck,
            timed_out=timed_out,
            stderr_tail=list(stderr_tail),
            synthetic_errors=list(synthetic_errors),
            tool_use_records=tool_use_records,
        )

    def _kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass

# -----------------------------------------------------------------------------
# SamSession — orchestrates one wake-up using a chosen AgentRunner
# -----------------------------------------------------------------------------

@dataclass
class SessionResult:
    """SamSession-level result. Wraps AgentRunResult with Sam-specific bits
    (session id, lifecycle reaction flags) the daemon uses to react and
    decide on retries.

    Reaction flags are independent — any combination can fire on a given
    session:
    - opus_used     → :brain:
    - web_used      → :globe_with_meridians:
    - bash_used     → :computer:
    - edited_files  → :gear:
    """
    session_id: str
    started_at: float
    started_at_wall: float        # time.time() at subprocess launch — for Slack history queries
    ended_at: float
    exit_code: Optional[int]
    last_output_at: float
    stuck: bool
    timed_out: bool
    opus_used: bool = False       # Agent tool dispatched to opus
    web_used: bool = False        # WebFetch or WebSearch was used
    bash_used: bool = False       # Bash used for non-Slack-housekeeping work (git, gh, curl, etc.)
    edited_files: bool = False    # Edit/Write to a path outside /data/journal/
    stderr_tail: list[str] = field(default_factory=list)
    synthetic_errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.stuck or self.timed_out or bool(self.exit_code)


class SamSession:
    """Runs one agent session against one incoming message.

    Reads identity/scope/capabilities/skills fresh, builds the system prompt,
    hands the request to an `AgentRunner`, and translates the runner result
    into a `SessionResult`. Default runner is ClaudeCodeAgentRunner; pass a
    different `AgentRunner` to swap the backend.
    """

    DEFAULT_ALLOWED_TOOLS: list[str] = [
        "Bash", "Read", "Write", "Edit", "Grep", "Glob",
        "WebFetch", "WebSearch", "Agent",
    ]

    def __init__(
        self,
        message: IncomingMessage,
        agent_runner: Optional[AgentRunner] = None,
    ):
        self.message = message
        self.session_id = uuid.uuid4().hex[:12]
        self.agent_runner: AgentRunner = agent_runner or ClaudeCodeAgentRunner()

    async def run(self) -> SessionResult:
        log.info(
            "session %s starting (channel=%s thread_ts=%s)",
            self.session_id, self.message.channel, self.message.thread_ts,
        )

        request = AgentRunRequest(
            system_prompt=assemble_system_prompt(),
            initial_user_message=self.message.to_initial_user_message(),
            allowed_tools=self.DEFAULT_ALLOWED_TOOLS,
            model=SAM_MODEL,
            cwd=str(SAM_REPO),
            env=os.environ.copy(),
        )

        agent_result = await self.agent_runner.run(request)

        log.info(
            "session %s ended (exit=%s stuck=%s timed_out=%s)",
            self.session_id, agent_result.exit_code,
            agent_result.stuck, agent_result.timed_out,
        )

        opus_used, web_used, bash_used, edited_files = self._classify_tool_use(
            agent_result.tool_use_records,
        )

        result = SessionResult(
            session_id=self.session_id,
            started_at=agent_result.started_at,
            started_at_wall=agent_result.started_at_wall,
            ended_at=agent_result.ended_at,
            exit_code=agent_result.exit_code,
            last_output_at=agent_result.last_output_at,
            stuck=agent_result.stuck,
            timed_out=agent_result.timed_out,
            opus_used=opus_used,
            web_used=web_used,
            bash_used=bash_used,
            edited_files=edited_files,
            stderr_tail=agent_result.stderr_tail,
            synthetic_errors=agent_result.synthetic_errors,
        )

        # Safety net journal entry — only if the session didn't write one,
        # which we approximate by checking exit code / stuck / timeout.
        if result.failed:
            self._safety_net_journal_entry(result)

        return result

    @staticmethod
    def _classify_tool_use(
        records: list[ToolUseRecord],
    ) -> tuple[bool, bool, bool, bool]:
        """Return (opus_used, web_used, bash_used, edited_files) for the
        post-session badges.

        - opus_used    = Agent tool dispatched to subagent_type=opus.
        - web_used     = WebFetch or WebSearch was used.
        - bash_used    = Bash used for non-Slack-housekeeping work (Bash
          calls whose command contains "slack.com" are post/react/reply
          calls and don't count).
        - edited_files = Edit or Write touched a path outside `/data/journal/`.
          Routine journal entries are written by every session and would
          make this flag meaningless if they counted.

        Read-only tools (Read, Grep, Glob) are pure context-gathering and
        don't drive any badge. Each flag drives one independent emoji on
        Sam's response; any combination can fire on a given session.
        """
        opus_used = False
        web_used = False
        bash_used = False
        edited_files = False
        for record in records:
            name = record.name
            input_dict = record.input or {}
            if name == "Agent":
                if input_dict.get("subagent_type") == "opus":
                    opus_used = True
            elif name in ("WebFetch", "WebSearch"):
                web_used = True
            elif name == "Bash":
                command = input_dict.get("command") or ""
                if "slack.com" not in command:
                    bash_used = True
            elif name in ("Edit", "Write"):
                file_path = input_dict.get("file_path") or ""
                if not file_path.startswith("/data/journal/"):
                    edited_files = True
            # Read, Grep, Glob: read-only context gathering, doesn't count.
        return opus_used, web_used, bash_used, edited_files

    def _safety_net_journal_entry(self, result: SessionResult) -> None:
        """Append a minimal journal entry when something went wrong.

        Sam should write its own entries normally. This is for the cases where
        Sam was killed or crashed and couldn't.
        """
        status = "stuck" if result.stuck else ("timed_out" if result.timed_out else "errored")
        entry = f"""---
date: {datetime.now().astimezone().isoformat()}
session: {result.session_id}
trigger: slack mention (channel={self.message.channel})
status: {status}
---

## What happened

Session ended without writing its own journal entry. Daemon detected: {status}.

The triggering message was from <@{self.message.user}> in channel {self.message.channel}.

## Open threads

The original request may be unaddressed. Future-Sam should check the Slack thread.

"""
        with journal_path_for_today().open("a") as f:
            f.write(entry)
