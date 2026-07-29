"""
Updated bot.py for server side.
- Receives video/link from user
- Triggers GitHub Actions (repository_dispatch)
- Forwards file to runner bot (same Telethon user, different bot)
- Receives callback from runner with download link
- Sends link to user
"""
import os
import uuid
import asyncio
import logging
import aiohttp
import aiofiles
import json
from urllib.parse import urlparse
from telethon import TelegramClient, events, Button
from config import API_ID, API_HASH, BOT_TOKEN, FILES_DIR, SERVER_BASE_URL, RETENTION_HOURS, CLEANUP_INTERVAL_MINUTES, MAX_FILE_SIZE
from database import Database

logger = logging.getLogger(__name__)
db = Database()
client = TelegramClient("bot_session", API_ID, API_HASH)

# GitHub trigger config
GH_PAT = os.environ.get("GH_PAT", "")
GH_REPO = os.environ.get("GH_REPO", "linktofiletg/linktofile-bot")


def _new_id():
    return uuid.uuid4().hex


def _format_size(b):
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


async def _trigger_github_action(payload: dict):
    """Trigger GitHub Actions workflow via repository_dispatch."""
    url = f"https://api.github.com/repos/{GH_REPO}/dispatches"
    data = {"event_type": "process-file", "client_payload": payload}
    headers = {
        "Authorization": f"token {GH_PAT}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 204:
                    logger.info(f"GitHub action triggered: {payload}")
                    return True
                else:
                    text = await resp.text()
                    logger.error(f"GitHub trigger failed: {resp.status} {text}")
                    return False
    except Exception as e:
        logger.error(f"GitHub trigger error: {e}")
        return False


# Pending jobs: map original chat_id → wait for runner response
pending_jobs = {}


@client.on(events.NewMessage(pattern="/start"))
async def cmd_start(event):
    await event.reply(
        f"🎬 **VideoLinkBot**\n\n"
        f"📤 Send me a **video** → I'll process it on GitHub Actions\n"
        f"📥 Send me a **link** → I'll download and process it\n\n"
        f"⚡ Files uploaded to GitHub Releases (24h link)\n"
        f"📦 Max file size: **2 GB**"
    )


@client.on(events.NewMessage())
async def handle_message(event):
    if event.message.out:
        return
    if event.message.text and event.message.text.startswith("/"):
        return

    if event.message.video:
        await handle_video(event)
    elif event.message.document:
        await handle_document(event)
    elif event.message.text:
        await handle_text(event)


async def handle_video(event):
    wait_msg = await event.reply("⏳ Receiving your video...")
    msg = event.message
    file = msg.file

    if not file or (file.size and file.size > MAX_FILE_SIZE):
        await wait_msg.edit("❌ File too large (max 2 GB).")
        return

    original_name = file.name or f"video_{_new_id()[:8]}.mp4"
    chat_id = event.chat_id
    user_id = event.sender_id

    # Store job as pending
    job_id = _new_id()
    pending_jobs[job_id] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "filename": original_name,
        "type": "video",
        "status": "triggering"
    }

    # Trigger GitHub Action
    payload = {
        "job_id": job_id,
        "type": "video",
        "filename": original_name,
        "chat_id": str(chat_id),
        "user_id": str(user_id)
    }
    triggered = await _trigger_github_action(payload)

    if not triggered:
        await wait_msg.edit("❌ Failed to start processing. Try again later.")
        pending_jobs.pop(job_id, None)
        return

    await wait_msg.edit(
        f"✅ Processing started on GitHub Actions!\n"
        f"📁 `{original_name}`\n"
        f"🆔 Job: `{job_id[:8]}`\n\n"
        f"⏳ Runner is starting up... I'll forward your video to it shortly."
    )

    # Wait a bit for runner to come up, then forward the file
    await asyncio.sleep(60)  # Wait for runner to start

    # Forward the original message to runner bot (same user sees both bots)
    # The runner bot will receive this as the same user
    runner_bot_username = os.environ.get("RUNNER_BOT_USERNAME", "")
    if runner_bot_username:
        try:
            await wait_msg.edit(f"📤 Forwarding video to runner bot (@{runner_bot_username})...")
            # Forward the original video message to the runner bot
            await client.forward_messages(runner_bot_username, msg)
            await wait_msg.edit(
                f"📤 Video forwarded to runner!\n"
                f"⏳ Waiting for upload to GitHub Release...\n"
                f"🆔 Job: `{job_id[:8]}`"
            )
        except Exception as e:
            logger.error(f"Forward to runner failed: {e}")
            await wait_msg.edit(f"⚠️ Forward failed: {e}\nThe runner might still be starting.")
    else:
        await wait_msg.edit("⚠️ RUNNER_BOT_USERNAME not set. Cannot forward.")


async def handle_document(event):
    doc = event.message.document
    if doc and doc.mime_type and doc.mime_type.startswith("video/"):
        await handle_video(event)
    else:
        # Non-video document — still trigger runner
        await handle_video(event)  # reuse logic


async def handle_text(event):
    text = event.message.text.strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        await event.reply("Please send a video file or a valid URL (http/https).")
        return

    chat_id = event.chat_id
    user_id = event.sender_id
    job_id = _new_id()
    pending_jobs[job_id] = {
        "chat_id": chat_id,
        "user_id": user_id,
        "url": text,
        "type": "url",
        "status": "triggering"
    }

    # Trigger GitHub Action
    payload = {
        "job_id": job_id,
        "type": "url",
        "url": text,
        "chat_id": str(chat_id),
        "user_id": str(user_id)
    }
    triggered = await _trigger_github_action(payload)

    if not triggered:
        await event.reply("❌ Failed to start processing. Try again later.")
        pending_jobs.pop(job_id, None)
        return

    await event.reply(
        f"✅ Processing started on GitHub Actions!\n"
        f"🔗 `{text[:50]}...`\n"
        f"🆔 Job: `{job_id[:8]}`\n\n"
        f"⏳ Runner is starting up..."
    )

    # For URL type, forward the URL to runner bot
    await asyncio.sleep(60)
    runner_bot_username = os.environ.get("RUNNER_BOT_USERNAME", "")
    if runner_bot_username:
        try:
            await client.send_message(runner_bot_username, text)
        except Exception as e:
            logger.error(f"Send URL to runner failed: {e}")


async def cleanup_loop():
    while True:
        try:
            expired = db.get_expired_files()
            count = 0
            for rec in expired:
                fp = os.path.join(FILES_DIR, rec["stored_name"])
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                        count += 1
                except Exception as e:
                    logger.error(f"Cleanup error {rec['stored_name']}: {e}")
                db.mark_deleted(rec["id"])
            if count:
                logger.info(f"Cleaned up {count} expired files")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(CLEANUP_INTERVAL_MINUTES * 60)


async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    logger.info(f"Server bot started: @{me.username} (ID: {me.id})")
    asyncio.create_task(cleanup_loop())
    await client.run_until_disconnected()
