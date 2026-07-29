"""
Runner-side bot: runs on GitHub Actions.
- Receives forwarded video/link from server bot (same Telethon user)
- Downloads video or URL
- Uploads to GitHub Release
- Sends download link back to server bot
- Exits
"""
import os
import sys
import json
import asyncio
import logging
import aiohttp
import aiofiles
import subprocess
import datetime
from telethon import TelegramClient, events

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GH_PAT = os.environ.get("GH_PAT", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "linktofiletg/linktofile-bot")
SERVER_BOT_ID = int(os.environ.get("SERVER_BOT_ID", "0"))
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")  # http://95.182.92.43:8000/runner-callback

client = TelegramClient("runner_session", API_ID, API_HASH)


def _upload_to_release(file_path, tag=None):
    """Upload file to GitHub Release, return download URL."""
    if not tag:
        tag = f"v{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"

    subprocess.run(
        ["gh", "release", "create", tag, file_path,
         "--title", f"File {tag}",
         "--notes", "Auto-uploaded by runner bot",
         "--repo", REPO],
        check=True, env={**os.environ, "GH_TOKEN": GH_PAT}
    )
    logger.info(f"Release created: {tag}")

    # Get download URL
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/tags/{tag}",
        headers={"Authorization": f"token {GH_PAT}"}
    )
    resp = urllib.request.urlopen(req, timeout=10)
    data = json.loads(resp.read())
    assets = data.get("assets", [])
    if assets:
        return assets[0]["browser_download_url"], tag
    return None, tag


async def _send_to_server_bot(message_text):
    """Send result back to server bot via Telegram or HTTP callback."""
    if CALLBACK_URL:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(CALLBACK_URL, json={"text": message_text}, timeout=aiohttp.ClientTimeout(total=10))
                logger.info("Callback sent to server bot")
                return
        except Exception as e:
            logger.warning(f"Callback failed: {e}")
    # Fallback: send directly via Telegram to server bot
    if SERVER_BOT_ID:
        await client.send_message(SERVER_BOT_ID, message_text)
        logger.info("Sent to server bot via Telegram")


@client.on(events.NewMessage())
async def handle(event):
    if event.message.out:
        return

    msg = event.message
    chat_id = event.chat_id
    logger.info(f"Received message from {chat_id}: {'video' if msg.video else 'document' if msg.document else 'text'}")

    # Only process from server bot or our own user
    if SERVER_BOT_ID and event.sender_id != SERVER_BOT_ID:
        # Allow from ourselves too (same user, different bot)
        me = await client.get_me()
        if event.sender_id != me.id:
            return

    # Handle video
    if msg.video or (msg.document and msg.document.mime_type and msg.document.mime_type.startswith("video/")):
        await _handle_video(event, chat_id)
    elif msg.document:
        # Non-video document: upload directly
        await _handle_document(event, chat_id)
    elif msg.text and msg.text.startswith("http"):
        await _handle_url(event, chat_id)
    elif msg.text == "/start":
        await event.reply("🤖 Runner bot is alive! Send me a video or link.")
    else:
        # Check if it's a URL
        from urllib.parse import urlparse
        if msg.text:
            parsed = urlparse(msg.text.strip())
            if parsed.scheme in ("http", "https"):
                await _handle_url(event, chat_id)


