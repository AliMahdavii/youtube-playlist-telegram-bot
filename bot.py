"""Telegram front-end: handlers, message sending with retries, startup."""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from datetime import timedelta

from telegram import LinkPreviewOptions, Update
from telegram.error import (
    BadRequest,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from config import Config, ConfigError, load_config
from playlist import PlaylistError, fetch_playlist_async, parse_playlist_url
from tasks import (
    Cancelled,
    FatalSendError,
    SendError,
    StartResult,
    TaskManager,
    run_playlist_job,
    sleep_or_cancel,
)

logger = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"(?:https?://\S+|(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S+)",
    re.IGNORECASE,
)
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

WELCOME = (
    "👋 Hi! Send me a YouTube playlist link and I'll reply with the link of every "
    "video in it, one message per video, in the playlist's real order, always starting "
    "from the first video (even if your link points to a video in the middle).\n\n"
    "Use /help for supported links and /cancel to stop a running task."
)
HELP = (
    "Supported links:\n"
    "• https://www.youtube.com/playlist?list=PLAYLIST_ID\n"
    "• https://www.youtube.com/watch?v=VIDEO_ID&list=PLAYLIST_ID\n"
    "• https://youtu.be/VIDEO_ID?list=PLAYLIST_ID\n\n"
    "A link to a single video without 'list=' can't be used. "
    "YouTube 'Mix' playlists, private playlists and your Liked/Watch Later lists are not supported.\n\n"
    "Commands:\n"
    "/start – introduction\n"
    "/help – this message\n"
    "/cancel – stop your current task"
)


# --------------------------------------------------------------------------- logging
class RedactingFormatter(logging.Formatter):
    """Replaces secrets (the bot token) in every log line, including tracebacks."""

    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def setup_logging(config: Config) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", [config.bot_token])
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.log_level)
    # httpx logs full request URLs (which contain the token) at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- sending
def _retry_seconds(exc: RetryAfter) -> float:
    value = exc.retry_after
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


async def send_text(bot, chat_id: int, text: str, cancel_event: asyncio.Event | None = None, attempts: int = 5) -> None:
    """Send one message, honouring Telegram's retry delays. Raises SendError/FatalSendError."""
    for attempt in range(1, attempts + 1):
        try:
            await bot.send_message(chat_id=chat_id, text=text, link_preview_options=_NO_PREVIEW)
            return
        except RetryAfter as exc:
            wait = _retry_seconds(exc) + 1
            logger.warning("Telegram rate limit hit; waiting %.0fs", wait)
        except Forbidden as exc:
            raise FatalSendError(str(exc)) from exc
        except BadRequest as exc:
            if "chat not found" in str(exc).lower():
                raise FatalSendError(str(exc)) from exc
            raise SendError(str(exc)) from exc
        except (TimedOut, NetworkError) as exc:
            wait = min(2.0**attempt, 20.0)
            logger.warning("Network problem sending message (%s); retrying in %.0fs", type(exc).__name__, wait)
        except TelegramError as exc:
            raise SendError(str(exc)) from exc

        if attempt < attempts:
            if cancel_event is not None:
                await sleep_or_cancel(wait, cancel_event)  # raises Cancelled if cancelled
            else:
                await asyncio.sleep(wait)
    raise SendError(f"gave up after {attempts} attempts")


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    return match.group(0).rstrip(").,;>]") if match else None


# --------------------------------------------------------------------------- handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(WELCOME)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(HELP, link_preview_options=_NO_PREVIEW)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None:
        return
    manager: TaskManager = context.application.bot_data["tasks"]
    if manager.cancel(user.id):  # only ever looks at the caller's own task
        await message.reply_text("Cancelling…")
    else:
        await message.reply_text("You have no active task.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or not message.text:
        return

    url = extract_url(message.text)
    if url is None:
        await message.reply_text("Please send a YouTube playlist link. See /help for examples.")
        return
    try:
        playlist_id = parse_playlist_url(url)
    except PlaylistError as exc:
        await message.reply_text(f"❌ {exc.user_message}")
        return

    config: Config = context.application.bot_data["config"]
    manager: TaskManager = context.application.bot_data["tasks"]
    bot, chat_id = context.bot, message.chat_id

    async def job(cancel_event: asyncio.Event) -> None:
        async def send(text: str) -> None:
            await send_text(bot, chat_id, text, cancel_event)

        async def fetch(pid: str):
            return await fetch_playlist_async(
                pid,
                max_entries=config.max_playlist_entries,
                timeout=config.extract_timeout,
                retries=config.extract_retries,
            )

        await run_playlist_job(
            playlist_id=playlist_id,
            send=send,
            fetch=fetch,
            cancel_event=cancel_event,
            delay=config.message_delay,
            progress_every=config.progress_every,
        )

    status = manager.start(user.id, playlist_id, job)
    if status is StartResult.DUPLICATE:
        await message.reply_text("I'm already processing that playlist for you. Use /cancel to stop it.")
    elif status is StartResult.BUSY:
        await message.reply_text("You already have a playlist running. Wait for it to finish or use /cancel first.")
    else:
        logger.info("Started job for user %s, playlist %s", user.id, playlist_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, Conflict):
        logger.error(
            "Telegram Conflict: another process is polling with this same bot token. "
            "Make sure only ONE instance is running (stop local runs / old deployments)."
        )
    elif isinstance(error, NetworkError):
        logger.warning("Network error talking to Telegram: %s", error)
    else:
        logger.error("Unhandled error while handling an update", exc_info=error)


# --------------------------------------------------------------------------- app wiring
async def _post_stop(application: Application) -> None:
    logger.info("Shutting down: stopping active jobs…")
    await application.bot_data["tasks"].shutdown()


def build_application(config: Config) -> Application:
    application = ApplicationBuilder().token(config.bot_token).post_stop(_post_stop).build()
    application.bot_data["config"] = config
    application.bot_data["tasks"] = TaskManager()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(on_error)
    return application


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config)
    application = build_application(config)
    logger.info("Starting bot with long polling…")
    try:
        application.run_polling(allowed_updates=["message"], drop_pending_updates=True)
    except InvalidToken:
        logger.error("Telegram rejected BOT_TOKEN. Check it in @BotFather and update your configuration.")
        sys.exit(1)


if __name__ == "__main__":
    main()
