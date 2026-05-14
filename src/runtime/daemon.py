"""
Sam's runtime daemon.

Responsibilities:
- Listen for Slack events (Socket Mode, Agents & AI Apps surface)
- For each incoming message that wakes Sam up, launch a Claude Code session
- Stream Sam's output back to Slack as it arrives
- Route in-thread replies by asking Slack whether Sam has posted in the thread
  (no local thread bookkeeping; cached in-memory only)
- Redirect messages received in the Agents & AI Apps side-pane back to the
  whitelisted channel
- On startup, replay messages missed while the daemon was offline
- Maintain a single-instance lock and a journal safety net
- Detect stuck sessions and clean them up

What this daemon does NOT do:
- Reason about anything. All reasoning happens inside Sam (the Claude Code session).
- Talk to GitHub, Linear, or any external API except Slack. Sam does that itself.
- Modify Sam's source. The daemon is tier-3 substrate; Sam doesn't touch it either.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

load_dotenv()

SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SAM_CHANNEL = os.environ.get("SAM_CHANNEL")  # optional: restrict to one channel for v0
SAM_OPERATOR_USER_ID = os.environ.get("SAM_OPERATOR_USER_ID")  # @-mentioned when both attempts fail
SAM_HOME = Path(os.environ.get("SAM_HOME", "/data"))
SAM_REPO = Path("/home/sam")          # where Sam's checkout lives in the container
SAM_SRC = SAM_REPO / "src"            # identity, scope, capabilities, skills, runtime

JOURNAL_DIR = SAM_HOME / "journal"
# Pre-directory combined journal lives alongside the new directory and stays
# greppable. Sam reads both `data/journal.md` and `data/journal/*.md`.
LEGACY_JOURNAL_PATH = SAM_HOME / "journal.md"
LOCK_PATH = SAM_HOME / "sam.lock"
REPOS_DIR = SAM_HOME / "repos"
CURSOR_PATH = SAM_HOME / "cursor.json"


def journal_path_for_today() -> Path:
    """Path to today's journal file. The directory is created if missing."""
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return JOURNAL_DIR / f"{datetime.now().date().isoformat()}.md"