async def _handle_video(event, chat_id):
    wait = await event.reply("⏳ Receiving video on runner...")
    file = event.message.file
    if not file:
        await wait.edit("❌ No file found.")
        return

    if file.size and file.size > 2 * 1024 * 1024 * 1024:
        await wait.edit("❌ File too large (max 2GB).")
        return

    original_name = file.name or f"video_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    tmp_path = f"/tmp/{original_name}"

    async def progress(current, total):
        pct = (current / total) * 100
        await wait.edit(f"⏳ Downloading from Telegram...\n📥 {current/1e6:.1f} / {total/1e6:.1f} MB\n📊 {pct:.1f}%")

    try:
        await progress(0, file.size or 1)
        await client.download_media(event.message, file=tmp_path, progress_callback=progress)
        size = os.path.getsize(tmp_path)
        logger.info(f"Downloaded: {original_name} ({size/1e6:.1f} MB)")

        await wait.edit("📤 Uploading to GitHub Release...")
        download_url, tag = _upload_to_release(tmp_path)
        os.remove(tmp_path)

        if download_url:
            result = f"✅ Uploaded!\n📁 {original_name}\n📦 {size/1e6:.1f} MB\n🔗 {download_url}"
            await wait.edit(result)
            # Send to SERVER BOT (not user) — server bot forwards to user
            # Format: RELEASE_RESULT::filename::size::url::chat_id
            await _send_to_server_bot(f"RELEASE_RESULT::{original_name}::{size}::{download_url}::{chat_id}")
            logger.info("Done! Shutting down in 10s...")
            await asyncio.sleep(10)
            await client.disconnect()
        else:
            await wait.edit("❌ Upload failed — no download URL.")
    except Exception as e:
        logger.exception("Video processing failed")
        await wait.edit(f"❌ Error: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def _handle_document(event, chat_id):
    wait = await event.reply("⏳ Receiving file on runner...")
    file = event.message.file
    original_name = file.name or f"file_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    tmp_path = f"/tmp/{original_name}"

    try:
        await client.download_media(event.message, file=tmp_path)
        size = os.path.getsize(tmp_path)
        await wait.edit("📤 Uploading to GitHub Release...")
        download_url, tag = _upload_to_release(tmp_path)
        os.remove(tmp_path)
        if download_url:
            result = f"✅ Uploaded!\n📁 {original_name}\n📦 {size/1e6:.1f} MB\n🔗 {download_url}"
            await wait.edit(result)
            await _send_to_server_bot(f"RELEASE_RESULT::{original_name}::{size}::{download_url}::{chat_id}")
            await asyncio.sleep(10)
            await client.disconnect()
    except Exception as e:
        logger.exception("Document processing failed")
        await wait.edit(f"❌ Error: {e}")


async def _handle_url(event, chat_id):
    url = event.message.text.strip()
    wait = await event.reply("⏳ Downloading from URL on runner...")
    tmp_path = f"/tmp/download_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"

    try:
        timeout = aiohttp.ClientTimeout(total=3600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    await wait.edit(f"❌ Download failed: HTTP {resp.status}")
                    return

                # Get filename
                cd = resp.headers.get("Content-Disposition", "")
                from urllib.parse import urlparse
                original_name = "downloaded_file"
                if "filename=" in cd:
                    original_name = cd.split("filename=")[-1].split(";")[0].strip('"\'')
                else:
                    bn = os.path.basename(urlparse(url).path)
                    if bn:
                        original_name = bn

                total = int(resp.headers.get("Content-Length", 0)) or 1
                downloaded = 0
                async with aiofiles.open(tmp_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        pct = (downloaded / total) * 100
                        if downloaded % (10 * 1024 * 1024) < 1024 * 1024:
                            await wait.edit(f"⏳ Downloading...\n📥 {downloaded/1e6:.1f} / {total/1e6:.1f} MB\n📊 {pct:.1f}%")

        size = os.path.getsize(tmp_path)
        await wait.edit("📤 Uploading to GitHub Release...")
        download_url, tag = _upload_to_release(tmp_path, )
        os.remove(tmp_path)

        if download_url:
            result = f"✅ Uploaded!\n📁 {original_name}\n📦 {size/1e6:.1f} MB\n🔗 {download_url}"
            await wait.edit(result)
            await _send_to_server_bot(f"RELEASE_RESULT::{original_name}::{size}::{download_url}::{chat_id}")
            await asyncio.sleep(10)
            await client.disconnect
    except Exception as e:
        logger.exception("URL processing failed")
        await wait.edit(f"❌ Error: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


async def main():
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    logger.info(f"Runner bot started: {me.username or me.id}")
    logger.info(f"Waiting for forwarded message from server bot (ID={SERVER_BOT_ID})...")
    # Auto-shutdown after 5 minutes if no message
    async def _timeout():
        await asyncio.sleep(300)
        logger.warning("Timeout: no message received in 5 minutes. Shutting down.")
        await client.disconnect()
    asyncio.create_task(_timeout())
    await client.run_until_disconnected()
    logger.info("Runner bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
