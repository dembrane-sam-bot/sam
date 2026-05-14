"""Runtime configuration, paths, constants, and shared helpers.

This module is the single source of truth for environment-derived values
(paths, tokens, timing knobs) and the cross-cutting helpers everything else
depends on (logging setup, redaction, lock, cursor, journal path,
subagent provisioning).

Imported by every other runtime module. Imports nothing from sibling
runtime modules — those depend on this, not the other way around.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Env vars and paths
# -----------------------------------------------------------------------------

load_dotenv()

SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SAM_CHANNEL = os.environ.get("SAM_CHANNEL")  # optional: restrict to one channel for v0
SAM_OPERATOR_USER_ID = os.environ.get("SAM_OPERATOR_USER_ID")  # @-mentioned when both attempts fail
SAM_HOME = Path(os.environ.get("SAM_HOME", "/data"))
SAM_REPO = Path("/home/sam")          # where Sam's checkout lives in the container
SAM_SRC = SAM_REPO / "src"            # identity, scope, capabilities, skills, runtime
SAM_CLAUDE_DIR = SAM_REPO / ".claude" # where Claude Code looks for project-level config

# Default model for Sam's main session. Sam dispatches to the opus subagent
# (see src/runtime/agents/opus.md) when it wants deeper reasoning.
SAM_MODEL = "sonnet"

JOURNAL_DIR = SAM_HOME / "journal"
# Pre-directory combined journal lives alongside the new directory and stays
# greppable. Sam reads both `data/journal.md` and `data/journal/*.md`.
LEGACY_JOURNAL_PATH = SAM_HOME / "journal.md"
LOCK_PATH = SAM_HOME / "sam.lock"
REPOS_DIR = SAM_HOME / "repos"
CURSOR_PATH = SAM_HOME / "cursor.json"

# -----------------------------------------------------------------------------
# Timing and buffer knobs
# -----------------------------------------------------------------------------

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
# Logging — configured once for the whole runtime package
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sam.runtime")

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
# Single-instance lock
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
    import json
    if not CURSOR_PATH.exists():
        return None
    try:
        return json.loads(CURSOR_PATH.read_text()).get("last_seen_ts")
    except (json.JSONDecodeError, OSError):
        log.warning("cursor.json unreadable, treating as first run")
        return None


def save_cursor(ts: str) -> None:
    import json
    SAM_HOME.mkdir(parents=True, exist_ok=True)
    current = load_cursor()
    # Only advance the cursor — never rewind it.
    if current and float(ts) <= float(current):
        return
    CURSOR_PATH.write_text(json.dumps({"last_seen_ts": ts}))

# -----------------------------------------------------------------------------
# Journal + commit SHA
# -----------------------------------------------------------------------------

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

# -----------------------------------------------------------------------------
# Subagent provisioning — mirror Tier 3 agent defs into the path Claude Code reads
# -----------------------------------------------------------------------------

def provision_subagents() -> None:
    """Copy Tier 3 subagent definitions into the place Claude Code looks for them.

    Subagent definitions live under `src/runtime/agents/` (Tier 3 — substrate,
    not Sam-editable). Claude Code only auto-discovers agents under
    `.claude/agents/`, so on each daemon start we mirror the substrate copies
    into that runtime path. If a teammate (or Sam itself, accidentally)
    overwrote one of those files between restarts, this restores it.
    """
    source_dir = SAM_SRC / "runtime" / "agents"
    if not source_dir.exists():
        return
    target_dir = SAM_CLAUDE_DIR / "agents"
    target_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(source_dir.glob("*.md")):
        dst = target_dir / src.name
        try:
            dst.write_text(src.read_text())
            log.info("provisioned subagent: %s", dst)
        except OSError:
            log.exception("could not provision subagent %s", src)
