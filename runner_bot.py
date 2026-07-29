"""
Runner-side bot: runs on GitHub Actions.
- Receives UUID via dispatch payload
- Scans intermediate channel for post with matching UUID
- Downloads the video
- Uploads to GitHub Release
- Sends download link back to inSell (owner)
- Exits
"""
import os
import sys
import json
import asyncio
import logging
import aiohttp
import subprocess
import datetime
from telethon import TelegramClient

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
GH_PAT = os.environ.get("GH_PAT", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "linktofiletg/linktofile-bot")
OWNER_ID = 6576436474  # inSell
INTERMEDIATE_CHANNEL = -1004363509644
SESSION_FILE = "runner_session.session"

# Parse dispatch payload
DISPATCH_PAYLOAD = os.environ.get("DISPATCH_PAYLOAD", "{}")
try:
    payload = json.loads(DISPATCH_PAYLOAD)
except Exception:
    payload = {}

JOB_ID = payload.get("job_id", "")
FILENAME = payload.get("filename", f"video_{JOB_ID}.mp4")
USER_CHAT_ID = payload.get("chat_id", "")
USER_ID = payload.get("user_id", "")

logger.info(f"Job: {JOB_ID} | File: {FILENAME} | User chat: {USER_CHAT_ID}")

client = TelegramClient(SESSION_FILE, API_ID, API_HASH)


def _upload_to_release(file_path, tag=None):
    """Upload file to GitHub Release, return download URL."""
    if not tag:
        tag = f"v{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"

    subprocess.run(
        ["gh", "release", "create", tag, file_path,
         "--title", f"File {tag}",
         "--notes", f"Auto-uploaded by runner bot (job: {JOB_ID})",
         "--repo", REPO],
        check=True, env={**os.environ, "GH_TOKEN": GH_PAT}
    )
    logger.info(f"Release created: {tag}")

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


async def find_video_in_channel():
    """Scan channel for post with UUID in caption."""
    logger.info(f"Scanning channel {INTERMEDIATE_CHANNEL} for UUID={JOB_ID}...")
    
    # Try up to 10 times, 10s apart (wait for user_client to forward)
    for attempt in range(10):
        msgs = await client.get_messages(INTERMEDIATE_CHANNEL, limit=20)
        for m in msgs:
            caption = (m.caption or "") + " " + (m.text or "")
            if JOB_ID in caption and (m.video or (m.document and m.document.mime_type and m.document.mime_type.startswith("video/"))):
                logger.info(f"Found video with UUID {JOB_ID} (msg {m.id})")
                return m
        logger.info(f"Attempt {attempt+1}: UUID not found yet, waiting 10s...")
        await asyncio.sleep(10)
    
    return None


async def main():
    await client.connect()
    
    if not await client.is_user_authorized():
        logger.error("Session not authorized!")
        return
    
    me = await client.get_me()
    logger.info(f"Runner connected as: {me.first_name} (ID: {me.id})")
    
    # Find video in channel by UUID
    video_msg = await find_video_in_channel()
    if not video_msg:
        logger.error(f"Video with UUID {JOB_ID} not found in channel!")
        await client.send_message(OWNER_ID, f"❌ Video not found in channel!\n🆔 {JOB_ID}")
        await client.disconnect()
        return
    
    # Download video
    tmp_path = f"/tmp/{FILENAME}"
    logger.info(f"Downloading {FILENAME}...")
    await client.download_media(video_msg, file=tmp_path)
    size = os.path.getsize(tmp_path)
    logger.info(f"Downloaded: {FILENAME} ({size/1e6:.1f} MB)")
    
    # Upload to GitHub Release
    logger.info("Uploading to GitHub Release...")
    download_url, tag = _upload_to_release(tmp_path)
    os.remove(tmp_path)
    
    if download_url:
        logger.info(f"Release URL: {download_url}")
        # Send link to inSell with UUID
        msg = f"RELEASE_RESULT::{FILENAME}::{size}::{download_url}::{USER_CHAT_ID}::{JOB_ID}"
        await client.send_message(OWNER_ID, msg)
        logger.info(f"Result sent to inSell: {msg}")
    else:
        logger.error("Upload failed!")
        await client.send_message(OWNER_ID, f"❌ Upload failed!\n🆔 {JOB_ID}")
    
    await asyncio.sleep(5)
    await client.disconnect()
    logger.info("Runner done.")


if __name__ == "__main__":
    asyncio.run(main())
