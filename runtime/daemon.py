"""
Sam's runtime daemon.

Responsibilities:
- Listen for Slack events (Socket Mode, Agents & AI Apps surface)
- For each incoming message that wakes Sam up, launch a Claude Code session
- Stream Sam's output back to Slack as it arrives
- Track threads Sam has participated in, so in-thread replies route back even
  without an @-mention
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
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
SAM_HOME = Path(os.environ.get("SAM_HOME", "/data"))
SAM_REPO = Path("/home/sam")  # where Sam's source lives inside the container

JOURNAL_PATH = SAM_HOME / "journal.md"
LOCK_PATH = SAM_HOME / "sam.lock"
REPOS_DIR = SAM_HOME / "repos"
CURSOR_PATH = SAM_HOME / "cursor.json"
THREADS_PATH = SAM_HOME / "threads.json"

# How long without any stdout/stderr output before we consider Sam stuck
STUCK_TIMEOUT_SECONDS = 10 * 60  # 10 minutes
# Hard cap on a single session's wall clock
MAX_SESSION_SECONDS = 60 * 60     # 1 hour
# How long we remember a thread for in-thread reply routing
THREAD_TRACKING_DAYS = 30
# Subtypes accepted as real user messages in a tracked thread
ALLOWED_MESSAGE_SUBTYPES = {None, "thread_broadcast", "file_share"}
# Subtypes we explicitly log-and-drop so they're visible in debugging
NOISY_MESSAGE_SUBTYPES = {"message_changed", "message_deleted", "bot_message"}

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
# Persisted cursor + thread tracking
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

def load_tracked_threads() -> dict[str, str]:
    """Map of 'channel:thread_ts' -> iso timestamp first tracked."""
    if not THREADS_PATH.exists():
        return {}
    try:
        return json.loads(THREADS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("threads.json unreadable, treating as empty")
        return {}

def save_tracked_threads(threads: dict[str, str]) -> None:
    SAM_HOME.mkdir(parents=True, exist_ok=True)
    THREADS_PATH.write_text(json.dumps(threads, indent=2, sort_keys=True))

def track_thread(channel: str, thread_ts: str) -> None:
    """Remember (channel, thread_ts) so we route future replies in this thread."""
    threads = load_tracked_threads()
    key = f"{channel}:{thread_ts}"
    if key in threads:
        return
    now = datetime.now(tz=timezone.utc)
    threads[key] = now.isoformat()
    cutoff = (now - timedelta(days=THREAD_TRACKING_DAYS)).isoformat()
    threads = {k: v for k, v in threads.items() if v >= cutoff}
    save_tracked_threads(threads)

def is_tracked_thread(channel: str, thread_ts: str) -> bool:
    return f"{channel}:{thread_ts}" in load_tracked_threads()

# -----------------------------------------------------------------------------
# System prompt assembly
# -----------------------------------------------------------------------------

def assemble_system_prompt() -> str:
    """Read identity, scope, all capabilities, and all skills.

    Re-read every session, so a `git pull` followed by next message picks up
    the new version of Sam.
    """
    sections: list[str] = []

    def _add(path: Path, header: str) -> None:
        if path.exists():
            sections.append(f"# {header}\n\n{path.read_text()}")

    _add(SAM_REPO / "identity.md", "IDENTITY")
    _add(SAM_REPO / "scope.md", "SCOPE")

    for cap in sorted((SAM_REPO / "capabilities").glob("*.md")):
        sections.append(f"# CAPABILITY: {cap.stem}\n\n{cap.read_text()}")

    skills_dir = SAM_REPO / "skills"
    if skills_dir.exists():
        for skill in sorted(skills_dir.glob("*.md")):
            sections.append(f"# SKILL: {skill.stem}\n\n{skill.read_text()}")

    orchestration = """
# ORCHESTRATION

You are Sam, running as a Claude Code session. Each time you wake up, you
are responding to a Slack message that came in via the daemon.

