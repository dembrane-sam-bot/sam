"""ADK-based agent runner.

Implements the AgentRunner Protocol using Google's Agent Development Kit (ADK)
with Gemini models on Vertex AI. The main session runs on SMALL_MODEL
(gemini-2.5-flash); the `arc` subagent uses BIG_MODEL (gemini-2.5-pro) and
is invoked via AgentTool when Sam needs deeper reasoning.

Auth: set GOOGLE_GENAI_USE_VERTEXAI=true + GOOGLE_CLOUD_PROJECT +
GOOGLE_CLOUD_LOCATION in .env, or provide Application Default Credentials.
Model choices live in config.py (version-controlled), not .env.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import types as genai_types

from .config import (
    BIG_MODEL,
    MAX_SESSION_SECONDS,
    SAM_SRC,
    SMALL_MODEL,
    STUCK_TIMEOUT_SECONDS,
    log,
)
from .session import AgentRunRequest, AgentRunResult, ToolUseRecord


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def bash(command: str, description: Optional[str] = None, timeout: int = 120) -> str:
    """Execute a bash shell command and return stdout + stderr.

    Args:
        command: The shell command to run.
        description: Optional human-readable description (for readability only).
        timeout: Timeout in seconds. Default 120.

    Returns:
        Combined stdout + stderr, or an error message on failure.
    """
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, env=os.environ.copy(),
        )
        out = result.stdout
        if result.stderr:
            out += ("\n" if out else "") + result.stderr
        return out or "(no output)"
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    except Exception as exc:
        return f"error: {exc}"


def read_file(
    file_path: str,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """Read a file, with optional line offset and limit. Output uses cat -n format.

    Args:
        file_path: Absolute path to the file.
        offset: First line to return, 1-indexed. Default: start of file.
        limit: Maximum number of lines to return.

    Returns:
        Numbered file contents (line TAB content), or an error message.
    """
    try:
        lines = Path(file_path).read_text().splitlines()
        start = (offset - 1) if offset else 0
        lines = lines[start:]
        if limit:
            lines = lines[:limit]
        return "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(lines))
    except FileNotFoundError:
        return f"file not found: {file_path}"
    except Exception as exc:
        return f"error reading {file_path}: {exc}"


def write_file(file_path: str, content: str) -> str:
    """Write content to a file, overwriting if it exists. Creates parent dirs.

    Args:
        file_path: Absolute path to write.
        content: Content to write.

    Returns:
        Success note or error message.
    """
    try:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} bytes to {file_path}"
    except Exception as exc:
        return f"error writing {file_path}: {exc}"


def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace a string in a file.

    Args:
        file_path: Absolute path to the file.
        old_string: Exact string to replace. Must be unique unless replace_all=True.
        new_string: Replacement string.
        replace_all: Replace all occurrences. Default replaces exactly one.

    Returns:
        Success note or error message.
    """
    try:
        p = Path(file_path)
        text = p.read_text()
        count = text.count(old_string)
        if count == 0:
            return f"old_string not found in {file_path}"
        if not replace_all and count > 1:
            return (
                f"old_string appears {count} times — add more context to make "
                f"it unique, or set replace_all=True"
            )
        new_text = (
            text.replace(old_string, new_string)
            if replace_all
            else text.replace(old_string, new_string, 1)
        )
        p.write_text(new_text)
        return f"replaced {count if replace_all else 1} occurrence(s) in {file_path}"
    except FileNotFoundError:
        return f"file not found: {file_path}"
    except Exception as exc:
        return f"error editing {file_path}: {exc}"


