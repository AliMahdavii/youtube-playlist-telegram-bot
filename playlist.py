"""YouTube URL parsing and playlist extraction (no Telegram code in here).

The playlist's own order is the source of truth: entry N in the list returned
by yt-dlp is position N. Nothing is sorted and titles are never parsed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_V_PARAM_RE = re.compile(r"[?&]v=([A-Za-z0-9_-]{11})(?:[&#]|$)")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_TITLE_LENGTH = 200


# --------------------------------------------------------------------------- errors
class PlaylistError(Exception):
    """Base class. ``user_message`` is safe to show to the Telegram user."""

    def __init__(self, user_message: str):
        super().__init__(user_message)
        self.user_message = user_message


class InvalidURLError(PlaylistError):
    pass


class NoPlaylistError(PlaylistError):
    pass


class UnsupportedPlaylistError(PlaylistError):
    pass


class EmptyPlaylistError(PlaylistError):
    pass


class PlaylistUnavailableError(PlaylistError):
    """Permanent problem: private, deleted or non-existent playlist."""


class PlaylistExtractionError(PlaylistError):
    """Possibly temporary problem: network, timeout, YouTube blocking/changes."""


# --------------------------------------------------------------------------- data
@dataclass(frozen=True)
class VideoEntry:
    position: int  # 1-based position in the playlist
    video_id: str
    title: str
    url: str


@dataclass(frozen=True)
class SkippedEntry:
    position: int
    reason: str


@dataclass
class Playlist:
    playlist_id: str
    title: str
    entries: list[VideoEntry]
    skipped: list[SkippedEntry]
    total: int  # number of raw entries examined (playable + skipped)
    reported_count: int | None = None  # what YouTube says the size is, if known
    truncated: bool = False  # True if the playlist was cut at MAX_PLAYLIST_ENTRIES

    @property
    def count_mismatch(self) -> bool:
        return (
            not self.truncated
            and self.reported_count is not None
            and self.reported_count != self.total
        )


# --------------------------------------------------------------------------- URLs
def parse_playlist_url(text: str) -> str:
    """Return the playlist ID found in ``text`` or raise a PlaylistError.

    Only the ``list`` query parameter is used. The ``v`` parameter (the video the
    user happened to click) is deliberately ignored.
    """
    candidate = (text or "").strip()
    if not candidate or len(candidate) > 2048 or any(c.isspace() for c in candidate):
        raise InvalidURLError("That doesn't look like a valid link.")
    if "://" not in candidate:
        candidate = "https://" + candidate

    try:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
    except ValueError:
        raise InvalidURLError("That doesn't look like a valid link.") from None

    if parsed.scheme not in ("http", "https") or host not in _YOUTUBE_HOSTS:
        raise InvalidURLError("Please send a youtube.com or youtu.be link.")

    path = parsed.path or "/"
    is_short = host in _SHORT_HOSTS
    is_video_path = path.startswith(("/watch", "/embed")) or (is_short and path.strip("/") != "")
    is_playlist_path = path.startswith("/playlist")
    if not (is_video_path or is_playlist_path):
        raise InvalidURLError("That YouTube link is not a video or playlist link.")

    list_values = parse_qs(parsed.query).get("list")
    if not list_values or not list_values[0]:
        if is_video_path:
            raise NoPlaylistError(
                "That link points to a single video without a playlist. "
                "Please send a playlist link (it contains 'list=...')."
            )
        raise InvalidURLError("That playlist link has no 'list=' parameter.")

    playlist_id = list_values[0]
    if not _PLAYLIST_ID_RE.match(playlist_id):
        raise InvalidURLError("The playlist ID in that link is not valid.")
    if playlist_id.startswith("RD"):
        raise UnsupportedPlaylistError(
            "YouTube 'Mix' playlists are generated per viewer and can't be listed reliably."
        )
    return playlist_id


def build_video_url(video_id: str) -> str:
    if not _VIDEO_ID_RE.match(video_id or ""):
        raise ValueError(f"Invalid video ID: {video_id!r}")
    return f"https://www.youtube.com/watch?v={video_id}"


def build_playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


# --------------------------------------------------------------------------- parsing yt-dlp output
def _clean_title(title: Any) -> str:
    if not isinstance(title, str):
        return ""
    cleaned = " ".join(_CONTROL_CHARS_RE.sub(" ", title).split())
    if len(cleaned) > MAX_TITLE_LENGTH:
        cleaned = cleaned[: MAX_TITLE_LENGTH - 1] + "…"
    return cleaned


def _video_id_of(entry: Mapping[str, Any]) -> str | None:
    video_id = entry.get("id")
    if isinstance(video_id, str) and _VIDEO_ID_RE.match(video_id):
        return video_id
    url = entry.get("url")
    if isinstance(url, str):
        match = _V_PARAM_RE.search(url)
        if match:
            return match.group(1)
    return None


def _unavailable_reason(title: str) -> str | None:
    """yt-dlp labels removed videos like '[Private video]' / '[Deleted video]'."""
    lowered = title.strip().lower()
    if lowered.startswith("[") and lowered.endswith("video]"):
        if "private" in lowered:
            return "private video"
        if "deleted" in lowered:
            return "deleted video"
        return "unavailable video"
    return None


def parse_playlist_info(info: Mapping[str, Any], playlist_id: str, max_entries: int) -> Playlist:
    """Convert yt-dlp's info dict into a Playlist, keeping the original order."""
    raw_entries = list(info.get("entries") or [])
    if not raw_entries:
        raise EmptyPlaylistError("That playlist is empty (or has no accessible videos).")

    truncated = len(raw_entries) > max_entries
    raw_entries = raw_entries[:max_entries]

    entries: list[VideoEntry] = []
    skipped: list[SkippedEntry] = []
    for position, raw in enumerate(raw_entries, start=1):
        if not isinstance(raw, Mapping):
            skipped.append(SkippedEntry(position, "no data returned"))
            continue
        title = _clean_title(raw.get("title"))
        reason = _unavailable_reason(title)
        if reason:
            skipped.append(SkippedEntry(position, reason))
            continue
        video_id = _video_id_of(raw)
        if video_id is None:
            skipped.append(SkippedEntry(position, "no usable video ID"))
            continue
        entries.append(VideoEntry(position, video_id, title, build_video_url(video_id)))

    count = info.get("playlist_count")
    return Playlist(
        playlist_id=playlist_id,
        title=_clean_title(info.get("title")) or "Untitled playlist",
        entries=entries,
        skipped=skipped,
        total=len(raw_entries),
        reported_count=count if isinstance(count, int) and count > 0 else None,
        truncated=truncated,
    )


