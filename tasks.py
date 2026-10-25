"""Per-user task management and the playlist workflow (no Telegram imports).

Keeping this free of Telegram code means cancellation, ordering and progress
logic can be unit-tested with a fake ``send`` function.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from playlist import Playlist, PlaylistError, VideoEntry

logger = logging.getLogger(__name__)

SendFn = Callable[[str], Awaitable[None]]
FetchFn = Callable[[str], Awaitable[Playlist]]


class Cancelled(Exception):
    """The user cancelled (or the bot is shutting down)."""


class SendError(Exception):
    """One message could not be delivered; later messages may still work."""


class FatalSendError(Exception):
    """The chat is unreachable (e.g. the user blocked the bot); stop trying."""


# --------------------------------------------------------------------------- cancellation helpers
async def sleep_or_cancel(delay: float, cancel_event: asyncio.Event) -> None:
    """Sleep for ``delay`` seconds but wake up immediately if cancelled."""
    if cancel_event.is_set():
        raise Cancelled
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return
    raise Cancelled


async def run_cancellable(awaitable: Awaitable, cancel_event: asyncio.Event):
    """Await ``awaitable`` but stop it and raise Cancelled if the event is set first."""
    work = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({work, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        raise Cancelled
    finally:
        for future in (work, waiter):
            if not future.done():
                future.cancel()
        await asyncio.gather(work, waiter, return_exceptions=True)


# --------------------------------------------------------------------------- message formatting
def format_entry_message(entry: VideoEntry, total: int) -> str:
    header = f"[{entry.position}/{total}]"
    if entry.title:
        header += f" {entry.title}"
    return f"{header}\n{entry.url}"


def _limited(items: list[str], limit: int) -> str:
    shown = ", ".join(items[:limit])
    if len(items) > limit:
        shown += f", … and {len(items) - limit} more"
    return shown


# --------------------------------------------------------------------------- delivery
@dataclass
class DeliveryResult:
    sent: int = 0
    failed: list[int] = field(default_factory=list)  # playlist positions
    cancelled: bool = False
    aborted: bool = False  # chat became unreachable


async def _attempt(send: SendFn, text: str) -> str:
    try:
        await send(text)
        return "ok"
    except Cancelled:
        return "cancelled"
    except FatalSendError:
        return "fatal"
    except SendError:
        return "failed"


async def deliver_entries(
    entries: list[VideoEntry],
    total: int,
    send: SendFn,
    *,
    delay: float,
    progress_every: int,
    cancel_event: asyncio.Event,
) -> DeliveryResult:
    """Send one message per entry, in the given order, stopping cleanly on cancel."""
    result = DeliveryResult()
    count = len(entries)
    for index, entry in enumerate(entries, start=1):
        if cancel_event.is_set():
            result.cancelled = True
            return result

        outcome = await _attempt(send, format_entry_message(entry, total))
        if outcome == "ok":
            result.sent += 1
        elif outcome == "failed":
            result.failed.append(entry.position)
            logger.warning("Could not deliver playlist position %d", entry.position)
        elif outcome == "cancelled":
            result.cancelled = True
            return result
        else:
            result.aborted = True
            return result

        if index == count:
            break

        if progress_every and index % progress_every == 0:
            outcome = await _attempt(send, f"⏳ Progress: {index}/{count} links processed.")
            if outcome == "cancelled":
                result.cancelled = True
                return result
            if outcome == "fatal":
                result.aborted = True
                return result

        try:
            await sleep_or_cancel(delay, cancel_event)
        except Cancelled:
            result.cancelled = True
            return result
    return result


def _final_summary(playlist: Playlist, result: DeliveryResult) -> str:
    playable = len(playlist.entries)
    problems = bool(result.failed or playlist.skipped or playlist.truncated or playlist.count_mismatch)
    if result.cancelled:
        head = f"🛑 Cancelled. Sent {result.sent} of {playable} links."
    elif result.aborted:
        head = f"⚠️ Stopped: I can't deliver messages to this chat. Sent {result.sent} of {playable} links."
    elif problems:
        head = f"⚠️ Finished with issues. Sent {result.sent} of {playable} links."
    else:
        head = f"✅ Done. Sent all {result.sent} links."

    lines = [head]
    if result.failed:
        lines.append(f"Failed to send (positions): {_limited([str(p) for p in result.failed], 30)}")
    if playlist.skipped:
        lines.append(f"Skipped {len(playlist.skipped)} unavailable entries:")
        lines.extend(f"  #{s.position}: {s.reason}" for s in playlist.skipped[:20])
        if len(playlist.skipped) > 20:
            lines.append(f"  … and {len(playlist.skipped) - 20} more")
    if playlist.truncated:
        lines.append(f"The playlist is longer than the limit; only the first {playlist.total} entries were processed.")
    if playlist.count_mismatch:
        lines.append(
            f"YouTube reports {playlist.reported_count} videos but only {playlist.total} entries "
            "were listed; some may be hidden from this bot."
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- the whole job
async def _notify(send: SendFn, text: str) -> None:
    """Best-effort status message: failures are logged, never raised."""
    try:
        await send(text)
    except (SendError, FatalSendError, Cancelled) as exc:
        logger.debug("Status message not delivered: %s", type(exc).__name__)


async def run_playlist_job(
    *,
    playlist_id: str,
    send: SendFn,
    fetch: FetchFn,
    cancel_event: asyncio.Event,
    delay: float,
    progress_every: int,
) -> DeliveryResult | None:
    """Fetch the playlist and send every link. Returns None if nothing was delivered."""
    try:
        await _notify(send, "🔎 Reading the playlist… this can take a few seconds.")
        try:
            playlist = await run_cancellable(fetch(playlist_id), cancel_event)
        except Cancelled:
            await _notify(send, "🛑 Cancelled.")
            return None
        except PlaylistError as exc:
            await _notify(send, f"❌ {exc.user_message}")
            return None

        found = f"📋 “{playlist.title}”\nFound {playlist.total} entries"
        if playlist.skipped:
            found += f" ({len(playlist.entries)} playable, {len(playlist.skipped)} unavailable)"
        found += ".\nSending links in playlist order, starting with the first entry…"
        await _notify(send, found)

        result = await deliver_entries(
            playlist.entries,
            playlist.total,
            send,
            delay=delay,
            progress_every=progress_every,
            cancel_event=cancel_event,
        )
        await _notify(send, _final_summary(playlist, result))
        return result
    except asyncio.CancelledError:
        raise  # hard cancel from shutdown(); let it propagate
    except Exception:  # noqa: BLE001
        logger.exception("Playlist job crashed")
        await _notify(send, "❌ Something went wrong while processing the playlist. Please try again.")
        return None


# --------------------------------------------------------------------------- task manager
class StartResult(enum.Enum):
    STARTED = "started"
    DUPLICATE = "duplicate"  # same user, same playlist already running
    BUSY = "busy"  # same user, different playlist already running


@dataclass
class ActiveJob:
    playlist_id: str
    cancel_event: asyncio.Event
    task: asyncio.Task


class TaskManager:
    """In-memory, one active job per Telegram user ID."""

    def __init__(self) -> None:
        self._jobs: dict[int, ActiveJob] = {}

    @property
    def active_count(self) -> int:
        return len(self._jobs)

    def is_active(self, user_id: int) -> bool:
        job = self._jobs.get(user_id)
        return job is not None and not job.task.done()

    def start(
        self,
        user_id: int,
        playlist_id: str,
        job_factory: Callable[[asyncio.Event], Awaitable[None]],
    ) -> StartResult:
        existing = self._jobs.get(user_id)
        if existing is not None and not existing.task.done():
            return StartResult.DUPLICATE if existing.playlist_id == playlist_id else StartResult.BUSY

        cancel_event = asyncio.Event()
        task = asyncio.create_task(self._guard(user_id, job_factory(cancel_event)))
        self._jobs[user_id] = ActiveJob(playlist_id, cancel_event, task)
        task.add_done_callback(lambda finished: self._release(user_id, finished))
        return StartResult.STARTED

    def cancel(self, user_id: int) -> bool:
        """Cancel this user's own job. Returns False if they have none."""
        job = self._jobs.get(user_id)
        if job is None or job.task.done():
            return False
        job.cancel_event.set()
        return True

    async def shutdown(self, grace: float = 5.0) -> None:
        """Ask every job to stop, wait briefly, then hard-cancel stragglers."""
        jobs = list(self._jobs.values())
        if not jobs:
            return
        for job in jobs:
            job.cancel_event.set()
        _, pending = await asyncio.wait([job.task for job in jobs], timeout=grace)
        for task in pending:
            task.cancel()
        await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)

    async def _guard(self, user_id: int, awaitable: Awaitable[None]) -> None:
        try:
            await awaitable
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Unhandled error in job for user %s", user_id)

    def _release(self, user_id: int, finished: asyncio.Task) -> None:
        job = self._jobs.get(user_id)
        if job is not None and job.task is finished:
            del self._jobs[user_id]
