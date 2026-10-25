import asyncio
import unittest

from playlist import Playlist, PlaylistUnavailableError, SkippedEntry, VideoEntry
from tasks import (
    Cancelled,
    FatalSendError,
    SendError,
    StartResult,
    TaskManager,
    deliver_entries,
    format_entry_message,
    run_playlist_job,
    sleep_or_cancel,
)


def make_entries(n: int, first_position: int = 1) -> list[VideoEntry]:
    return [
        VideoEntry(p, f"vid{p:08d}", f"Title {p}", f"https://www.youtube.com/watch?v=vid{p:08d}")
        for p in range(first_position, first_position + n)
    ]


def make_playlist(n: int, skipped=()) -> Playlist:
    return Playlist("PLx" * 4, "My List", make_entries(n), list(skipped), total=n + len(skipped))


class Recorder:
    def __init__(self):
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        self.messages.append(text)

    @property
    def links(self):
        return [m for m in self.messages if m.startswith("[")]


class FormatTests(unittest.TestCase):
    def test_format(self):
        entry = make_entries(1, first_position=15)[0]
        self.assertEqual(format_entry_message(entry, 100), "[15/100] Title 15\nhttps://www.youtube.com/watch?v=vid00000015")

    def test_format_without_title(self):
        entry = VideoEntry(1, "abcdefghijk", "", "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(format_entry_message(entry, 3), "[1/3]\nhttps://www.youtube.com/watch?v=abcdefghijk")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_in_order_with_progress(self):
        rec = Recorder()
        result = await deliver_entries(make_entries(5), 5, rec.send, delay=0, progress_every=2, cancel_event=asyncio.Event())
        self.assertEqual(result.sent, 5)
        self.assertEqual([m.split("\n")[0] for m in rec.links], [f"[{i}/5] Title {i}" for i in range(1, 6)])
        progress = [m for m in rec.messages if m.startswith("⏳")]
        self.assertEqual(len(progress), 2)  # after 2 and 4, not after the last

    async def test_progress_disabled(self):
        rec = Recorder()
        await deliver_entries(make_entries(5), 5, rec.send, delay=0, progress_every=0, cancel_event=asyncio.Event())
        self.assertFalse([m for m in rec.messages if m.startswith("⏳")])

    async def test_failed_message_is_recorded_and_delivery_continues(self):
        sent = []

        async def send(text):
            if "[2/3]" in text:
                raise SendError("boom")
            sent.append(text)

        result = await deliver_entries(make_entries(3), 3, send, delay=0, progress_every=0, cancel_event=asyncio.Event())
        self.assertEqual((result.sent, result.failed), (2, [2]))
        self.assertEqual(len(sent), 2)

    async def test_fatal_error_aborts(self):
        async def send(text):
            raise FatalSendError("blocked")

        result = await deliver_entries(make_entries(3), 3, send, delay=0, progress_every=0, cancel_event=asyncio.Event())
        self.assertTrue(result.aborted)
        self.assertEqual(result.sent, 0)

    async def test_cancel_mid_delivery_stops_cleanly(self):
        cancel = asyncio.Event()
        sent = []

        async def send(text):
            sent.append(text)
            if len(sent) == 2:
                cancel.set()

        result = await deliver_entries(make_entries(10), 10, send, delay=0.1, progress_every=0, cancel_event=cancel)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.sent, 2)
        self.assertEqual(len(sent), 2)  # nothing sent after the cancel

    async def test_sleep_or_cancel_wakes_immediately(self):
        cancel = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, cancel.set)
        with self.assertRaises(Cancelled):
            await asyncio.wait_for(sleep_or_cancel(30, cancel), timeout=2)


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_job_starts_at_first_entry_and_summarises(self):
        rec = Recorder()
        playlist = make_playlist(3, skipped=[SkippedEntry(4, "private video")])

        async def fetch(pid):
            return playlist

        result = await run_playlist_job(
            playlist_id="PLx", send=rec.send, fetch=fetch, cancel_event=asyncio.Event(), delay=0, progress_every=0
        )
        self.assertEqual(result.sent, 3)
        self.assertTrue(rec.links[0].startswith("[1/4]"))
        self.assertIn("#4: private video", rec.messages[-1])
        self.assertIn("Finished with issues", rec.messages[-1])  # not claimed as fully successful

    async def test_clean_job_reports_done(self):
        rec = Recorder()

        async def fetch(pid):
            return make_playlist(2)

        await run_playlist_job(playlist_id="PLx", send=rec.send, fetch=fetch, cancel_event=asyncio.Event(), delay=0, progress_every=0)
        self.assertIn("Done. Sent all 2 links", rec.messages[-1])

    async def test_all_entries_unavailable_sends_no_links(self):
        rec = Recorder()

        async def fetch(pid):
            return Playlist("PLx" * 4, "L", [], [SkippedEntry(1, "deleted video")], total=1)

        result = await run_playlist_job(playlist_id="PLx", send=rec.send, fetch=fetch, cancel_event=asyncio.Event(), delay=0, progress_every=0)
        self.assertEqual(result.sent, 0)
        self.assertFalse(rec.links)
        self.assertIn("Finished with issues", rec.messages[-1])

    async def test_fetch_error_is_reported(self):
        rec = Recorder()

        async def fetch(pid):
            raise PlaylistUnavailableError("It is private.")

        result = await run_playlist_job(playlist_id="PLx", send=rec.send, fetch=fetch, cancel_event=asyncio.Event(), delay=0, progress_every=0)
        self.assertIsNone(result)
        self.assertIn("❌ It is private.", rec.messages[-1])

    async def test_unexpected_crash_is_reported_not_raised(self):
        rec = Recorder()

        async def fetch(pid):
            raise RuntimeError("bug")

        with self.assertLogs("tasks", level="ERROR"):
            result = await run_playlist_job(playlist_id="PLx", send=rec.send, fetch=fetch, cancel_event=asyncio.Event(), delay=0, progress_every=0)
        self.assertIsNone(result)
        self.assertIn("Something went wrong", rec.messages[-1])

    async def test_cancel_during_extraction(self):
        rec = Recorder()
        cancel = asyncio.Event()
        fetch_cancelled = asyncio.Event()

        async def fetch(pid):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                fetch_cancelled.set()
                raise

        job = asyncio.create_task(
            run_playlist_job(playlist_id="PLx", send=rec.send, fetch=fetch, cancel_event=cancel, delay=0, progress_every=0)
        )
        await asyncio.sleep(0.05)
        cancel.set()
        await asyncio.wait_for(job, timeout=2)
        self.assertTrue(fetch_cancelled.is_set())  # the extraction itself was stopped
        self.assertEqual(rec.messages[-1], "🛑 Cancelled.")

    async def test_cancel_during_delivery_reports_partial(self):
        rec = Recorder()
        cancel = asyncio.Event()

        async def fetch(pid):
            return make_playlist(10)

        async def send(text):
            await rec.send(text)
            if text.startswith("[3/"):
                cancel.set()

        result = await run_playlist_job(playlist_id="PLx", send=send, fetch=fetch, cancel_event=cancel, delay=0.1, progress_every=0)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(rec.links), 3)
        self.assertIn("Cancelled. Sent 3 of 10", rec.messages[-1])


class TaskManagerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def blocking_job(started: asyncio.Event | None = None):
        async def job(cancel_event: asyncio.Event):
            if started:
                started.set()
            await cancel_event.wait()

        return job

    async def test_start_duplicate_and_busy(self):
        manager = TaskManager()
        self.assertIs(manager.start(1, "A", self.blocking_job()), StartResult.STARTED)
        self.assertIs(manager.start(1, "A", self.blocking_job()), StartResult.DUPLICATE)
        self.assertIs(manager.start(1, "B", self.blocking_job()), StartResult.BUSY)
        self.assertIs(manager.start(2, "A", self.blocking_job()), StartResult.STARTED)  # other user unaffected
        await manager.shutdown()

    async def test_cancel_only_affects_own_task_and_cleans_up(self):
        manager = TaskManager()
        manager.start(1, "A", self.blocking_job())
        manager.start(2, "A", self.blocking_job())
        self.assertFalse(manager.cancel(3))  # user without a task
        self.assertTrue(manager.cancel(1))
        await asyncio.sleep(0.05)
        self.assertFalse(manager.is_active(1))
        self.assertTrue(manager.is_active(2))
        self.assertEqual(manager.active_count, 1)
        self.assertFalse(manager.cancel(1))  # nothing left to cancel
        await manager.shutdown()
        self.assertEqual(manager.active_count, 0)

    async def test_state_released_after_completion_and_restart_allowed(self):
        manager = TaskManager()

        async def quick(cancel_event):
            return None

        manager.start(1, "A", quick)
        await asyncio.sleep(0.05)
        self.assertEqual(manager.active_count, 0)
        self.assertIs(manager.start(1, "A", quick), StartResult.STARTED)
        await asyncio.sleep(0.05)

    async def test_state_released_after_crash(self):
        manager = TaskManager()

        async def crash(cancel_event):
            raise RuntimeError("bug")

        with self.assertLogs("tasks", level="ERROR"):
            manager.start(1, "A", crash)
            await asyncio.sleep(0.05)
        self.assertEqual(manager.active_count, 0)

    async def test_shutdown_stops_uncooperative_jobs(self):
        manager = TaskManager()
        started = asyncio.Event()

        async def stubborn(cancel_event):
            started.set()
            await asyncio.sleep(60)  # ignores cancel_event

        manager.start(1, "A", stubborn)
        await started.wait()
        await asyncio.wait_for(manager.shutdown(grace=0.1), timeout=3)
        self.assertEqual(manager.active_count, 0)


if __name__ == "__main__":
    unittest.main()