def _read_commit_sha() -> Optional[str]:
    """Return the short commit SHA of the source the daemon is running from.

    Read once at module import. Stable for the process lifetime — the
    container is rebuilt to pick up new code, so the SHA doesn't shift
    mid-run. Returns None if `git` isn't available or the working tree
    isn't a git checkout (e.g. running outside docker for tests).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, cwd=str(SAM_REPO),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


COMMIT_SHA: Optional[str] = _read_commit_sha()

# How long without any stdout/stderr output before we consider Sam stuck
STUCK_TIMEOUT_SECONDS = 10 * 60  # 10 minutes
# Hard cap on a single session's wall clock
MAX_SESSION_SECONDS = 60 * 60     # 1 hour
# How long to cache "has the bot posted in this thread?" lookups
THREAD_CACHE_TTL_SECONDS = 5 * 60
# How many stderr lines we keep per session, to feed to a retry session on failure
STDERR_TAIL_LINES = 40
# How many synthetic-error messages we keep per session (Claude Code surfaces
# API errors and similar internal failures as assistant messages with
# `model: "<synthetic>"`. We capture them to brief retry sessions.)
SYNTHETIC_ERRORS_MAX = 10
# Subtypes accepted as real user messages
ALLOWED_MESSAGE_SUBTYPES = {None, "thread_broadcast", "file_share"}
# Subtypes we explicitly log-and-drop so they're visible in debugging
NOISY_MESSAGE_SUBTYPES = {"message_changed", "message_deleted", "bot_message"}

# Secret-redaction: minimum env value length to consider for redaction.
# Short values produce too many false positives in normal text
# (e.g. TZ=Europe/Amsterdam, SAM_HOME=/data, country codes).
REDACT_MIN_LEN = 8
REDACT_PLACEHOLDER = "xxxx"

# -----------------------------------------------------------------------------
# Secret redaction — defense-in-depth for anything the daemon posts to Slack
# -----------------------------------------------------------------------------

def _build_redaction_values() -> list[str]:
    """Collect env var values worth scrubbing from outbound Slack content.

    Skips values shorter than `REDACT_MIN_LEN` (would clobber benign matches).
    Sorted longest-first so a substring of a longer secret can't get partially
    redacted before the longer secret is matched.
    """
    values = {v for v in os.environ.values() if v and len(v) >= REDACT_MIN_LEN}
    return sorted(values, key=len, reverse=True)


_REDACT_VALUES: list[str] = _build_redaction_values()


def redact_secrets(text: Optional[str]) -> str:
    """Replace any env var value found in `text` with the redaction placeholder.

    Defense-in-depth for daemon-originated Slack posts. Sam should never put
    a secret into a message in the first place; this catches it if Sam ever
    does, before the bytes leave the process.

    Built once at import from the live env. If the env changes after import,
    the table is stale — acceptable, since the daemon's env doesn't change
    at runtime.
    """
    if not text:
        return text or ""
    out = text
    for v in _REDACT_VALUES:
        if v in out:
            out = out.replace(v, REDACT_PLACEHOLDER)
    return out

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sam.daemon")

# -----------------------------------------------------------------------------
# Lock — single-instance enforcement
# -----------------------------------------------------------------------------

class LockError(Exception):
    pass

def acquire_lock() -> None:
    """Acquire the daemon-level lock. Refuses to start a second daemon."""
    if LOCK_PATH.exists():
        try:
            pid = int(LOCK_PATH.read_text().strip())
        except (ValueError, OSError):
            log.warning("lock file unreadable, treating as stale")
            LOCK_PATH.unlink(missing_ok=True)
        else:
            if _pid_alive(pid):
                raise LockError(f"another daemon is running (pid {pid})")
            log.warning("stale lock from dead pid %s, cleaning up", pid)
            LOCK_PATH.unlink(missing_ok=True)
    SAM_HOME.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(os.getpid()))

def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

# -----------------------------------------------------------------------------
# Cursor persistence (a single Slack ts — the high-water mark for catch-up)
# -----------------------------------------------------------------------------

def load_cursor() -> Optional[str]:
    """Read the last-seen Slack ts from disk. None on first run."""
    if not CURSOR_PATH.exists():
        return None
    try:
        return json.loads(CURSOR_PATH.read_text()).get("last_seen_ts")
    except (json.JSONDecodeError, OSError):
        log.warning("cursor.json unreadable, treating as first run")
        return None

def save_cursor(ts: str) -> None:
    SAM_HOME.mkdir(parents=True, exist_ok=True)
    current = load_cursor()
    # Only advance the cursor — never rewind it.
    if current and float(ts) <= float(current):
        return
    CURSOR_PATH.write_text(json.dumps({"last_seen_ts": ts}))

# -----------------------------------------------------------------------------
# System prompt assembly
# -----------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse simple YAML-ish frontmatter from the top of a markdown file.

    Supports only single-line `key: value` pairs (no nested structures, no
    multi-line scalars). Returns (metadata, body). If no frontmatter is
    present, returns ({}, text).
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    head = text[4:end]
    body = text[end + len("\n---\n"):]
    meta: dict[str, str] = {}
    for line in head.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        meta[key.strip()] = val
    return meta, body


def _build_skill_catalog(skills_dir: Path) -> str:
    """Emit a frontmatter-only listing of skills.

    Skills are NOT hot-loaded into the system prompt. The model sees only
    name + description + when_to_use + path, and decides whether to Read
    the full skill body when relevant.
    """
    entries: list[str] = []
    for skill in sorted(skills_dir.glob("*.md")):
        meta, _ = _parse_frontmatter(skill.read_text())
        name = meta.get("name") or skill.stem
        desc = meta.get("description")
        when = meta.get("when_to_use") or meta.get("when to use")
        cron_expr = meta.get("cron")
        if not desc:
            log.warning("skill %s missing 'description' in frontmatter", skill)
            desc = "(no description)"
        rel = skill.relative_to(SAM_REPO)
        line = f"- **{name}** — {desc}"
        if when:
            line += f"\n  *when to use:* {when}"
        if cron_expr:
            line += f"\n  *also scheduled by the daemon:* `{cron_expr}` (cron). When this fires you'll see a SCHEDULED SKILL invocation block at the top of your conversation."
        line += f"\n  *full content:* `Read {rel}`"
        entries.append(line)
    if not entries:
        return ""
    header = (
        "# SKILLS (catalog only)\n\n"
        "Skills are patterns Sam has learned. The listing below shows names "
        "and descriptions; bodies are NOT included in this prompt. When a "
        "skill matches your task, `Read` the listed path BEFORE applying it. "
        "Don't guess what's in a skill — read it.\n"
    )
    return header + "\n" + "\n\n".join(entries)


def assemble_system_prompt() -> str:
    """Build the system prompt for a Claude Code session.

    Hot-loads identity, scope, and capabilities (stable, always relevant).
    Skills are catalog-only — model reads them on demand.

    Re-read every session, so a `git pull` followed by next message picks up
    the new version of Sam.
    """
    sections: list[str] = []

    def _add(path: Path, header: str) -> None:
        if path.exists():
            sections.append(f"# {header}\n\n{path.read_text()}")

    _add(SAM_SRC / "identity.md", "IDENTITY")
    _add(SAM_SRC / "scope.md", "SCOPE")

    for cap in sorted((SAM_SRC / "capabilities").glob("*.md")):
        sections.append(f"# CAPABILITY: {cap.stem}\n\n{cap.read_text()}")

    skills_dir = SAM_SRC / "skills"
    if skills_dir.exists():
        catalog = _build_skill_catalog(skills_dir)
        if catalog:
            sections.append(catalog)

    orchestration = """
# ORCHESTRATION

You are Sam, running as a Claude Code session. Each time you wake up, you
are responding to a Slack message that came in via the daemon.

You are running source at commit `{commit_sha}`. When you propose a Tier 3
(runtime) PR or talk publicly about behaviour changes, quote this commit so
observers can tell whether what they're seeing is "live" or "pending the
next container restart."

Before responding, decide what context you need and go get it. Use:
- The journal: one file per day at `/data/journal/<YYYY-MM-DD>.md`. Today's
  file is where you'll write this session's entry. The pre-directory combined
  journal is still at `/data/journal.md` (kept in place for grep continuity).
  When looking back, grep across BOTH `/data/journal/` AND `/data/journal.md`.
