"""ADK agent runner for Sam.

Implements the AgentRunner Protocol using Google's Agent Development Kit
(ADK) with an INVERTED hybrid model setup on Vertex AI EU multi-region:
  - Main loop: Claude Opus 4.7 via ADK's Claude class (subclass injects the
    multi-region base_url AnthropicVertex doesn't infer).
  - Worker fleet: Gemini 3.1 Flash-Lite via ADK's native Gemini client,
    accessed via `worker` (single dispatch) and `parallel_workers`
    (fan-out). Workers do narrow, focused tasks — file reads, greps,
    edits, shell, parallel lookups — that don't need Opus-level reasoning.

Architecture:
  main agent — SAM_MAIN_MODEL (Claude Opus 4.7, deep reasoning + Slack reply)
    ├── bash                  run shell commands
    ├── read_file             read files from disk
    ├── write_file            write files to disk
    ├── edit_file             exact-string replacement in files
    ├── grep                  search file contents
    ├── glob_files            find files by pattern
    ├── fetch_url             fetch a URL and return content
    ├── worker                AgentTool → one worker (SAM_WORKER_MODEL, full tools sans recursion)
    │                             ├── bash, read_file, write_file, edit_file
    │                             └── grep, glob_files, fetch_url
    └── parallel_workers      fan-out: N workers via asyncio.gather
                                  each branch gets its own LlmAgent + InMemorySession
                                  results returned labeled and concatenated

Vertex AI auth: set GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION, and either
GOOGLE_APPLICATION_CREDENTIALS or ADC (gcloud auth application-default login)
in the environment. ADK's Gemini client picks up location for multi-region
routing automatically; LiteLLM gets project/location/api_base via explicit
kwargs we pass in `_generate_adk_model`.

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
    SAM_WORKER_MODEL,
    SAM_MAIN_MODEL,
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
        # noqa rationale: Sam is single-tenant; subprocess args are LLM-generated
        # but the LLM also has bash access — there's no privilege boundary to defend here.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)  # noqa: S603
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


# ─── Worker agent instruction ──────────────────────────────────────────────────


def _load_worker_instruction() -> str:
    """Read the worker agent instruction from src/runtime/agents/worker.md.

    Returns the body (everything after the YAML frontmatter). Falls back to
    a minimal inline instruction if the file is missing — so a first-run
    before the file exists doesn't hard-fail.
    """
    worker_path = SAM_SRC / "runtime" / "agents" / "worker.md"
    if not worker_path.exists():
        log.warning("worker.md not found at %s; using fallback instruction", worker_path)
        return (
            "You are a focused worker for Sam. Do the task and report what "
            "happened. You have read/write/grep/fetch/bash tools. Cite "
            "file:line. Be honest about uncertainty. Don't post to Slack — "
            "Sam handles user-facing communication."
        )
    text = worker_path.read_text()
    # Strip YAML frontmatter if present
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            text = text[end + len("\n---\n"):]
    return text.strip()


# ─── Model routing ────────────────────────────────────────────────────────────


def _generate_adk_model(model_id: str):
    """Return whatever `LlmAgent(model=...)` should receive for this model ID.

    Two paths:
    - `claude-*` → returns an ADK `Claude` instance with a subclass that
      injects the correct `base_url` for EU/US multi-region endpoints.
      Stock ADK uses AnthropicVertex's default `{region}-aiplatform.googleapis.com`
      hostname, which doesn't exist for multi-region values like "eu";
      the working host is `aiplatform.{region}.rep.googleapis.com/v1`.
    - Anything else (Gemini) → return the plain string; ADK's native Gemini
      client handles routing, including EU multi-region.

    Both paths read project/location from `GOOGLE_CLOUD_PROJECT` /
    `GOOGLE_CLOUD_LOCATION` so the .env stays single-source. Lazy-imports
    keep the module loadable in test envs where google-adk isn't installed.
    """
    if not model_id.startswith("claude-"):
        # Native ADK Gemini — pass through.
        return model_id

    from functools import cached_property
    from anthropic import AsyncAnthropicVertex
    from google.adk.models.anthropic_llm import Claude, get_tracking_headers

    class _ClaudeMultiRegion(Claude):
        @cached_property
        def _anthropic_client(self) -> AsyncAnthropicVertex:
            project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
            location = os.environ.get("GOOGLE_CLOUD_LOCATION")
            if not project_id or not location:
                raise RuntimeError(
                    "GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION must be "
                    "set for Vertex AI Claude. See .env.example."
                )
            kwargs = {
                "project_id": project_id,
                "region": location,
                "default_headers": get_tracking_headers(),
            }
            if location in ("eu", "us"):
                kwargs["base_url"] = (
                    f"https://aiplatform.{location}.rep.googleapis.com/v1"
                )
            client = AsyncAnthropicVertex(**kwargs)

            # Ephemeral prompt-cache the system prompt. ADK passes system
            # as a plain string; Anthropic also accepts a structured
            # [{text, cache_control}] form. Sam's system prompt is large
            # and stable across turns within a session — caching reads
            # the prior tokens at ~10% of base cost on subsequent calls
            # within the 5-min ephemeral window.
            original_create = client.messages.create

            async def _create_with_cache(**call_kwargs):
                sys = call_kwargs.get("system")
                if isinstance(sys, str) and sys:
                    call_kwargs["system"] = [{
                        "type": "text",
                        "text": sys,
                        "cache_control": {"type": "ephemeral"},
                    }]
                return await original_create(**call_kwargs)

            client.messages.create = _create_with_cache
            return client

    return _ClaudeMultiRegion(model=model_id)


# ─── Parallel worker fan-out ──────────────────────────────────────────────────


def _make_parallel_workers_tool(worker_instruction: str, bash_tool):
    """Return a coroutine that fans out N worker tasks in parallel via asyncio.gather.

    Each task gets a fresh LlmAgent(worker) + InMemorySessionService so there
    is no shared state between branches. Results are returned in order,
    labeled by task number, and concatenated with a separator.

    Use this when you have 2+ independent narrow tasks — e.g. "read file A"
    and "search for pattern B simultaneously". Do not use for sequential
    tasks where one result feeds the next (call worker twice instead).

    Workers have write access (write_file, edit_file, bash) — only the
    recursive tools (worker, parallel_workers) are withheld to prevent
    exponential fan-out.
    """

    async def parallel_workers(tasks: list[str]) -> str:
        """Run each task in parallel against a fresh worker (small model) agent.

        tasks: list of self-contained task descriptions. Each is dispatched to
        its own worker instance simultaneously. Returns all results labeled
        and concatenated.
        """
        try:
            from google.adk.agents import LlmAgent
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService
            from google.adk.tools import FunctionTool
            from google.genai import types as genai_types
        except ImportError as exc:
            return f"error: google-adk not installed — {exc}"

        if not tasks:
            return "error: no tasks provided"

        async def run_one(task: str, idx: int) -> str:
            worker = LlmAgent(
                name=f"worker_{idx}",
                model=_generate_adk_model(SAM_WORKER_MODEL),
                instruction=worker_instruction,
                tools=[
                    FunctionTool(func=bash_tool),
                    FunctionTool(func=read_file),
                    FunctionTool(func=write_file),
                    FunctionTool(func=edit_file),
                    FunctionTool(func=grep),
                    FunctionTool(func=glob_files),
                    FunctionTool(func=fetch_url),
                ],
            )
            svc = InMemorySessionService()
            worker_runner = Runner(
                agent=worker, app_name="sam_fanout", session_service=svc
            )
            session = await svc.create_session(
                app_name="sam_fanout", user_id="sam"
            )
            user_content = genai_types.Content(
                role="user",
                parts=[genai_types.Part.from_text(text=task)],
            )
            result_parts: list[str] = []
            try:
                async for event in worker_runner.run_async(
                    user_id="sam",
                    session_id=session.id,
                    new_message=user_content,
                ):
                    content = getattr(event, "content", None)
                    if content:
                        for part in (getattr(content, "parts", None) or []):
                            text = getattr(part, "text", None)
                            if text:
                                result_parts.append(text)
            except Exception as exc:
                return f"(worker_{idx} error: {exc})"
            return "".join(result_parts) or "(no output)"

        results = await asyncio.gather(*[run_one(t, i + 1) for i, t in enumerate(tasks)])
        sections = [
            f"**worker task {i + 1}:** {task}\n\n{result}"
            for i, (task, result) in enumerate(zip(tasks, results))
        ]
        return "\n\n---\n\n".join(sections)

    return parallel_workers


# ─── ADK runner ───────────────────────────────────────────────────────────────


class ADKAgentRunner:
    """Runs a Sam session using Google ADK on Vertex AI.

    Inverted architecture: main is Claude Opus 4.7 (deep reasoning, Slack
    composition); workers are Gemini 3.1 Flash-Lite (fast, focused tasks)
    accessible via the `worker` (single) and `parallel_workers` (fan-out)
    tools.

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

        # Tools shared between main and workers. Workers get the same tools
        # except for the recursive `worker` / `parallel_workers` (which would
        # create exponential fan-out).
        worker_tools = [
            FunctionTool(func=bash_tool),
            FunctionTool(func=read_file),
            FunctionTool(func=write_file),
            FunctionTool(func=edit_file),
            FunctionTool(func=grep),
            FunctionTool(func=glob_files),
            FunctionTool(func=fetch_url),
        ]

        worker_instruction = _load_worker_instruction()

        # `worker` — single dispatch to one Flash-Lite worker.
        worker_agent = LlmAgent(
            name="worker",
            model=_generate_adk_model(SAM_WORKER_MODEL),
            instruction=worker_instruction,
            tools=worker_tools,
        )

        # `parallel_workers` — fan-out to N fresh workers concurrently.
        parallel_workers = _make_parallel_workers_tool(
            worker_instruction, bash_tool
        )

        # Main — Opus 4.7, full tools including worker dispatch.
        main_agent = LlmAgent(
            name="sam",
            model=_generate_adk_model(SAM_MAIN_MODEL),
            instruction=request.system_prompt,
            tools=[
                *worker_tools,
                AgentTool(agent=worker_agent),
                FunctionTool(func=parallel_workers),
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
