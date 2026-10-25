"""Configuration loading and validation.

Values come from real environment variables (e.g. set in the Runflare panel)
or from a local ``.env`` file. Real environment variables always win over
``.env`` because ``load_dotenv`` does not override them by default.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Mapping

from dotenv import load_dotenv

# Telegram tokens look like "123456789:AAH...35 URL-safe characters...".
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    progress_every: int = 25
    message_delay: float = 1.0
    max_playlist_entries: int = 2000
    extract_timeout: float = 120.0
    extract_retries: int = 3
    log_level: str = "INFO"


def _get_number(env: Mapping[str, str], name: str, default, cast, minimum):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw.strip())
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}.") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}.")
    return value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Build a validated Config. Pass ``env`` in tests to avoid touching os.environ."""
    if env is None:
        load_dotenv()
        env = os.environ

    token = (env.get("BOT_TOKEN") or "").strip().strip("'\"")
    if not token:
        raise ConfigError(
            "BOT_TOKEN is not set. Create a .env file (see .env.example) or "
            "add BOT_TOKEN as an environment variable on your host."
        )
    if not _TOKEN_RE.match(token):
        raise ConfigError(
            "BOT_TOKEN does not look like a Telegram bot token "
            "(expected something like 123456789:AA...). "
            "Copy it again from @BotFather."
        )

    log_level = (env.get("LOG_LEVEL") or "INFO").strip().upper()
    if log_level not in logging.getLevelNamesMapping() or log_level == "NOTSET":
        raise ConfigError(f"LOG_LEVEL {log_level!r} is not a valid logging level.")

    return Config(
        bot_token=token,
        progress_every=_get_number(env, "PROGRESS_EVERY", 25, int, 0),
        message_delay=_get_number(env, "MESSAGE_DELAY_SECONDS", 1.0, float, 0.0),
        max_playlist_entries=_get_number(env, "MAX_PLAYLIST_ENTRIES", 2000, int, 1),
        extract_timeout=_get_number(env, "EXTRACT_TIMEOUT_SECONDS", 120.0, float, 5.0),
        extract_retries=_get_number(env, "EXTRACT_RETRIES", 3, int, 1),
        log_level=log_level,
    )