- The Linear API for issues and comments (`LINEAR_API_KEY` is in env)
- The GitHub API and `gh` CLI for PRs, issues, code (`GITHUB_TOKEN` is in env)
- The Slack Web API for posting back (`SLACK_BOT_TOKEN` is in env)
- Repos cloned under `/data/repos/` (clone what you need, fetch what's stale)
- Sam's own source under `src/` — identity, scope, capabilities, skills

The Slack message that triggered this session is at the start of your
conversation. The channel ID and thread timestamp are included so you can
post back in the right place. The sender's display name and principal-
operator status are also included — use them verbatim in journal entries.
Do NOT infer a sender's identity from the `<@U…>` mention alone; the
daemon already resolved the name for you.

If the start of your conversation is a "SCHEDULED SKILL invocation" block
instead of a Slack message, the daemon's scheduler triggered a skill that
has a `cron:` field in its frontmatter. Read the named skill file and
follow it. Silence (post nothing) is an acceptable answer for scheduled
wake-ups; only post when there's substance.

Before you finish, append a journal entry to today's file at
`/data/journal/<YYYY-MM-DD>.md`. Follow the format in
`src/capabilities/journal.md`. This is how future-you remembers.

Be honest, terse, and useful. Don't perform engagement. Don't explain what
you're about to do unless someone is going to be watching the status
indicator. Just do it, then say the result.
"""
    sections.append(orchestration.format(commit_sha=COMMIT_SHA or "unknown"))

    return "\n\n---\n\n".join(sections)

# -----------------------------------------------------------------------------
# Slack helpers
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
        scheduler from SCHEDULED_CHECKIN_PROMPT).
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
            "A previous Sam session attempting to respond to a Slack message FAILED.",
            "Do not retry the original task. Your one job in this session is:",
            "",
            f"1. Read the failure context below.",
            f"2. Post ONE reply in the original Slack thread (channel={self.channel}, thread_ts={thread_target}) in your normal Slack voice. Name what failed in human terms, name the likely cause, and suggest a concrete fix. Don't dump stderr verbatim — read it, summarise it.",
            "3. Then stop. This is a one-shot. The daemon will not retry again.",
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
        lines.append(
            "Remember: post ONCE, in the original thread, in your Slack voice. "
            "Be honest about the failure. Suggest a fix if you can see one. "
            "Prefer the synthetic errors above over stderr when identifying the cause. "
            "Then exit."
        )
        return "\n".join(lines)

# -----------------------------------------------------------------------------
# Sam session — one Claude Code subprocess per Slack interaction
# -----------------------------------------------------------------------------

@dataclass
class SessionResult:
    session_id: str
    started_at: float
    ended_at: float
    exit_code: Optional[int]
    last_output_at: float
    stuck: bool
    timed_out: bool
    stderr_tail: list[str] = field(default_factory=list)
    synthetic_errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.stuck or self.timed_out or bool(self.exit_code)

class SamSession:
    """Runs one Claude Code session against one incoming message.

    Reads identity/scope/capabilities/skills fresh, builds the system prompt,
    pipes the Slack message in, lets Sam stream out, and watches for stuck.
    """

    def __init__(self, message: IncomingMessage):
        self.message = message
        self.session_id = uuid.uuid4().hex[:12]
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.last_output_at = time.monotonic()
        # Ring buffer of recent stderr — used to brief a retry session if this one fails
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        # Synthetic error messages observed in stdout (claude code surfaces API
        # errors, etc., as assistant messages with model="<synthetic>")
        self.synthetic_errors: deque[str] = deque(maxlen=SYNTHETIC_ERRORS_MAX)

    async def run(self) -> SessionResult:
        system_prompt = assemble_system_prompt()
        initial_user_message = self.message.to_initial_user_message()

        log.info(
            "session %s starting (channel=%s thread_ts=%s)",
            self.session_id, self.message.channel, self.message.thread_ts
        )

        started_at = time.monotonic()

        # Build the input the Claude CLI expects on stdin (stream-json input)
        # Format: one JSON object per line, each being {type: "user", message: {role: "user", content: "..."}}
        user_input = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": initial_user_message}
        }) + "\n"

        env = os.environ.copy()
        # Sam runs from its own home so relative paths in capability files work
        cwd = str(SAM_REPO)

        self.proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",                                  # print/non-interactive
            "--verbose",                           # required by -p + stream-json output
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--system-prompt", system_prompt,
            "--allowed-tools", "Bash,Read,Write,Edit,Grep,Glob,WebFetch,WebSearch",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )

        # Send the user message and close stdin (Sam will respond and exit)
        assert self.proc.stdin is not None
        self.proc.stdin.write(user_input.encode())
        await self.proc.stdin.drain()
        self.proc.stdin.close()

        # Run stdout reader, stderr reader, and stuck watcher concurrently
        stuck = False
        timed_out = False

        async def read_stdout() -> None:
            assert self.proc and self.proc.stdout
            async for line in self.proc.stdout:
                self.last_output_at = time.monotonic()
                raw = line.decode().strip()
                if not raw:
                    continue
                log.debug("stdout: %s", raw[:200])
                # Stream-json: try to parse and extract synthetic error messages.
                # These are claude code's way of surfacing API errors and similar
                # internal failures — exactly the signal a retry session needs.
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "assistant":
                    continue
                msg = event.get("message") or {}
                if msg.get("model") != "<synthetic>":
                    continue
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = (block.get("text") or "").strip()
                        if text:
                            self.synthetic_errors.append(text[:1000])
                            log.warning("synthetic error: %s", text[:200])

        async def read_stderr() -> None:
            assert self.proc and self.proc.stderr
            async for line in self.proc.stderr:
                self.last_output_at = time.monotonic()
                text = line.decode().rstrip()[:500]
                self.stderr_tail.append(text)
                log.warning("stderr: %s", text[:200])

        async def watch_stuck() -> None:
            nonlocal stuck, timed_out
            while self.proc and self.proc.returncode is None:
                await asyncio.sleep(30)
                elapsed = time.monotonic() - started_at
                idle = time.monotonic() - self.last_output_at
                if elapsed > MAX_SESSION_SECONDS:
                    log.warning("session %s exceeded max wall time, killing", self.session_id)
                    timed_out = True
                    self._kill()
                    return
                if idle > STUCK_TIMEOUT_SECONDS:
                    log.warning("session %s stuck (no output for %ds), killing", self.session_id, int(idle))
                    stuck = True
                    self._kill()
                    return

        await asyncio.gather(
            read_stdout(),
            read_stderr(),
            watch_stuck(),
            return_exceptions=True,
        )

        exit_code = await self.proc.wait()
        ended_at = time.monotonic()

        log.info("session %s ended (exit=%s stuck=%s timed_out=%s)",
                 self.session_id, exit_code, stuck, timed_out)

        result = SessionResult(
            session_id=self.session_id,
            started_at=started_at,
            ended_at=ended_at,
            exit_code=exit_code,
            last_output_at=self.last_output_at,
            stuck=stuck,
            timed_out=timed_out,
            stderr_tail=list(self.stderr_tail),
            synthetic_errors=list(self.synthetic_errors),
        )

        # Safety net journal entry — only if the session didn't write one,
        # which we approximate by checking exit code / stuck / timeout.
        # Sam writing its own entry is the happy path; this is the fallback.
        if stuck or timed_out or (exit_code and exit_code != 0):
            self._safety_net_journal_entry(result)

        return result

    def _kill(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass

    def _safety_net_journal_entry(self, result: SessionResult) -> None:
        """Append a minimal journal entry when something went wrong.

        Sam should write its own entries normally. This is for the cases where
        Sam was killed or crashed and couldn't.
        """
        from datetime import datetime
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

# -----------------------------------------------------------------------------
# Daemon — Slack listener + session queue
# -----------------------------------------------------------------------------

class Daemon:
    def __init__(self):
        self.app = AsyncApp(token=SLACK_BOT_TOKEN)
        self.handler = AsyncSocketModeHandler(self.app, SLACK_APP_TOKEN)
        self.queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()
        self.session_lock = asyncio.Lock()
        self.shutdown_event = asyncio.Event()
        self.bot_user_id: Optional[str] = None
        self.sam_channel_name: Optional[str] = None
        # Recent message ts values we've already queued, to dedupe app_mention
        # and message events that fire for the same underlying Slack message.
        self._seen_ts: set[str] = set()
        self._seen_ts_order: list[str] = []
        # In-memory cache: 'channel:thread_ts' -> (bot_has_posted, cached_at)
        self._thread_cache: dict[str, tuple[bool, float]] = {}
        # Channel ids seen via assistant_thread_started; used as a fallback to
        # detect side-pane messages that don't carry an `assistant_thread` field.
        self._assistant_thread_channels: set[str] = set()
        # In-memory cache: slack user_id -> (display_name, is_principal_operator)
        # Cleared on daemon restart; display names rarely change so this is fine.
        self._user_cache: dict[str, tuple[str, bool]] = {}

        self._register_handlers()

    def _mark_seen(self, ts: str) -> bool:
        """Returns True if this ts is new; False if already queued."""
        if ts in self._seen_ts:
            return False
        self._seen_ts.add(ts)
        self._seen_ts_order.append(ts)
        if len(self._seen_ts_order) > 1024:
            old = self._seen_ts_order.pop(0)
            self._seen_ts.discard(old)
        return True

    def _channel_allowed(self, channel: Optional[str]) -> bool:
        if not channel:
            return False
        if SAM_CHANNEL and channel != SAM_CHANNEL:
            return False
        return True

    def _is_side_pane_event(self, event: dict) -> bool:
        """Detect Agents & AI Apps assistant-thread (side-pane) messages.

        Slack delivers side-pane messages to a separate channel id from
        the workspace channel. So anything arriving on the whitelisted
        SAM_CHANNEL is, by definition, NOT a side-pane event — even when
        Slack staples an `assistant_thread` field onto it as workspace-
        level metadata (which it does for assistant-type apps).

        After that short-circuit, two signals: an `assistant_thread`
        field on the event, or a channel id we've previously seen open
        via `assistant_thread_started`.
        """
        channel = event.get("channel")
        if SAM_CHANNEL and channel == SAM_CHANNEL:
            return False
        if event.get("assistant_thread"):
            return True
        return bool(channel and channel in self._assistant_thread_channels)

    async def _resolve_user(self, user_id: str) -> tuple[str, bool]:
        """Return (display_name, is_principal_operator) for a Slack user id.

        Hits `users.info` once per user_id and caches in-memory for the
        process lifetime. Falls back to the raw user_id as the display name
        if the API call fails — the daemon never blocks queuing on a
        resolution failure, since `is_principal_operator` is still
        computable from env regardless.
        """
        cached = self._user_cache.get(user_id)
        if cached is not None:
            return cached
        display_name = user_id
        try:
            resp = await self.app.client.users_info(user=user_id)
        except Exception:
            log.exception("users.info failed for %s; falling back to id", user_id)
            resp = None
        if resp:
            u = resp.get("user") or {}
            profile = u.get("profile") or {}
            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or u.get("name")
                or user_id
            )
        is_principal = bool(SAM_OPERATOR_USER_ID) and user_id == SAM_OPERATOR_USER_ID
        resolved = (display_name, is_principal)
        self._user_cache[user_id] = resolved
        return resolved

    def _coalesce_thread_batch(self, first: IncomingMessage) -> IncomingMessage:
        """Drain queue siblings in the same thread and merge into one prompt.

        Goal: when a user sends two or three rapid follow-ups in the same
        thread (typing → "wait, actually…" → "and one more thing"), Sam
        should reply once to the combined set, not three times.

        Scheduled and retry messages never coalesce. Non-related queued
        messages are put back on the queue in receive order.

        The returned IncomingMessage carries the first message's identity
        and thread_ts (so the reply targets the correct thread), the
        latest message's event_ts (so any top-level reply lands at the
        most recent message), files concatenated across the batch, and
        a text body that labels each message with its source ts so Sam
        can refer to them individually.
        """
        if first.scheduled or first.retry_context:
            return first
        first_thread_key = (first.channel, first.thread_ts or first.event_ts)
        batch: list[IncomingMessage] = [first]
        keep: list[IncomingMessage] = []
        while True:
            try:
                other = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (
                other.scheduled
                or other.retry_context
                or other.channel != first.channel
            ):
                keep.append(other)
                continue
            other_thread_key = (other.channel, other.thread_ts or other.event_ts)
            if other_thread_key == first_thread_key:
                batch.append(other)
            else:
                keep.append(other)
        # Put non-related messages back in the order we received them.
        for m in keep:
            self.queue.put_nowait(m)
        if len(batch) == 1:
            return first
        log.info(
            "coalesced %d messages in thread (channel=%s thread_ts=%s)",
            len(batch), first.channel, first.thread_ts or first.event_ts,
        )
        parts: list[str] = [
            (
                f"The user sent {len(batch)} messages in rapid succession in this thread. "
                "Read them together and respond ONCE — don't reply to each one separately."
            ),
            "",
        ]
        for i, m in enumerate(batch, 1):
            parts.append(f"--- message {i}/{len(batch)} (ts={m.event_ts}) ---")
            parts.append(m.text or "(no text)")
            parts.append("")
        merged_files: list[dict] = []
        for m in batch:
            merged_files.extend(m.files)
        return IncomingMessage(
            channel=first.channel,
            user=first.user,
            text="\n".join(parts).rstrip(),
            thread_ts=first.thread_ts,
            event_ts=batch[-1].event_ts,
            files=merged_files,
            display_name=first.display_name,
            is_principal_operator=first.is_principal_operator,
            raw_event=first.raw_event,
        )

    async def _bot_participates_in_thread(self, channel: str, thread_ts: str) -> bool:
        """Has the bot ever posted in this thread?

        Cached in-memory with TTL. On Slack API failure, returns False
        (conservative — better to miss a reply than to spam from a
        confused state).
        """
        key = f"{channel}:{thread_ts}"
        cached = self._thread_cache.get(key)
        if cached is not None and time.monotonic() - cached[1] < THREAD_CACHE_TTL_SECONDS:
            return cached[0]
        try:
            resp = await self.app.client.conversations_replies(
                channel=channel, ts=thread_ts, limit=200,
            )
        except Exception:
            log.exception("conversations.replies failed for %s", key)
            return False
        participates = False
        for msg in resp.get("messages", []):
            if msg.get("bot_id") or msg.get("user") == self.bot_user_id:
                participates = True
                break
        self._thread_cache[key] = (participates, time.monotonic())
        return participates

    async def _post_eyes_reaction(self, channel: str, ts: str) -> None:
        """Add :eyes: to the inbound message so the user knows it landed.

        Fired at queue time, BEFORE any session work. Bridges the gap between
        the user hitting send and Sam actually running — especially noticeable
        when there's a backlog and the message sits in the queue for a while.
        Best-effort: `already_reacted` (double-fire on dedup races) and
        `message_not_found` are both non-fatal — log and move on.
        """
        try:
            await self.app.client.reactions_add(
                channel=channel, timestamp=ts, name="eyes",
            )
        except Exception as e:
            log.debug("reactions.add failed for %s/%s: %s", channel, ts, e)

    async def _set_thinking_status(self, channel: str, thread_ts: str) -> None:
        """Set 'is thinking…' status at session start.

        Bridges the gap between the worker pulling a message off the queue
        and Sam's first `setStatus` call from inside the session (which
        normally happens after the session has read context, opened files,
        etc. — several seconds in). Sam overwrites this with a more specific
        status as soon as it knows what it's doing.

        Slack prepends the bot's display name to the status string, so the
        status text starts with the verb. Passing "sam is thinking…" here
        renders as "sam sam is thinking…" in the UI.
        """
        try:
            await self.app.client.assistant_threads_setStatus(
                channel_id=channel,
                thread_ts=thread_ts,
                status="is thinking…",
            )
        except Exception as e:
            log.debug("setStatus failed for %s/%s: %s", channel, thread_ts, e)

    async def _send_side_pane_redirect(self, channel: str) -> None:
        target = f"#{self.sam_channel_name}" if self.sam_channel_name else "the configured channel"
        text = f"i live in {target} — talk to me there, not here."
        try:
            await self.app.client.chat_postMessage(channel=channel, text=redact_secrets(text))
            log.info("redirected side-pane message in %s", channel)
        except Exception:
            log.exception("failed to post side-pane redirect to %s", channel)

    def _register_handlers(self) -> None:
        @self.app.event("app_mention")
        async def on_app_mention(event, client):
            await self._handle_event(event)

        @self.app.event("message")
        async def on_message(event, client):
            subtype = event.get("subtype")
            if subtype in NOISY_MESSAGE_SUBTYPES:
                log.debug("ignoring %s in %s", subtype, event.get("channel"))
                return
            if subtype not in ALLOWED_MESSAGE_SUBTYPES:
                log.debug("ignoring message subtype=%s", subtype)
                return
            if event.get("bot_id") or event.get("user") == self.bot_user_id:
                return
            channel = event.get("channel")
            if not channel:
                return
            # Side-pane (Agents & AI Apps) — redirect the user to the channel.
            if self._is_side_pane_event(event):
                await self._send_side_pane_redirect(channel)
                return
            if not self._channel_allowed(channel):
                return
            # In a channel: only act on thread replies in threads where the
            # bot has already posted. Top-level non-mention chatter is
            # ignored on purpose (app_mention handles new mentions).
            thread_ts = event.get("thread_ts")
            if not thread_ts:
                return
            if not await self._bot_participates_in_thread(channel, thread_ts):
                return
            await self._handle_event(event)

        @self.app.event("assistant_thread_started")
        async def on_assistant_thread_started(event, client):
            thread = event.get("assistant_thread") or {}
            channel_id = thread.get("channel_id")
            if channel_id:
                self._assistant_thread_channels.add(channel_id)
                await self._send_side_pane_redirect(channel_id)
            log.info("assistant_thread_started in %s", channel_id)

        @self.app.event("reaction_added")
        async def on_reaction_added(event, client):
            log.info("reaction added: %s on %s", event.get("reaction"), event.get("item"))

        @self.app.event("reaction_removed")
        async def on_reaction_removed(event, client):
            log.info("reaction removed: %s on %s", event.get("reaction"), event.get("item"))

    async def _handle_event(self, event: dict) -> None:
        ts = event.get("ts")
        if not ts:
            return
        # Dedup across handlers (app_mention + message can both fire for the
        # same message when a user @mentions inside a thread).
        if not self._mark_seen(ts):
            return

        channel = event.get("channel")
        if not self._channel_allowed(channel):
            log.info("ignoring event from non-whitelisted channel %s", channel)
            return

        user = event.get("user")
        if not user:
            return

        display_name, is_principal = await self._resolve_user(user)

        files = event.get("files") or []
        message = IncomingMessage(
            channel=channel,
            user=user,
            text=event.get("text", ""),
            thread_ts=event.get("thread_ts"),
            event_ts=ts,
            files=files,
            display_name=display_name,
            is_principal_operator=is_principal,
            raw_event=event,
        )
        # Pre-warm the thread-participation cache so subsequent replies in
        # this thread route without a Slack round-trip.
        self._thread_cache[f"{channel}:{message.thread_ts or ts}"] = (
            True, time.monotonic(),
        )
        save_cursor(ts)
        # Acknowledge receipt with :eyes: immediately, before the worker even
        # picks the message up. Fire-and-forget — don't slow the queue down.
        asyncio.create_task(self._post_eyes_reaction(channel, ts))
        await self.queue.put(message)
        attachment_note = f" with {len(files)} attachment(s)" if files else ""
        principal_note = " [principal]" if message.is_principal_operator else ""
        log.info("queued message from %s (<@%s>)%s in %s (ts=%s)%s",
                 message.display_name or "?", message.user, principal_note,
                 message.channel, message.event_ts, attachment_note)

    async def _catch_up(self) -> None:
        """Replay messages missed while the daemon was offline.

        Scans the whitelisted channel's history since the cursor:
        - Top-level messages that @mention the bot are queued directly.
        - Top-level messages whose `reply_users` includes the bot have their
          replies fetched and queued (replies newer than the cursor).

        Limitation: a thread whose parent ts is older than the cursor but
        which received replies during downtime won't be discovered here.
        Re-@mentioning in the thread surfaces it again. This is the trade-off
        for not persisting any thread list locally.
        """
        last_seen = load_cursor()
        if last_seen is None:
            log.info("no cursor on disk; skipping catch-up (first run)")
            save_cursor(f"{time.time():.6f}")
            return

        if not SAM_CHANNEL:
            log.info("catch-up: SAM_CHANNEL unset, skipping")
            return

        log.info("catch-up: scanning since ts=%s", last_seen)
        client = self.app.client
        candidates: list[dict] = []

        try:
            resp = await client.conversations_history(
                channel=SAM_CHANNEL,
                oldest=last_seen,
                inclusive=False,
                limit=200,
            )
            top_level = resp.get("messages", [])
        except Exception:
            log.exception("catch-up: history fetch failed")
            top_level = []

        for msg in top_level:
            if msg.get("subtype") not in ALLOWED_MESSAGE_SUBTYPES:
                continue
            if msg.get("bot_id") or msg.get("user") == self.bot_user_id:
                continue
            text = msg.get("text", "")
            if self.bot_user_id and f"<@{self.bot_user_id}>" in text:
                msg["channel"] = SAM_CHANNEL
                candidates.append(msg)

        # Threads with bot participation that gained replies during downtime.
        for msg in top_level:
            if msg.get("reply_count", 0) <= 0:
                continue
            if self.bot_user_id not in (msg.get("reply_users") or []):
                continue
            thread_ts = msg.get("ts")
            if not thread_ts:
                continue
            try:
                replies = await client.conversations_replies(
                    channel=SAM_CHANNEL,
                    ts=thread_ts,
                    oldest=last_seen,
                    inclusive=False,
                    limit=200,
                )
            except Exception:
                log.exception("catch-up: replies fetch failed for thread %s", thread_ts)
                continue
            for reply in replies.get("messages", []):
                if reply.get("ts") == thread_ts:
                    continue
                if reply.get("subtype") not in ALLOWED_MESSAGE_SUBTYPES:
                    continue
                if reply.get("bot_id") or reply.get("user") == self.bot_user_id:
                    continue
                reply["channel"] = SAM_CHANNEL
                candidates.append(reply)

        # Chronological, deduped by ts.
        candidates.sort(key=lambda m: float(m["ts"]))
        seen: set[str] = set()
        queued = 0
        for msg in candidates:
            if msg["ts"] in seen:
                continue
            seen.add(msg["ts"])
            await self._handle_event(msg)
            queued += 1
        log.info("catch-up: queued %d message(s)", queued)

    async def _worker(self) -> None:
        """Single worker that drains the queue, one session at a time.

        On failure (exit != 0, stuck, or timed_out), the worker runs ONE
        retry session whose only job is to read the failure context and
        post a single explanatory reply in the original thread. If the
        retry also fails, the daemon posts an operator-alert directly.
        """
        while not self.shutdown_event.is_set():
            try:
                message = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # Drain any other queued messages that belong to the same thread
            # (or are top-level from the same user in the same channel within
            # the queue at this moment). If we find any, combine them into one
            # IncomingMessage so Sam responds once to the whole batch rather
            # than replying N times to N rapid follow-ups.
            message = self._coalesce_thread_batch(message)

            async with self.session_lock:
                # Set a placeholder status before the session starts so the
                # user sees "sam is thinking…" in the gap before Sam's own
                # `setStatus` call kicks in (Slack prepends the bot name to
                # the status text). Sam overwrites this shortly.
                await self._set_thinking_status(
                    message.channel, message.thread_ts or message.event_ts,
                )
                first = SamSession(message)
                try:
                    first_result = await first.run()
                except Exception:
                    log.exception("session crashed")
                    first_result = None

                if first_result is None or not first_result.failed:
                    continue

                log.info(
                    "session failed (exit=%s stuck=%s timed_out=%s); spawning one-shot retry",
                    first_result.exit_code, first_result.stuck, first_result.timed_out,
                )
                retry_message = IncomingMessage(
                    channel=message.channel,
                    user=message.user,
                    text=message.text,
                    thread_ts=message.thread_ts,
                    event_ts=message.event_ts,
                    files=message.files,
                    display_name=message.display_name,
                    is_principal_operator=message.is_principal_operator,
                    retry_context={
                        "exit_code": first_result.exit_code,
                        "stuck": first_result.stuck,
                        "timed_out": first_result.timed_out,
                        "stderr_tail": first_result.stderr_tail,
                        "synthetic_errors": first_result.synthetic_errors,
                    },
                    raw_event=message.raw_event,
                )
                retry = SamSession(retry_message)
                try:
                    retry_result = await retry.run()
                except Exception:
                    log.exception("retry session crashed")
                    retry_result = None

                if retry_result is None or retry_result.failed:
                    log.warning(
                        "retry session also failed (exit=%s); posting operator alert",
                        retry_result.exit_code if retry_result else "n/a",
                    )
                    await self._post_operator_alert(message, first_result, retry_result)

    async def _post_operator_alert(
        self,
        original: IncomingMessage,
        first: SessionResult,
        retry: Optional[SessionResult],
    ) -> None:
        """Posted directly by the daemon when both attempts fail.

        No claude subprocess — this has to work even when claude itself is
        the thing that's broken. Keep the text short and concrete.
        """
        mention = f"<@{SAM_OPERATOR_USER_ID}> " if SAM_OPERATOR_USER_ID else ""
        first_status = (
            "stuck" if first.stuck
            else "timed out" if first.timed_out
            else f"exit {first.exit_code}"
        )
        if retry is None:
            retry_status = "crashed before reporting"
        elif retry.stuck:
            retry_status = "stuck"
        elif retry.timed_out:
            retry_status = "timed out"
        else:
            retry_status = f"exit {retry.exit_code}"

        text = (
            f"{mention}something's wrong with me — i tried to respond and {first_status}, "
            f"then tried to explain what failed and that also {retry_status}. "
            f"need your eyes."
        )
        try:
            await self.app.client.chat_postMessage(
                channel=original.channel,
                thread_ts=original.thread_ts or original.event_ts,
                text=redact_secrets(text),
            )
        except Exception:
            log.exception("operator alert post failed")

    def _discover_cron_skills(self) -> list[tuple[str, str]]:
        """Scan src/skills/*.md for skills with a `cron:` frontmatter field.

        Returns a list of (skill_name, cron_expression) tuples. The skill
        body lives at `src/skills/<name>.md`; the daemon doesn't read it —
        sam reads it when the scheduled message fires.
        """
        skills_dir = SAM_SRC / "skills"
        if not skills_dir.exists():
            return []
        found: list[tuple[str, str]] = []
        for skill in sorted(skills_dir.glob("*.md")):
            try:
                meta, _ = _parse_frontmatter(skill.read_text())
            except OSError:
                log.exception("could not read skill %s", skill)
                continue
            cron_expr = meta.get("cron")
            if not cron_expr:
                continue
            name = meta.get("name") or skill.stem
            found.append((name, cron_expr))
        return found

    async def _run_cron_skill(self, skill_name: str, cron_expr: str) -> None:
        """One async task per scheduled skill. Sleeps until each next fire."""
        try:
            from croniter import croniter
        except ImportError:
            log.error(
                "croniter not installed; cannot schedule skill %s. "
                "Add `croniter` to src/runtime/requirements.txt and rebuild.",
                skill_name,
            )
            return
        if not SAM_CHANNEL:
            log.info("scheduled skill %s: disabled (SAM_CHANNEL unset)", skill_name)
            return
        try:
            iterator = croniter(cron_expr, datetime.now().astimezone())
        except (ValueError, KeyError):
            log.exception("invalid cron expression %r for skill %s", cron_expr, skill_name)
            return

        while not self.shutdown_event.is_set():
            next_fire: datetime = iterator.get_next(datetime)
            now = datetime.now().astimezone()
            sleep_seconds = max(0.0, (next_fire - now).total_seconds())
            log.info(
                "scheduled skill %s: next fire at %s (in %ds)",
                skill_name, next_fire.isoformat(), int(sleep_seconds),
            )
            try:
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=sleep_seconds)
                return  # shutdown signalled
            except asyncio.TimeoutError:
                pass  # woke up
            await self._enqueue_scheduled_skill(skill_name)

    async def _enqueue_scheduled_skill(self, skill_name: str) -> None:
        ts = f"{time.time():.6f}"
        today_journal = f"/data/journal/{datetime.now().date().isoformat()}.md"
        text = (
            f"This is a SCHEDULED SKILL invocation — not a Slack message. "
            f"The daemon's scheduler triggered `{skill_name}`. "
            f"Read `src/skills/{skill_name}.md` for the full directive, then follow it.\n\n"
            f"BEFORE running the full directive: grep today's journal file "
            f"({today_journal}) for past fires of `{skill_name}`. If past-you "
            f"already ran it today, your job here is a delta check — what changed "
            f"since the previous fire? — not a full re-run. If past-you only got "
            f"partway, pick up where it stopped. If today's file has no prior fire, "
            f"this is the first run and you do the whole directive.\n\n"
            f"Target channel for any Slack post: {SAM_CHANNEL}. "
            f"Silence is acceptable — only post when there's substance."
        )
        message = IncomingMessage(
            channel=SAM_CHANNEL,
            user=self.bot_user_id or "scheduler",
            text=text,
            thread_ts=None,
            event_ts=ts,
            scheduled=True,
            raw_event={},
        )
        log.info("scheduled skill %s: queuing (event_ts=%s)", skill_name, ts)
        await self.queue.put(message)

    async def run(self) -> None:
        # Identify ourselves so we can recognize and skip our own messages.
        try:
            auth = await self.app.client.auth_test()
            self.bot_user_id = auth.get("user_id")
            log.info("authenticated as bot user %s", self.bot_user_id)
        except Exception:
            log.exception("auth.test failed; will not recognize own messages reliably")

        # Resolve the SAM_CHANNEL id to a name so we can refer to it by #name
        # in the side-pane redirect copy.
        if SAM_CHANNEL:
            try:
                info = await self.app.client.conversations_info(channel=SAM_CHANNEL)
                self.sam_channel_name = (info.get("channel") or {}).get("name")
                log.info("Sam channel resolved: #%s (%s)", self.sam_channel_name, SAM_CHANNEL)
            except Exception:
                log.exception("conversations.info failed; side-pane redirect will use a generic phrase")

        worker_task = asyncio.create_task(self._worker())
        socket_task = asyncio.create_task(self.handler.start_async())
        # Run catch-up after socket starts so live events have a path in,
        # but don't block startup on it — it can be slow if many threads.
        catchup_task = asyncio.create_task(self._catch_up())

        # One async task per scheduled skill (skills with a `cron:` frontmatter
        # field). Discovered at startup; changes require a daemon restart.
        cron_tasks: list[asyncio.Task] = []
        for skill_name, cron_expr in self._discover_cron_skills():
            log.info("registering scheduled skill: %s (cron=%s)", skill_name, cron_expr)
            cron_tasks.append(asyncio.create_task(self._run_cron_skill(skill_name, cron_expr)))
        if not cron_tasks:
            log.info("no skills with `cron:` frontmatter; no scheduled tasks running")

        log.info(
            "Sam daemon ready (channel=%s, commit=%s)",
            SAM_CHANNEL or "all", COMMIT_SHA or "unknown",
        )

        await self.shutdown_event.wait()

        log.info("shutting down")
        for t in cron_tasks:
            t.cancel()
        catchup_task.cancel()
        socket_task.cancel()
        worker_task.cancel()
        for t in (*cron_tasks, catchup_task, socket_task, worker_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

async def amain() -> int:
    try:
        acquire_lock()
    except LockError as e:
        log.error("%s", e)
        return 1

    daemon = Daemon()

    def _on_signal() -> None:
        log.info("signal received, initiating shutdown")
        daemon.shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    try:
        await daemon.run()
    finally:
        release_lock()

    return 0

def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(130)

if __name__ == "__main__":
    main()