# --------------------------------------------------------------------------- yt-dlp access
Extractor = Callable[[str, dict], Mapping[str, Any]]


def build_ydl_options(max_entries: int, socket_timeout: float = 20.0) -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "skip_download": True,  # never download media
        "extract_flat": "in_playlist",  # list entries only, don't visit each video
        "noplaylist": False,
        "writethumbnail": False,
        "playlistend": max_entries + 1,  # +1 lets us detect that the cap was hit
        "socket_timeout": socket_timeout,
        "retries": 3,
        "extractor_retries": 3,
    }


def classify_error(message: str) -> PlaylistError:
    lowered = message.lower()
    if "not a bot" in lowered or "confirm you" in lowered:
        return PlaylistExtractionError(
            "YouTube is asking the server to prove it isn't a bot, so the playlist "
            "can't be read from here right now. Please try again later."
        )
    if any(
        marker in lowered
        for marker in ("private", "does not exist", "not found", "unavailable", "removed", "terminated")
    ):
        return PlaylistUnavailableError(
            "I can't access that playlist. It may be private, deleted, or the link is wrong."
        )
    return PlaylistExtractionError(
        "I couldn't read that playlist from YouTube. This is often temporary, so please try again."
    )


def _ytdlp_extract(url: str, options: dict) -> Mapping[str, Any]:
    import yt_dlp  # imported lazily so URL parsing/tests don't need yt-dlp
    from yt_dlp.utils import DownloadError, ExtractorError

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except (DownloadError, ExtractorError) as exc:
        raise classify_error(str(exc)) from exc


def fetch_playlist(playlist_id: str, max_entries: int, extractor: Extractor | None = None) -> Playlist:
    """Blocking extraction. Always queries the canonical playlist URL (never a video URL)."""
    url = build_playlist_url(playlist_id)
    options = build_ydl_options(max_entries)
    info = (extractor or _ytdlp_extract)(url, options)
    if not isinstance(info, Mapping):
        raise PlaylistExtractionError("YouTube returned no data for that playlist.")
    return parse_playlist_info(info, playlist_id, max_entries)


async def fetch_playlist_async(
    playlist_id: str,
    *,
    max_entries: int,
    timeout: float,
    retries: int,
    backoff: float = 2.0,
    extractor: Extractor | None = None,
) -> Playlist:
    """Run extraction in a worker thread with a timeout and retries for transient errors."""
    last_error: PlaylistError | None = None
    for attempt in range(1, retries + 1):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fetch_playlist, playlist_id, max_entries, extractor),
                timeout=timeout,
            )
        except PlaylistExtractionError as exc:
            last_error = exc
        except PlaylistError:
            raise  # permanent: retrying will not help
        except asyncio.TimeoutError:
            last_error = PlaylistExtractionError("Reading the playlist timed out. Please try again.")
        except Exception as exc:  # noqa: BLE001 - network errors etc.
            logger.warning("Extraction attempt %d failed: %s", attempt, type(exc).__name__)
            last_error = PlaylistExtractionError(
                "I couldn't read that playlist from YouTube. This is often temporary, so please try again."
            )
        if attempt < retries:
            logger.info("Retrying playlist extraction (attempt %d/%d)", attempt + 1, retries)
            await asyncio.sleep(backoff * attempt)
    assert last_error is not None
    raise last_error
