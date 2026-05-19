"""Per-message session lifecycle and the agent-runner abstraction.

A `SamSession` is one wake-up: take an incoming Slack message, hand it to an
`AgentRunner`, and turn the runner's result into a `SessionResult` the daemon
can use to decide what reactions to add and whether to retry.

The `AgentRunner` Protocol is the swap point. The default implementation
`ADKAgentRunner` (in .adk_runner) runs Sam via Google ADK + Vertex Gemini.

Imports from .config and .prompts. Imported by .daemon.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

from .config import (
    SAM_REPO,
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
    # Prior messages in the thread, oldest-first, excluding the triggering message.
    # Populated by the daemon for thread-reply events; empty for top-level mentions.
    thread_history: list[dict] = field(default_factory=list)

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

    def _format_thread_history(self) -> str:
        """Render prior thread messages as a readable context block.

        Each message is one line: [ts] sender: text. Bot messages are
        labelled "Sam (bot)"; human messages use their <@user_id> Slack
        reference. Messages whose text exceeds 2 000 chars are truncated.
        Empty (no text) messages are skipped.

        Returns an empty string when there is nothing to show.
        """
        if not self.thread_history:
            return ""
        lines: list[str] = ["## Prior thread context (oldest first)", ""]
        for msg in self.thread_history:
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            ts = msg.get("ts") or "?"
            if msg.get("bot_id"):
                sender = "Sam (bot)"
            else:
                uid = msg.get("user") or "unknown"
                sender = f"<@{uid}>"
            if len(text) > 2000:
                text = text[:2000] + "… [truncated]"
            lines.append(f"[{ts}] {sender}: {text}")
        if len(lines) <= 2:
            # Only the header was added — no real content.
            return ""
        lines.append("")
        return "\n".join(lines)

    def to_initial_user_message(self) -> str:
        """Format as the first user message into Sam's session."""
        if self.retry_context:
            return self._format_retry_message()
        if self.scheduled:
            return self._format_scheduled_message()

        thread_part = f"thread_ts={self.thread_ts}" if self.thread_ts else "no thread"
        history_block = self._format_thread_history()
        preamble = f"{history_block}\n---\n\n" if history_block else ""
        body = (
            f"{preamble}"
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

    Default implementation: ADKAgentRunner (google.adk + Vertex Gemini).
    Any class with a compatible run() method satisfies this protocol.
    """

    async def run(self, request: AgentRunRequest) -> AgentRunResult: ...


# ADKAgentRunner is defined in .adk_runner and imported below.
# It is the sole default runner — ClaudeCodeAgentRunner has been removed.
# To restore the old Claude Code runner, see git history.

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
    - worker_used   → :brain:
    - web_used      → :globe_with_meridians:
    - bash_used     → :computer:
    - edited_files  → :gear:
    """
    session_id: str
    started_at: float
    started_at_wall: float        # time.time() at runner launch — for Slack history queries
    ended_at: float
    exit_code: Optional[int]
    last_output_at: float
    stuck: bool
    timed_out: bool
    worker_used: bool = False        # worker subagent (small model) was dispatched
    web_used: bool = False        # fetch_url or web search was used
    bash_used: bool = False       # bash used for non-Slack-housekeeping work (git, gh, curl, etc.)
    edited_files: bool = False    # write_file/edit_file touched a path outside /data/journal/
    stderr_tail: list[str] = field(default_factory=list)
    synthetic_errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.stuck or self.timed_out or bool(self.exit_code)


class SamSession:
    """Runs one agent session against one incoming message.

    Reads identity/scope/capabilities/skills fresh, builds the system prompt,
    hands the request to an `AgentRunner`, and translates the runner result
    into a `SessionResult`. Default runner is ADKAgentRunner (Vertex Gemini).
    """

    DEFAULT_ALLOWED_TOOLS: list[str] = [
        "bash", "read_file", "write_file", "edit_file", "grep", "glob_files",
        "fetch_url", "worker", "parallel_workers",
    ]

    def __init__(
        self,
        message: IncomingMessage,
        agent_runner: Optional[AgentRunner] = None,
    ):
        self.message = message
        self.session_id = uuid.uuid4().hex[:12]
        if agent_runner is None:
            from .adk_runner import ADKAgentRunner
            agent_runner = ADKAgentRunner()
        self.agent_runner: AgentRunner = agent_runner

    async def run(self) -> SessionResult:
        log.info(
            "session %s starting (channel=%s thread_ts=%s)",
            self.session_id, self.message.channel, self.message.thread_ts,
        )

        request = AgentRunRequest(
            system_prompt=assemble_system_prompt(),
            initial_user_message=self.message.to_initial_user_message(),
            allowed_tools=self.DEFAULT_ALLOWED_TOOLS,
            cwd=str(SAM_REPO),
            env=os.environ.copy(),
        )

        agent_result = await self.agent_runner.run(request)

        log.info(
            "session %s ended (exit=%s stuck=%s timed_out=%s)",
            self.session_id, agent_result.exit_code,
            agent_result.stuck, agent_result.timed_out,
        )

        worker_used, web_used, bash_used, edited_files = self._classify_tool_use(
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
            worker_used=worker_used,
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
        """Return (worker_used, web_used, bash_used, edited_files) for the
        post-session badges.

        ADK tool names (function names from adk_runner.py):
        - worker_used  = "worker" (single dispatch) or "parallel_workers"
                         (fan-out) was called — either counts as delegated work.
        - web_used     = fetch_url was called.
        - bash_used    = bash was called for non-Slack-housekeeping work (bash
          calls whose command contains "slack.com" are post/react/reply
          calls and don't count).
        - edited_files = write_file or edit_file touched a path outside
          /data/journal/. Routine journal entries are written by every session
          and would make this flag meaningless if they counted.

        Read-only tools (read_file, grep, glob_files) are pure context-gathering
        and don't drive any badge. Each flag drives one independent emoji on
        Sam's response; any combination can fire on a given session.
        """
        worker_used = False
        web_used = False
        bash_used = False
        edited_files = False
        for record in records:
            name = record.name
            input_dict = record.input or {}
            if name in ("worker", "parallel_workers"):
                worker_used = True
            elif name == "fetch_url":
                web_used = True
            elif name == "bash":
                command = input_dict.get("command") or ""
                if "slack.com" not in command:
                    bash_used = True
            elif name in ("write_file", "edit_file"):
                file_path = input_dict.get("file_path") or ""
                if not file_path.startswith("/data/journal/"):
                    edited_files = True
            # read_file, grep, glob_files: read-only, no badge.
        return worker_used, web_used, bash_used, edited_files

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