def grep(
    pattern: str,
    path: str = ".",
    glob_pattern: Optional[str] = None,
    case_insensitive: bool = False,
    output_mode: str = "files_with_matches",
    context: Optional[int] = None,
    head_limit: int = 250,
) -> str:
    """Search files for a regex pattern using ripgrep.

    Args:
        pattern: Regex pattern to search for.
        path: File or directory to search. Default: current directory.
        glob_pattern: Glob filter for files (e.g. "*.py", "**/*.ts").
        case_insensitive: Enable case-insensitive search.
        output_mode: "files_with_matches", "content", or "count".
        context: Lines of context around matches (content mode only).
        head_limit: Max output lines to return. Default 250.

    Returns:
        Search results as a string, or "(no matches)".
    """
    cmd = ["rg"]
    if case_insensitive:
        cmd.append("-i")
    if output_mode == "files_with_matches":
        cmd.append("-l")
    elif output_mode == "count":
        cmd.append("--count")
    else:
        cmd.append("-n")
    if context:
        cmd.extend(["-C", str(context)])
    if glob_pattern:
        cmd.extend(["--glob", glob_pattern])
    cmd.extend([pattern, path])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        lines = result.stdout.splitlines()
        if head_limit:
            lines = lines[:head_limit]
        return "\n".join(lines) if lines else "(no matches)"
    except subprocess.TimeoutExpired:
        return "search timed out"
    except Exception as exc:
        return f"search error: {exc}"


def glob_files(pattern: str, path: Optional[str] = None) -> str:
    """Find files matching a glob pattern, sorted by modification time.

    Args:
        pattern: Glob pattern (e.g. "**/*.py", "src/**/*.ts").
        path: Directory to search. Default: current working directory.

    Returns:
        Newline-separated list of matching paths, or "(no matches)".
    """
    base = Path(path) if path else Path.cwd()
    try:
        matches = sorted(
            base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True,
        )
        return "\n".join(str(m) for m in matches) if matches else "(no matches)"
    except Exception as exc:
        return f"glob error: {exc}"


async def web_fetch(url: str, prompt: str) -> str:
    """Fetch a URL and return its content as text (up to 50 000 chars).

    Args:
        url: Fully-formed URL to fetch.
        prompt: Description of what to extract (informational only).

    Returns:
        Response body, or an error message.
    """
    import httpx
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url, headers={"User-Agent": "Sam/1.0 (adk-runner)"})
            resp.raise_for_status()
            return resp.text[:50_000]
    except Exception as exc:
        return f"error fetching {url}: {exc}"


# ---------------------------------------------------------------------------
# Agent builders
# ---------------------------------------------------------------------------

def _load_arc_instruction() -> str:
    """Load the arc agent's instruction from src/runtime/agents/arc.md body."""
    arc_path = SAM_SRC / "runtime" / "agents" / "arc.md"
    try:
        text = arc_path.read_text()
        # Strip YAML frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                return text[end + 3:].strip()
        return text.strip()
    except OSError:
        log.warning("arc.md not found at %s; using fallback instruction", arc_path)
        return (
            "You are a deep-thinking research and analysis partner. "
            "Analyze carefully, cite file:line references, and report findings. "
            "You are read-only — return reasoning and plans, do not act."
        )


def _build_arc_agent() -> LlmAgent:
    """Build the arc (big model) subagent — read-only, deep reasoning."""
    from google.adk.tools import google_search
    return LlmAgent(
        name="arc",
        model=BIG_MODEL,
        instruction=_load_arc_instruction(),
        tools=[read_file, grep, glob_files, web_fetch, google_search],
    )


def _build_sam_agent(system_prompt: str, arc_agent: LlmAgent) -> LlmAgent:
    """Build the main Sam agent with full tool access and arc as a callable subagent."""
    from google.adk.tools import google_search
    return LlmAgent(
        name="sam",
        model=SMALL_MODEL,
        instruction=system_prompt,
        tools=[
            bash,
            read_file,
            write_file,
            edit_file,
            grep,
            glob_files,
            web_fetch,
            google_search,
            AgentTool(agent=arc_agent),
        ],
    )


# ---------------------------------------------------------------------------
# Run-state helper — mutable bag shared between async tasks
# ---------------------------------------------------------------------------

