# linktofile-bot

Telegram bot that runs on GitHub Actions — processes videos/links and uploads to GitHub Releases.

## Architecture

```
User → Server Bot (our VPS) → trigger GitHub Actions
  → Runner Bot (GitHub Actions) starts
  → Server Bot forwards file to Runner Bot via Telegram
  → Runner Bot downloads → uploads to GitHub Release
  → Runner Bot sends download link back to Server Bot
  → Server Bot sends link to user
  → Runner shuts down
```

## Files

- `server_bot.py` — runs on our VPS (receives from user, triggers, forwards, delivers)
- `runner_bot.py` — runs on GitHub Actions runner (receives, downloads, uploads, returns link)
- `.github/workflows/runner.yml` — GitHub Actions workflow

## Required Secrets

| Secret | Value |
|--------|-------|
| `BOT_TOKEN` | Telegram bot token |
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API hash |
| `GH_PAT` | GitHub PAT for gh CLI |
| `TELETHON_SESSION_B64` | Base64-encoded Telethon session file |
| `SERVER_BOT_ID` | Server bot's Telegram user ID |
| `CALLBACK_URL` | Server bot callback URL (http://IP:8000/runner-callback) |
