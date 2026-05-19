"""ADK agent runner for Sam.

Implements the AgentRunner Protocol using Google's Agent Development Kit (ADK)
with Vertex AI Gemini models.

Architecture:
  main agent — SAM_SMALL_MODEL (gemini flash)
    ├── bash           run shell commands
    ├── read_file      read files from disk
    ├── write_file     write files to disk
    ├── edit_file      exact-string replacement in files
    ├── grep           search file contents
    ├── glob_files     find files by pattern
    ├── fetch_url      fetch a URL and return content
    └── arc            AgentTool → arc agent (SAM_BIG_MODEL, gemini pro, read-only)
                           ├── read_file
                           ├── grep
                           ├── glob_files
                           └── fetch_url

Vertex AI auth: set GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, and
GOOGLE_APPLICATION_CREDENTIALS (or use workload identity) in the environment.
The google-genai SDK picks these up automatically.

Session flow: one Runner per call to run(). Sessions are ephemeral
(InMemorySessionService). Stuck/timeout detection mirrors
ClaudeCodeAgentRunner via asyncio task cancellation.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .config import (
    MAX_SESSION_SECONDS,
    SAM_BIG_MODEL,
    SAM_SMALL_MODEL,
    SAM_SRC,
    STUCK_TIMEOUT_SECONDS,
    log,
)
from .session import AgentRunRequest, AgentRunResult, ToolUseRecord

# ─── Tool implementations ─────────────────────────────────────────────────────
# These are plain Python functions/coroutines; ADK wraps them as FunctionTools
# and generates the JSON schema from type annotations + docstrings.
# Tool names (function names) are what appear in ToolUseRecord.name —
# _classify_tool_use in session.py matches against these exact strings.


def _make_bash(cwd: Optional[str], env: Optional[dict[str, str]]):
    async def bash(command: str) -> str:
        """Run a shell command and return combined stdout+stderr. Timeout 120s."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env or os.environ.copy(),
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=120
                )
            except asyncio.TimeoutError:
                proc.kill()
                return "(command timed out after 120s)"
            out = stdout.decode(errors="replace")
            err = stderr.decode(errors="replace")
            if err:
                out = out + ("\n" if out else "") + "[stderr]\n" + err
            return out.rstrip() or "(no output)"
        except Exception as exc:
            return f"error: {exc}"

    return bash


def read_file(
    file_path: str,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """Read lines from a file. Returns cat -n format (line_number TAB content).
    offset: 1-based line to start reading from. limit: max lines to read."""
    try:
        p = Path(file_path)
        if not p.exists():
            return f"error: {file_path} does not exist"
        if p.is_dir():
            return f"error: {file_path} is a directory"
        content = p.read_text(errors="replace")
        lines = content.splitlines()
        start = max(0, (offset or 1) - 1)
        end = start + (limit or len(lines))
        numbered = [
            f"{i + 1}\t{line}" for i, line in enumerate(lines[start:end], start=start)
        ]
        return "\n".join(numbered) or "(empty file)"
    except Exception as exc:
        return f"error: {exc}"


def write_file(file_path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed."""
    try:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {file_path}"
    except Exception as exc:
        return f"error: {exc}"


def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace old_string with new_string in file_path.
    Fails if old_string is not unique (unless replace_all=True)."""
    try:
        p = Path(file_path)
        if not p.exists():
            return f"error: {file_path} does not exist"
        text = p.read_text(errors="replace")
        if old_string not in text:
            return f"error: old_string not found in {file_path}"
        if not replace_all:
            count = text.count(old_string)
            if count > 1:
                return (
                    f"error: old_string appears {count} times — not unique. "
                    "Provide more surrounding context or use replace_all=True."
                )
        new_text = (
            text.replace(old_string, new_string)
            if replace_all
            else text.replace(old_string, new_string, 1)
        )
        p.write_text(new_text)
        return f"edited {file_path}"
    except Exception as exc:
        return f"error: {exc}"


def grep(
    pattern: str,
    path: Optional[str] = None,
    glob: Optional[str] = None,
    output_mode: str = "files_with_matches",
    context: Optional[int] = None,
    case_insensitive: bool = False,
) -> str:
    """Search file contents using ripgrep. output_mode: files_with_matches | content | count."""
    try:
        cmd = ["rg", pattern]
        if case_insensitive:
            cmd.append("-i")
        if output_mode == "content":
            cmd.append("-n")
            if context:
                cmd.extend(["-C", str(context)])
        elif output_mode == "count":
            cmd.append("--count")
        else:
            cmd.append("-l")
        if glob:
            cmd.extend(["--glob", glob])
        if path:
            cmd.append(path)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (result.stdout or "(no matches)").rstrip()
    except subprocess.TimeoutExpired:
        return "(timed out)"
    except FileNotFoundError:
        return "(rg not available)"
    except Exception as exc:
        return f"error: {exc}"


def glob_files(pattern: str, path: Optional[str] = None) -> str:
    """Find files matching a glob pattern. Returns matching paths, one per line."""
    try:
        import glob as glob_module

        base = path or "."
        full_pattern = os.path.join(base, pattern)
        matches = sorted(glob_module.glob(full_pattern, recursive=True))
        if not matches:
            return "(no matches)"
        return "\n".join(matches[:500])
    except Exception as exc:
        return f"error: {exc}"


async def fetch_url(url: str) -> str:
    """Fetch the content of a URL and return it as text (up to 50 000 chars)."""
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True
        ) as client:
            resp = await client.get(
                url, headers={"User-Agent": "Sam-ADK/1.0 (github.com/Dembrane/sam)"}
            )
            resp.raise_for_status()
            return resp.text[:50_000]
    except Exception as exc:
        return f"error fetching {url}: {exc}"


# ─── Arc agent instruction ─────────────────────────────────────────────────────


def _load_arc_instruction() -> str:
    """Read the arc agent instruction from src/runtime/agents/arc.md.

    Returns the body (everything after the YAML frontmatter). Falls back to
    a minimal inline instruction if the file is missing — so a first-run
    before the file exists doesn't hard-fail.
    """
    arc_path = SAM_SRC / "runtime" / "agents" / "arc.md"
    if not arc_path.exists():
        log.warning("arc.md not found at %s; using fallback instruction", arc_path)
        return (
            "You are arc, a deep-reasoning partner for Sam. "
            "Analyse carefully, cite file:line, be honest about uncertainty. "
            "Return findings — do not edit files or call external APIs."
        )
    text = arc_path.read_text()
    # Strip YAML frontmatter if present
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + len("\n---\n"):]
    return text.strip()


