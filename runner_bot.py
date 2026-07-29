"""
Runner-side bot: runs on GitHub Actions.
- Connects with inSell user session
- Scans @ytbdwnmasebot chat for video with matching UUID (fast)
- Downloads video
- Uploads to GitHub Release
- Sends download link to server bot → server bot replies to user
- Exits
"""
import os
import json
import asyncio
import logging
import subprocess
import datetime
from telethon import TelegramClient

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
GH_PAT = os.environ.get("GH_PAT", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "linktofiletg/linktofile-bot")
SERVER_BOT_USERNAME = "ytbdwnmasebot"
SESSION_FILE = "runner_session.session"

DISPATCH_PAYLOAD = os.environ.get("DISPATCH_PAYLOAD", "{}")
try:
    payload = json.loads(DISPATCH_PAYLOAD)
except Exception:
    payload = {}

JOB_ID = payload.get("job_id", "")
FILENAME = payload.get("filename", f"video_{JOB_ID}.mp4")
USER_CHAT_ID = payload.get("chat_id", "")

logger.info(f"Job: {JOB_ID} | File: {FILENAME} | User chat: {USER_CHAT_ID}")

client = TelegramClient(SESSION_FILE, API_ID, API_HASH)


def _upload_to_release(file_path):
    """Upload file to GitHub Release, return download URL."""
    tag = f"v{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    subprocess.run(
        ["gh", "release", "create", tag, file_path,
         "--title", f"File {tag}",
         "--notes", f"Runner (job: {JOB_ID})",
         "--repo", REPO],
        check=True, env={**os.environ, "GH_TOKEN": GH_PAT}
    )
    logger.info(f"Release: {tag}")

    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases/tags/{tag}",
        headers={"Authorization": f"token {GH_PAT}"}
    )
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    assets = data.get("assets", [])
    if assets:
        return assets[0]["browser_download_url"]
    return None


async def find_video():
    """Fast scan — video is already in chat, UUID was sent right after it."""
    logger.info(f"Scanning @{SERVER_BOT_USERNAME} for UUID={JOB_ID} (fast)...")
    entity = await client.get_entity(SERVER_BOT_USERNAME)

    # Try 3 times with 3s gaps (video is usually there immediately)
    for attempt in range(3):
        msgs = await client.get_messages(entity, limit=20)
        for i, m in enumerate(msgs):
            if m.text and JOB_ID in m.text:
                logger.info(f"UUID at msg {m.id} (index {i})")
                # Video is before or after the UUID message
                for check_idx in [i + 1, i - 1]:
                    if 0 <= check_idx < len(msgs):
                        v = msgs[check_idx]
                        if v.video or (v.document and v.document.mime_type and
                                       v.document.mime_type.startswith("video/")):
                            logger.info(f"Video msg {v.id}, size={v.file.size}")
                            return v
        if attempt < 2:
            logger.info(f"Retry {attempt+1}...")
            await asyncio.sleep(3)
    return None


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        logger.error("Session not authorized!")
        return

    me = await client.get_me()
    logger.info(f"Connected as: {me.first_name}")

    video_msg = await find_video()
    if not video_msg:
        logger.error("Video not found!")
        await client.disconnect()
        return

    # Download
    tmp = f"/tmp/{FILENAME}"
    logger.info("Downloading...")
    await client.download_media(video_msg, file=tmp)
    size = os.path.getsize(tmp)
    logger.info(f"Downloaded {size/1e6:.1f} MB")

    # Release
    logger.info("Releasing...")
    url = _upload_to_release(tmp)
    os.remove(tmp)

    if url:
        logger.info(f"URL: {url}")
        bot = await client.get_entity(SERVER_BOT_USERNAME)
        await client.send_message(
            bot,
            f"LINK_RESULT::{JOB_ID}::{url}::{FILENAME}::{size}::{USER_CHAT_ID}"
        )
        logger.info(f"Sent to server bot for user {USER_CHAT_ID}")
    else:
        logger.error("Upload failed!")

    await asyncio.sleep(3)
    await client.disconnect()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