Before responding, decide what context you need and go get it. Use:
- The journal at `/data/journal.md` (grep, tail, head — your call)
- The Linear API for issues and comments (`LINEAR_API_KEY` is in env)
- The GitHub API and `gh` CLI for PRs, issues, code (`GITHUB_TOKEN` is in env)
- The Slack Web API for posting back (`SLACK_BOT_TOKEN` is in env)
- Repos cloned under `/data/repos/` (clone what you need, fetch what's stale)

The Slack message that triggered this session is at the start of your
conversation. The channel ID and thread timestamp are included so you can
post back in the right place.

Before you finish, append a journal entry describing what you did. Follow
the format in `capabilities/journal.md`. This is how future-you remembers.

Be honest, terse, and useful. Don't perform engagement. Don't explain what
you're about to do unless someone is going to be watching the status
indicator. Just do it, then say the result.
"""
    sections.append(orchestration)

    return "\n\n---\n\n".join(sections)

# -----------------------------------------------------------------------------
# Slack helpers
# -----------------------------------------------------------------------------

@dataclass
class IncomingMessage:
    """A Slack message that woke Sam up."""
    channel: str
    user: str
    text: str
    thread_ts: Optional[str]  # If part of a thread; else None
    event_ts: str
    raw_event: dict = field(repr=False)

    def to_initial_user_message(self) -> str:
        """Format as the first user message into Sam's session."""
        thread_part = f"thread_ts={self.thread_ts}" if self.thread_ts else "no thread"
        return (
            f"Slack message in channel {self.channel} from <@{self.user}> ({thread_part}):\n\n"
            f"{self.text}\n\n"
            f"Reply in Slack via the Web API. If this is in a thread, reply in-thread "
            f"(use thread_ts={self.thread_ts or self.event_ts})."
        )

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
                # Sam handles posting to Slack itself via its tools.
                # We log here for debugging — could later parse for plan/status events.
                log.debug("stdout: %s", line.decode().strip()[:200])

        async def read_stderr() -> None:
            assert self.proc and self.proc.stderr
            async for line in self.proc.stderr:
                self.last_output_at = time.monotonic()
                log.warning("stderr: %s", line.decode().strip()[:200])

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
        with JOURNAL_PATH.open("a") as f:
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
        # Recent message ts values we've already queued, to dedupe app_mention
        # and message events that fire for the same underlying Slack message.
        self._seen_ts: set[str] = set()
        self._seen_ts_order: list[str] = []

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
            # Skip messages from the bot itself (defense in depth — usually
            # bot_message subtype catches this, but not always).
            if event.get("bot_id") or event.get("user") == self.bot_user_id:
                return
            # Channel whitelist applies to all non-mention messages too.
            channel = event.get("channel")
            if not self._channel_allowed(channel):
                return
            # Only handle thread replies in threads we've previously been in.
            # Top-level non-mention channel chatter is ignored on purpose.
            thread_ts = event.get("thread_ts")
            if not thread_ts:
                return
            if not is_tracked_thread(channel, thread_ts):
                return
            await self._handle_event(event)

        @self.app.event("assistant_thread_started")
        async def on_assistant_thread_started(event, client):
            log.info("assistant_thread_started: %s", event)

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

        message = IncomingMessage(
            channel=channel,
            user=user,
            text=event.get("text", ""),
            thread_ts=event.get("thread_ts"),
            event_ts=ts,
            raw_event=event,
        )
        # Sam replies in-thread; remember the thread so non-mention replies
        # in it get routed back to Sam later.
        track_thread(message.channel, message.thread_ts or message.event_ts)
        save_cursor(ts)
        await self.queue.put(message)
        log.info("queued message from <@%s> in %s (ts=%s)",
                 message.user, message.channel, message.event_ts)

    async def _catch_up(self) -> None:
        """Replay messages we missed while the daemon was offline.

        Looks at the configured channel's recent history (for new @mentions)
        and at every tracked thread (for non-mention replies). Bounded by the
        last-seen cursor; on first run we skip backfill entirely.
        """
        last_seen = load_cursor()
        if last_seen is None:
            log.info("no cursor on disk; skipping catch-up (first run)")
            save_cursor(f"{time.time():.6f}")
            return

        log.info("catch-up: scanning since ts=%s", last_seen)
        client = self.app.client
        candidates: list[dict] = []

        if SAM_CHANNEL:
            try:
                resp = await client.conversations_history(
                    channel=SAM_CHANNEL,
                    oldest=last_seen,
                    inclusive=False,
                    limit=200,
                )
                for msg in resp.get("messages", []):
                    if msg.get("subtype") not in ALLOWED_MESSAGE_SUBTYPES:
                        continue
                    if msg.get("bot_id") or msg.get("user") == self.bot_user_id:
                        continue
                    text = msg.get("text", "")
                    is_mention = (
                        self.bot_user_id
                        and f"<@{self.bot_user_id}>" in text
                    )
                    if not is_mention:
                        continue
                    msg["channel"] = SAM_CHANNEL
                    candidates.append(msg)
            except Exception:
                log.exception("catch-up: history fetch failed")

        for key in list(load_tracked_threads().keys()):
            channel, _, thread_ts = key.partition(":")
            if not channel or not thread_ts:
                continue
            if not self._channel_allowed(channel):
                continue
            try:
                resp = await client.conversations_replies(
                    channel=channel,
                    ts=thread_ts,
                    oldest=last_seen,
                    inclusive=False,
                    limit=200,
                )
                for msg in resp.get("messages", []):
                    if msg.get("subtype") not in ALLOWED_MESSAGE_SUBTYPES:
                        continue
                    if msg.get("bot_id") or msg.get("user") == self.bot_user_id:
                        continue
                    # The replies endpoint always returns the thread parent;
                    # skip it (we've already handled it on its original ts).
                    if msg.get("ts") == thread_ts:
                        continue
                    msg["channel"] = channel
                    candidates.append(msg)
            except Exception:
                log.exception("catch-up: replies fetch failed for %s", key)

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
        """Single worker that drains the queue, one session at a time."""
        while not self.shutdown_event.is_set():
            try:
                message = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            async with self.session_lock:
                session = SamSession(message)
                try:
                    await session.run()
                except Exception:
                    log.exception("session crashed")

    async def run(self) -> None:
        # Identify ourselves so we can recognize and skip our own messages.
        try:
            auth = await self.app.client.auth_test()
            self.bot_user_id = auth.get("user_id")
            log.info("authenticated as bot user %s", self.bot_user_id)
        except Exception:
            log.exception("auth.test failed; will not recognize own messages reliably")

        worker_task = asyncio.create_task(self._worker())
        socket_task = asyncio.create_task(self.handler.start_async())
        # Run catch-up after socket starts so live events have a path in,
        # but don't block startup on it — it can be slow if many threads.
        catchup_task = asyncio.create_task(self._catch_up())

        log.info("Sam daemon ready (channel=%s)", SAM_CHANNEL or "all")

        await self.shutdown_event.wait()

        log.info("shutting down")
        catchup_task.cancel()
        socket_task.cancel()
        worker_task.cancel()
        for t in (catchup_task, socket_task, worker_task):
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