# ─── ADK runner ───────────────────────────────────────────────────────────────


class ADKAgentRunner:
    """Runs a Sam session using Google ADK + Vertex AI Gemini.

    Builds a two-tier agent tree on each call to run():
      main (flash) calls tools and delegates to arc (pro) via AgentTool.

    Vertex AI auth must be present in the environment before the runner is
    used — specifically GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, and
    either GOOGLE_APPLICATION_CREDENTIALS or workload identity.
    """

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        # Late imports so the module loads even when google-adk isn't installed
        # (e.g. during tests that mock the runner).
        try:
            from google.adk.agents import LlmAgent
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.adk.tools import AgentTool, FunctionTool
            from google.genai import types as genai_types
        except ImportError as exc:
            raise RuntimeError(
                "google-adk is not installed. "
                "Add `google-adk` to src/runtime/requirements.txt and rebuild."
            ) from exc

        started_at = time.monotonic()
        started_at_wall = time.time()
        last_output_at: float = started_at
        tool_use_records: list[ToolUseRecord] = []
        exit_code: int = 0
        stuck = False
        timed_out = False

        # Bash closes over the session's cwd + env.
        bash_tool = _make_bash(cwd=request.cwd, env=request.env)

        # Read-only tools shared by both agents.
        ro_tools = [
            FunctionTool(func=read_file),
            FunctionTool(func=grep),
            FunctionTool(func=glob_files),
            FunctionTool(func=fetch_url),
        ]

        # Arc — the big model, read-only.
        arc_agent = LlmAgent(
            name="arc",
            model=SAM_BIG_MODEL,
            instruction=_load_arc_instruction(),
            tools=ro_tools,
        )

        # Main — small model, full tools including arc.
        main_agent = LlmAgent(
            name="sam",
            model=SAM_SMALL_MODEL,
            instruction=request.system_prompt,
            tools=[
                FunctionTool(func=bash_tool),
                *ro_tools,
                FunctionTool(func=write_file),
                FunctionTool(func=edit_file),
                AgentTool(agent=arc_agent),
            ],
        )

        session_service = InMemorySessionService()
        runner = Runner(
            agent=main_agent,
            app_name="sam",
            session_service=session_service,
        )
        session = await session_service.create_session(
            app_name="sam", user_id="sam"
        )

        user_content = genai_types.Content(
            role="user",
            parts=[genai_types.Part.from_text(text=request.initial_user_message)],
        )

        run_task: Optional[asyncio.Task] = None

        async def do_run() -> None:
            nonlocal last_output_at, exit_code
            try:
                async for event in runner.run_async(
                    user_id="sam",
                    session_id=session.id,
                    new_message=user_content,
                ):
                    last_output_at = time.monotonic()
                    _extract_tool_use(event, tool_use_records)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("ADK runner error: %s", exc)
                exit_code = 1

        async def watchdog() -> None:
            nonlocal stuck, timed_out
            while True:
                await asyncio.sleep(30)
                if run_task is None or run_task.done():
                    return
                elapsed = time.monotonic() - started_at
                idle = time.monotonic() - last_output_at
                if elapsed > MAX_SESSION_SECONDS:
                    log.warning("ADK session timed out after %ds", int(elapsed))
                    timed_out = True
                    run_task.cancel()
                    return
                if idle > STUCK_TIMEOUT_SECONDS:
                    log.warning("ADK session stuck (idle %ds)", int(idle))
                    stuck = True
                    run_task.cancel()
                    return

        run_task = asyncio.create_task(do_run())
        watch_task = asyncio.create_task(watchdog())

        try:
            await run_task
        except asyncio.CancelledError:
            pass
        finally:
            watch_task.cancel()
            try:
                await watch_task
            except asyncio.CancelledError:
                pass

        ended_at = time.monotonic()

        return AgentRunResult(
            exit_code=exit_code,
            started_at=started_at,
            started_at_wall=started_at_wall,
            ended_at=ended_at,
            last_output_at=last_output_at,
            stuck=stuck,
            timed_out=timed_out,
            stderr_tail=[],          # no subprocess stderr in ADK
            synthetic_errors=[],     # no synthetic errors in ADK; real exceptions set exit_code=1
            tool_use_records=tool_use_records,
        )


def _extract_tool_use(event: object, records: list[ToolUseRecord]) -> None:
    """Pull function_call parts from an ADK event into records.

    ADK events carry a .content attribute (google.genai.types.Content)
    whose .parts list may contain Part objects with .function_call set.
    """
    content = getattr(event, "content", None)
    if content is None:
        return
    parts = getattr(content, "parts", None) or []
    for part in parts:
        fc = getattr(part, "function_call", None)
        if fc is None:
            continue
        name = getattr(fc, "name", None) or ""
        args = getattr(fc, "args", None) or {}
        if name:
            records.append(ToolUseRecord(name=name, input=dict(args)))
