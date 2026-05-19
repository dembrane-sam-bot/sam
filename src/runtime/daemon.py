"""Sam's Slack-event daemon.

Responsibilities:
- Listen for Slack events (Socket Mode, Agents & AI Apps surface)
- For each incoming message that wakes Sam up, run a SamSession
- Stream Sam's output back to Slack as it arrives (delegated to SamSession's
  AgentRunner — the daemon doesn't read stream-json itself)
- Route in-thread replies by asking Slack whether Sam has posted in the thread
  (no local thread bookkeeping; cached in-memory only)
- Redirect messages received in the Agents & AI Apps side-pane back to the
  whitelisted channel
- On startup, replay messages missed while the daemon was offline
- Maintain a single-instance lock and a journal safety net
- Add lifecycle reactions (:eyes: ack, :brain:/:gear: post-session) to Slack messages

What this daemon does NOT do:
- Reason about anything. All reasoning happens inside Sam (the agent session).
- Talk to GitHub, Linear, or any external API except Slack. Sam does that itself.
- Modify Sam's source. The daemon is Tier 3 substrate; Sam doesn't touch it either.

Code layout:
- src/runtime/config.py — env vars, paths, constants, redaction, lock, cursor, journal
- src/runtime/prompts.py — system-prompt assembly and named situational templates
- src/runtime/session.py — IncomingMessage, AgentRunner abstraction, SamSession
- src/runtime/daemon.py — this file: Daemon class + entrypoint
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from datetime import datetime
from typing import Optional

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import (
    ALLOWED_MESSAGE_SUBTYPES,
    COMMIT_SHA,
    LockError,
    NOISY_MESSAGE_SUBTYPES,
    SAM_CHANNEL,
    SAM_MODEL,
    SAM_OPERATOR_USER_ID,
    SLACK_APP_TOKEN,
    SLACK_BOT_TOKEN,
    THREAD_CACHE_TTL_SECONDS,
    acquire_lock,
    load_cursor,
    log,
    provision_subagents,
    redact_secrets,
    release_lock,
    save_cursor,
)
from .prompts import OPERATOR_ALERT_TEMPLATE, SCHEDULED_SKILL_TEMPLATE
from .session import IncomingMessage, SamSession, SessionResult


# -----------------------------------------------------------------------------
# Session badges — emoji footer appended to Sam's response
# -----------------------------------------------------------------------------

def _format_session_badges(result: SessionResult) -> str:
    """Compose the one-line emoji footer from a SessionResult's flags.

    Returns an empty string when no flag is set, so callers can short-circuit
    without posting anything. Order is fixed (brain · globe · computer · gear)
    so the footer reads consistently regardless of which combination fires.
    """
    parts: list[str] = []
    if result.opus_used:
        parts.append(":brain:")
    if result.web_used:
        parts.append(":globe_with_meridians:")
    if result.bash_used:
        parts.append(":computer:")
    if result.edited_files:
        parts.append(":gear:")
    return " ".join(parts)


# -----------------------------------------------------------------------------
# Daemon — Slack listener + session queue
# -----------------------------------------------------------------------------

class Daemon:
    def __init__(self) -> None:
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
            thread_history=first.thread_history,
        )

    async def _fetch_thread_history(
        self, channel: str, thread_ts: str, exclude_ts: str,
    ) -> list[dict]:
        """Return prior messages in a thread, oldest-first, excluding the trigger.

        Used to pre-load context so Sam sees the full thread without having to
        call conversations_replies manually in every session.

        Fetches up to 100 messages. On any API failure returns [] — thread
        context is best-effort; it should never block queuing.
        """
        try:
            resp = await self.app.client.conversations_replies(
                channel=channel, ts=thread_ts, limit=100,
            )
        except Exception:
            log.exception("thread history fetch failed for %s/%s", channel, thread_ts)
            return []
        messages = resp.get("messages") or []
        return [m for m in messages if m.get("ts") != exclude_ts]

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

    async def _append_session_badges(
        self, message: IncomingMessage, result: SessionResult,
    ) -> None:
        """Append a one-line emoji badges footer to Sam's most recent post.

        Complement to :eyes: (added as a reaction on inbound messages at
        queue time). Four independent signals — any combination can fire:
        - :brain:                 opus subagent was dispatched
        - :globe_with_meridians:  WebFetch or WebSearch was used
        - :computer:              Bash was used for non-Slack-housekeeping work
        - :gear:                  Edit or Write touched a non-journal path

        Implementation: find Sam's most recent post in the relevant thread (or
        channel for scheduled wake-ups), fetch its current text, and call
        chat.update with the badges appended on a new line. The bot retains
        edit rights on its own messages, so this works without extra scope.
        Slack will show a small "(edited)" indicator next to the message —
        an accepted cost of the in-message-text approach.

        Skip on failed sessions — the post (if any) might be partial garbage.
        Skip if no flag is set — nothing to mark.
        """
        if result.failed:
            return
        badges = _format_session_badges(result)
        if not badges:
            return
        if not self.bot_user_id:
            return
        oldest = f"{result.started_at_wall:.6f}"
        try:
            if message.scheduled or not (message.thread_ts or message.event_ts):
                resp = await self.app.client.conversations_history(
                    channel=message.channel, oldest=oldest, inclusive=False, limit=20,
                )
            else:
                resp = await self.app.client.conversations_replies(
                    channel=message.channel,
                    ts=message.thread_ts or message.event_ts,
                    oldest=oldest, inclusive=False, limit=100,
                )
        except Exception:
            log.exception("could not fetch messages for badge appending")
            return
        # Walk newest-first to find Sam's latest post. conversations_history
        # returns newest-first; conversations_replies returns oldest-first.
        # Try both directions to be safe.
        msgs = resp.get("messages", []) or []
        target = None
        for m in reversed(msgs):
            if m.get("user") == self.bot_user_id or m.get("bot_id"):
                target = m
                break
        if target is None:
            for m in msgs:
                if m.get("user") == self.bot_user_id or m.get("bot_id"):
                    target = m
                    break
        if target is None or not target.get("ts"):
            return
        original_text = target.get("text") or ""
        # Avoid double-appending if a previous run already added a badge line.
        if "\n\n" + badges in original_text or original_text.endswith(badges):
            return
        new_text = f"{original_text}\n\n{badges}" if original_text else badges
        try:
            await self.app.client.chat_update(
                channel=message.channel,
                ts=target["ts"],
                text=new_text,
            )
        except Exception as e:
            log.debug("chat.update for badges failed on %s: %s", target.get("ts"), e)

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

        # Fetch prior thread messages so Sam has full context without an extra
        # API call inside the session. Only fires for thread replies (when
        # thread_ts is set and differs from the triggering event ts).
        thread_ts = event.get("thread_ts")
        thread_history: list[dict] = []
        if thread_ts:
            thread_history = await self._fetch_thread_history(
                channel, thread_ts, exclude_ts=ts,
            )

        files = event.get("files") or []
        message = IncomingMessage(
            channel=channel,
            user=user,
            text=event.get("text", ""),
            thread_ts=thread_ts,
            event_ts=ts,
            files=files,
            display_name=display_name,
            is_principal_operator=is_principal,
            raw_event=event,
            thread_history=thread_history,
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
                    if first_result is not None:
                        await self._append_session_badges(message, first_result)
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
                else:
                    await self._append_session_badges(retry_message, retry_result)

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

        text = OPERATOR_ALERT_TEMPLATE.format(
            mention=mention, first_status=first_status, retry_status=retry_status,
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
        from .config import SAM_SRC
        from .prompts import _parse_frontmatter
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
        text = SCHEDULED_SKILL_TEMPLATE.format(
            skill_name=skill_name,
            today_journal=today_journal,
            channel=SAM_CHANNEL,
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
        # Provision Tier 3 subagent definitions into the path Claude Code reads.
        # Done before any session can run so the very first wake-up sees them.
        provision_subagents()

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
            "Sam daemon ready (channel=%s, commit=%s, model=%s)",
            SAM_CHANNEL or "all", COMMIT_SHA or "unknown", SAM_MODEL,
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
