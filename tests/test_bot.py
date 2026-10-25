"""Tests for bot.py. Skipped automatically if python-telegram-bot isn't installed."""

import asyncio
import importlib.util
import logging
import unittest
from types import SimpleNamespace
from unittest import mock

from playlist import Playlist, VideoEntry

TELEGRAM_AVAILABLE = importlib.util.find_spec("telegram") is not None

if TELEGRAM_AVAILABLE:
    import bot
    from config import Config
    from tasks import FatalSendError, SendError, TaskManager
    from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

TOKEN = "123456789:AAH" + "x" * 32


class FakeBot:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(text)


class FakeMessage:
    def __init__(self, text, chat_id=10):
        self.text, self.chat_id, self.replies = text, chat_id, []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


def make_update(text, user_id=1):
    message = FakeMessage(text)
    return SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user_id)), message


@unittest.skipUnless(TELEGRAM_AVAILABLE, "python-telegram-bot not installed")
class SendTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        fake = FakeBot()
        await bot.send_text(fake, 1, "hi")
        self.assertEqual(fake.sent, ["hi"])

    async def test_retry_after_then_success(self):
        fake = FakeBot([RetryAfter(1)])
        waits = mock.AsyncMock()
        with mock.patch.object(bot, "sleep_or_cancel", waits):
            await bot.send_text(fake, 1, "hi", asyncio.Event())
        self.assertEqual(fake.sent, ["hi"])
        self.assertGreaterEqual(waits.await_args.args[0], 2)  # honours retry_after (+1s margin)

    async def test_network_error_then_success(self):
        fake = FakeBot([NetworkError("down")])
        with mock.patch("asyncio.sleep"):
            await bot.send_text(fake, 1, "hi")
        self.assertEqual(fake.sent, ["hi"])

    async def test_gives_up(self):
        fake = FakeBot([NetworkError("down")] * 3)
        with mock.patch("asyncio.sleep"), self.assertRaises(SendError):
            await bot.send_text(fake, 1, "hi", attempts=3)

    async def test_forbidden_is_fatal(self):
        with self.assertRaises(FatalSendError):
            await bot.send_text(FakeBot([Forbidden("blocked")]), 1, "hi")

    async def test_bad_request_is_not_retried(self):
        fake = FakeBot([BadRequest("message is too long")])
        with self.assertRaises(SendError):
            await bot.send_text(fake, 1, "hi")

    async def test_chat_not_found_is_fatal(self):
        with self.assertRaises(FatalSendError):
            await bot.send_text(FakeBot([BadRequest("Chat not found")]), 1, "hi")


@unittest.skipUnless(TELEGRAM_AVAILABLE, "python-telegram-bot not installed")
class HelperTests(unittest.TestCase):
    def test_extract_url(self):
        self.assertEqual(bot.extract_url("look: https://youtu.be/abc?list=PLxyz."), "https://youtu.be/abc?list=PLxyz")
        self.assertEqual(bot.extract_url("youtube.com/playlist?list=PLxyz"), "youtube.com/playlist?list=PLxyz")
        self.assertIsNone(bot.extract_url("no link here"))

    def test_log_redaction_covers_message_and_traceback(self):
        formatter = bot.RedactingFormatter("%(message)s", [TOKEN])
        try:
            raise RuntimeError(f"failed for {TOKEN}")
        except RuntimeError:
            import sys

            record = logging.LogRecord("x", logging.ERROR, __file__, 1, f"url /bot{TOKEN}/getMe", None, sys.exc_info())
        text = formatter.format(record)
        self.assertNotIn(TOKEN, text)
        self.assertIn("***", text)


@unittest.skipUnless(TELEGRAM_AVAILABLE, "python-telegram-bot not installed")
class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, fake_bot):
        config = Config(bot_token=TOKEN, progress_every=0, message_delay=0.0)
        app = SimpleNamespace(bot_data={"config": config, "tasks": TaskManager()})
        return SimpleNamespace(application=app, bot=fake_bot), app.bot_data["tasks"]

    async def test_message_flow_sends_links_in_order_from_first(self):
        fake_bot = FakeBot()
        context, manager = self.make_context(fake_bot)
        entries = [VideoEntry(i, f"vid{i:08d}", f"T{i}", f"https://www.youtube.com/watch?v=vid{i:08d}") for i in (1, 2, 3)]
        playlist = Playlist("PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf", "L", entries, [], total=3)
        update, message = make_update("https://www.youtube.com/watch?v=vid00000002&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=2")

        with mock.patch.object(bot, "fetch_playlist_async", mock.AsyncMock(return_value=playlist)):
            await bot.handle_message(update, context)
            for _ in range(200):  # wait (max ~2s) for the background job to finish
                if not manager.is_active(1):
                    break
                await asyncio.sleep(0.01)

        self.assertFalse(manager.is_active(1))
        links = [m for m in fake_bot.sent if m.startswith("[")]
        # The user linked video 2, but delivery starts at entry 1 and keeps playlist order.
        self.assertEqual(
            links,
            [f"[{i}/3] T{i}\nhttps://www.youtube.com/watch?v=vid0000000{i}" for i in (1, 2, 3)],
        )
        self.assertIn("Done. Sent all 3 links", fake_bot.sent[-1])

    async def test_invalid_and_non_playlist_urls_get_clear_replies(self):
        context, _ = self.make_context(FakeBot())
        update, message = make_update("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        await bot.handle_message(update, context)
        self.assertIn("without a playlist", message.replies[0])

        update, message = make_update("hello")
        await bot.handle_message(update, context)
        self.assertIn("YouTube playlist link", message.replies[0])

    async def test_duplicate_and_cancel(self):
        context, manager = self.make_context(FakeBot())
        gate = asyncio.Event()

        async def slow_fetch(*args, **kwargs):
            await gate.wait()

        url = "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
        with mock.patch.object(bot, "fetch_playlist_async", slow_fetch):
            update, _ = make_update(url, user_id=1)
            await bot.handle_message(update, context)
            update2, message2 = make_update(url, user_id=1)
            await bot.handle_message(update2, context)
            self.assertIn("already processing", message2.replies[0])

            other, other_msg = make_update("/cancel", user_id=2)
            await bot.cancel_command(other, context)
            self.assertIn("no active task", other_msg.replies[0])
            self.assertTrue(manager.is_active(1))  # user 2 could not cancel user 1

            own, own_msg = make_update("/cancel", user_id=1)
            await bot.cancel_command(own, context)
            self.assertEqual(own_msg.replies[0], "Cancelling…")
            await asyncio.sleep(0.1)
            self.assertFalse(manager.is_active(1))
            await manager.shutdown()


if __name__ == "__main__":
    unittest.main()
