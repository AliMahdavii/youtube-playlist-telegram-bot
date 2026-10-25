# YouTube Playlist Link Bot

A Telegram bot. Send it a YouTube playlist link and it replies with the URL of **every video, one message each, in the playlist's real order, always starting from entry #1** – even if your link points at video 15 of 100.

```
[1/100] First episode title
https://www.youtube.com/watch?v=VIDEO_ID
```

It never downloads or sends video files, only links. No YouTube API key, database or web server is needed.

## Project structure

```
bot.py            Telegram handlers, message sending (rate-limit/retry handling), startup
tasks.py          Per-user task manager, cooperative cancellation, delivery + progress logic
playlist.py       URL parsing, yt-dlp extraction, retries, entry parsing
config.py         Environment variables + validation
requirements.txt  Dependencies (version ranges)
.env.example      Placeholder settings (copy to .env locally)
Procfile          `worker: python bot.py`
Dockerfile        Fallback if your host builds from a Dockerfile
tests/            Unit tests (no network, no real token needed)
```

`tasks.py` was split from `bot.py` (a small deviation from the suggested layout) so that cancellation and ordering logic can be tested without Telegram installed.

## How it behaves

- Accepts `playlist?list=ID`, `watch?v=VIDEO&list=ID` and `youtu.be/VIDEO?list=ID` (also `m.` and `music.` hosts). Only `list=` is used; the clicked video is ignored.
- Always queries the canonical playlist URL with `yt-dlp` in flat mode (no media download), so the order is YouTube's own.
- Private/deleted entries keep their position (so numbering stays correct), are **not** sent, and are listed in the final summary. If YouTube reports a different size than was listed, or the playlist exceeds `MAX_PLAYLIST_ENTRIES`, the summary says so. It says "Done" only when nothing went wrong.
- One message per link, `MESSAGE_DELAY_SECONDS` apart (default 1s), progress message every `PROGRESS_EVERY` links, Telegram `RetryAfter` honoured, network errors retried.
- `/cancel` stops the user's own task (including a running lookup) and reports how many links were sent.
- Each user may run one playlist at a time; sending the same playlist again while it runs is refused.

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # Windows: copy .env.example .env
# edit .env and set BOT_TOKEN=<token from @BotFather>
python bot.py
```

Get a token: open [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token. The bot logs `Starting bot with long polling…`; then message it `/start`. **Stop the local run (Ctrl+C) before deploying**, see "Only one instance" below.

## Tests

```bash
python -m unittest discover -s tests -t . -v      # works with only the standard library
# or, if you have pytest:  pip install pytest && pytest
```

Tests mock YouTube and Telegram; they need no token and no network. `tests/test_bot.py` is skipped automatically unless `python-telegram-bot` is installed.

## Publish to GitHub

Create an empty repository on GitHub (no README), then from the project folder:

```bash
git init -b main
git add .
git status            # confirm .env is NOT listed (only .env.example)
git commit -m "Initial commit: YouTube playlist link bot"
git remote add origin https://github.com/YOUR_USERNAME/youtube-playlist-telegram-bot.git
git push -u origin main
```

## Deploy on Runflare

> **Verification note:** I could not find or read Runflare's official documentation while preparing this project, so nothing Runflare-specific below is verified. Menu names and options may differ; use your Runflare dashboard/docs where they disagree, and treat this as a checklist of *what* to configure.

1. **Source:** connect your GitHub repository (or use Runflare's CLI/upload if it offers one).
2. **Runtime:** Python **3.12** (the version this was tested with; 3.11+ should also work).
3. **Install/build command:** `pip install -r requirements.txt` (installs `python-telegram-bot`, `yt-dlp`, `python-dotenv`).
4. **Start command:** `python bot.py` (same as the `Procfile`'s `worker` line).
5. **Process type:** a long-running **worker/background** process. The bot opens no port and serves no HTTP. If the only option is a "web" service that must listen on a port or pass an HTTP health check, this bot will likely be restarted or marked unhealthy, so pick a worker-type option or ask Runflare support.
6. **Environment variables:** add `BOT_TOKEN` = your token (and any optional settings from `.env.example`) in the app's environment-variables section. Do not upload `.env`.
7. **Instances:** set to **1**. Disable autoscaling/extra replicas.
8. **If Runflare builds from a Dockerfile instead:** use the included `Dockerfile` (no port needed, start command is built in).
9. **Deploy**, then open the logs.

### Verify it works and stays online

- Logs (find them in the app's logs view): you should see `Starting bot with long polling…` and no tracebacks.
- Message the bot `/start`, then send a small playlist link. Replies mean it's live.
- Check again after a few hours and after any redeploy. If it stops replying, look at the logs and the app's restart/status indicator.

### Only one instance may poll per token

Telegram allows a single `getUpdates` consumer per bot token. Two running copies cause `Conflict: terminated by other getUpdates request` (the bot logs a clear message). To avoid it:

- Stop `python bot.py` on your computer before/after deploying (or use a separate test bot token locally).
- Keep Runflare at 1 instance, and delete old/duplicate apps using the same token.
- A brief Conflict during a redeploy (old and new containers overlapping) is harmless and clears itself.
- Don't set a webhook for this token elsewhere. If you previously did, remove it: `https://api.telegram.org/bot<TOKEN>/deleteWebhook` (the bot also drops pending updates at start).
- Last resort: revoke the token in @BotFather (`/revoke`), update `BOT_TOKEN`, redeploy.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Configuration error: BOT_TOKEN is not set` | Variable missing in `.env` or Runflare settings. |
| `BOT_TOKEN does not look like a Telegram bot token` | Placeholder left in, or token copied incompletely. |
| `Telegram rejected BOT_TOKEN` | Token revoked or wrong; get a fresh one from @BotFather. |
| `Conflict … getUpdates` | Another instance is running; see section above. |
| Bot silent, no logs | Process not running: check the start command and that it's a worker process. |
| "YouTube is asking the server to prove it isn't a bot" | YouTube often blocks hosting-provider IPs. Try later; see limitations. |
| "couldn't read that playlist" repeatedly | Update yt-dlp (`pip install -U yt-dlp`, redeploy): YouTube changes break old versions. |
| `ModuleNotFoundError` | Install step didn't run `pip install -r requirements.txt`. |
| Bot is slow on big playlists | Intentional: ~1 message/second avoids Telegram flood limits. Lower `MESSAGE_DELAY_SECONDS` at your own risk. |

## Limitations

- State is in memory: a restart or redeploy drops running tasks (users must resend the link). Duplicate protection only applies while a task runs.
- Telegram limits bots to about 1 message/second per chat and about 30/second overall; many simultaneous large playlists will be slowed by retries.
- Private playlists, Liked/Watch Later, and auto-generated Mix playlists are unsupported.
- YouTube may block datacenter IPs or change its site; a `yt-dlp` upgrade usually fixes the latter. Because requirements use a minimum version for `yt-dlp`, a fresh deploy picks up the newest release.
- A cancel during the YouTube lookup stops the bot's wait immediately, but the lookup thread finishes in the background and its result is discarded.
- If Telegram times out on a send that actually went through, that one link may arrive twice.