class _RunState:
    __slots__ = (
        "started_at", "started_at_wall", "last_output_at",
        "stuck", "timed_out", "error",
        "tool_use_records",
    )

    def __init__(self, started_at: float, started_at_wall: float) -> None:
        self.started_at = started_at
        self.started_at_wall = started_at_wall
        self.last_output_at = started_at
        self.stuck = False
        self.timed_out = False
        self.error = False
        self.tool_use_records: list[ToolUseRecord] = []


# ---------------------------------------------------------------------------
# ADKAgentRunner — implements the AgentRunner Protocol
# ---------------------------------------------------------------------------

class ADKAgentRunner:
    """Runs one Sam session using Google ADK + Gemini on Vertex AI.

    Main agent: SMALL_MODEL (fast, used for all routine work).
    Arc subagent: BIG_MODEL (slower, deeper — dispatch via AgentTool when
    careful multi-file reasoning or an independent second opinion is needed).

    Stuck / timeout detection mirrors ClaudeCodeAgentRunner: a background
    watcher cancels the event stream if the session goes idle for
    STUCK_TIMEOUT_SECONDS or exceeds MAX_SESSION_SECONDS wall time.
    """

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        state = _RunState(
            started_at=time.monotonic(),
            started_at_wall=time.time(),
        )

        arc = _build_arc_agent()
        sam = _build_sam_agent(request.system_prompt, arc)

        session_service = InMemorySessionService()
        runner = Runner(
            agent=sam,
            app_name="sam",
            session_service=session_service,
        )
        session = await session_service.create_session(
            app_name="sam",
            user_id="sam",
        )

        user_content = genai_types.Content(
            role="user",
            parts=[genai_types.Part(text=request.initial_user_message)],
        )

        stream_task = asyncio.create_task(
            self._stream_events(runner, session.id, user_content, state)
        )
        watch_task = asyncio.create_task(self._watch(stream_task, state))

        await asyncio.gather(stream_task, watch_task, return_exceptions=True)

        exit_code = 0 if not (state.stuck or state.timed_out or state.error) else 1
        return AgentRunResult(
            exit_code=exit_code,
            started_at=state.started_at,
            started_at_wall=state.started_at_wall,
            ended_at=time.monotonic(),
            last_output_at=state.last_output_at,
            stuck=state.stuck,
            timed_out=state.timed_out,
            tool_use_records=state.tool_use_records,
        )

    async def _stream_events(
        self,
        runner: Runner,
        session_id: str,
        user_content: genai_types.Content,
        state: _RunState,
    ) -> None:
        try:
            async for event in runner.run_async(
                user_id="sam",
                session_id=session_id,
                new_message=user_content,
            ):
                state.last_output_at = time.monotonic()
                for fc in (event.get_function_calls() or []):
                    state.tool_use_records.append(ToolUseRecord(
                        name=fc.name or "",
                        input=dict(fc.args or {}),
                    ))
        except asyncio.CancelledError:
            pass  # cancelled by _watch; stuck/timed_out flags already set
        except Exception:
            log.exception("ADK stream error")
            state.error = True

    async def _watch(
        self,
        stream_task: "asyncio.Task[None]",
        state: _RunState,
    ) -> None:
        """Cancel stream_task if the session goes idle or exceeds the wall-time cap."""
        while not stream_task.done():
            await asyncio.sleep(30)
            elapsed = time.monotonic() - state.started_at
            idle = time.monotonic() - state.last_output_at
            if elapsed > MAX_SESSION_SECONDS:
                log.warning(
                    "ADK session exceeded max wall time (%ds), cancelling", int(elapsed),
                )
                state.timed_out = True
                stream_task.cancel()
                return
            if idle > STUCK_TIMEOUT_SECONDS:
                log.warning(
                    "ADK session stuck (no event for %ds), cancelling", int(idle),
                )
                state.stuck = True
                stream_task.cancel()
                return
