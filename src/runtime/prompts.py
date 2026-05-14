"""Prompt content the daemon synthesises and hands to Sam sessions.

Two kinds of content live here:
- The system-prompt assembler that hot-loads identity / scope / capabilities
  + a catalog of skills + an orchestration block sourced from
  `src/runtime/orchestration.md`. Built fresh for every session.
- Named templates for situational user-message content (retry session,
  scheduled-skill wake-up, operator alert). Kept as module constants so
  daemon-authored prompts are visibly grouped and reviewable, separate
  from the control flow that decides when each fires.

Imports from .config only. Imported by .session and .daemon.
"""

from __future__ import annotations

from pathlib import Path

from .config import COMMIT_SHA, SAM_SRC, log

# -----------------------------------------------------------------------------
# Situational prompt templates
# -----------------------------------------------------------------------------

RETRY_SESSION_INTRO = (
    "A previous Sam session attempting to respond to a Slack message FAILED.\n"
    "Do not retry the original task. Your one job in this session is:\n"
    "\n"
    "1. Read the failure context below.\n"
    "2. Post ONE reply in the original Slack thread (channel={channel}, "
    "thread_ts={thread_target}) in your normal Slack voice. Name what failed "
    "in human terms, name the likely cause, and suggest a concrete fix. "
    "Don't dump stderr verbatim — read it, summarise it.\n"
    "3. Then stop. This is a one-shot. The daemon will not retry again."
)

RETRY_SESSION_OUTRO = (
    "Remember: post ONCE, in the original thread, in your Slack voice. "
    "Be honest about the failure. Suggest a fix if you can see one. "
    "Prefer the synthetic errors above over stderr when identifying the cause. "
    "Then exit."
)

SCHEDULED_SKILL_TEMPLATE = (
    "This is a SCHEDULED SKILL invocation — not a Slack message. "
    "The daemon's scheduler triggered `{skill_name}`. "
    "Read `src/skills/{skill_name}.md` for the full directive, then follow it.\n\n"
    "BEFORE running the full directive: grep today's journal file "
    "({today_journal}) for past fires of `{skill_name}`. If past-you "
    "already ran it today, your job here is a delta check — what changed "
    "since the previous fire? — not a full re-run. If past-you only got "
    "partway, pick up where it stopped. If today's file has no prior fire, "
    "this is the first run and you do the whole directive.\n\n"
    "Target channel for any Slack post: {channel}. "
    "Silence is acceptable — only post when there's substance."
)

OPERATOR_ALERT_TEMPLATE = (
    "{mention}something's wrong with me — i tried to respond and {first_status}, "
    "then tried to explain what failed and that also {retry_status}. "
    "need your eyes."
)

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
        rel = skill.relative_to(SAM_SRC.parent)
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

    orchestration_path = SAM_SRC / "runtime" / "orchestration.md"
    if orchestration_path.exists():
        sections.append(
            orchestration_path.read_text().format(commit_sha=COMMIT_SHA or "unknown")
        )

    return "\n\n---\n\n".join(sections)
