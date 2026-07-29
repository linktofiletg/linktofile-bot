"""
Runner-side bot: runs on GitHub Actions.
- Connects with inSell user session
- Scans @ytbdwnmasebot chat for video with matching UUID
- Downloads video
- Uploads to GitHub Release
- Replies to the original user with the download link
- Exits
"""
import os
import sys
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
         "--notes", f"Auto-uploaded by runner (job: {JOB_ID})",
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


async def find_video_in_bot_chat():
    """Scan @ytbdwnmasebot chat for video. The UUID was sent as a separate message
    right after the forwarded video. We find the UUID message, then grab the
    message before it (which should be the forwarded video)."""
    logger.info(f"Scanning @{SERVER_BOT_USERNAME} chat for UUID={JOB_ID}...")

    for attempt in range(15):
        try:
            entity = await client.get_entity(SERVER_BOT_USERNAME)
            msgs = await client.get_messages(entity, limit=30)

            # Find the UUID message
            for i, m in enumerate(msgs):
                if m.text and JOB_ID in m.text:
                    logger.info(f"Found UUID message at index {i} (msg {m.id})")
                    # The video should be the message right before this one
                    # (forwarded video, then UUID text)
                    if i + 1 < len(msgs):
                        video_msg = msgs[i + 1]  # older message = next in list
                        if video_msg.video or (video_msg.document and
                            video_msg.document.mime_type and
                            video_msg.document.mime_type.startswith("video/")):
                            logger.info(f"Found video! msg {video_msg.id}, size={video_msg.file.size}")
                            return video_msg
                    # Also check message after (in case order is different)
                    if i > 0:
                        video_msg = msgs[i - 1]
                        if video_msg.video or (video_msg.document and
                            video_msg.document.mime_type and
                            video_msg.document.mime_type.startswith("video/")):
                            logger.info(f"Found video! msg {video_msg.id}, size={video_msg.file.size}")
                            return video_msg

            logger.info(f"Attempt {attempt+1}: UUID not found yet, waiting 10s...")
        except Exception as e:
            logger.warning(f"Scan error: {e}")
        await asyncio.sleep(10)

    return None


async def main():
    await client.connect()

    if not await client.is_user_authorized():
        logger.error("Session not authorized!")
        return

    me = await client.get_me()
    logger.info(f"Runner connected as: {me.first_name} (ID: {me.id})")

    # Find video in bot chat by UUID
    video_msg = await find_video_in_bot_chat()
    if not video_msg:
        logger.error(f"Video with UUID {JOB_ID} not found!")
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
        # Send link back to user via the server bot
        chat_id = int(USER_CHAT_ID)
        result_text = (
            f"✅ **Uploaded!**\n\n"
            f"📁 `{FILENAME}`\n"
            f"📦 {size/1e6:.1f} MB\n\n"
            f"🔗 {download_url}"
        )
        try:
            await client.send_message(chat_id, result_text, link_preview=False)
            logger.info(f"Link sent to user {chat_id}")
        except Exception as e:
            logger.error(f"Failed to send link to user: {e}")
    else:
        logger.error("Upload failed!")

    await asyncio.sleep(5)
    await client.disconnect()
    logger.info("Runner done.")


if __name__ == "__main__":
    asyncio.run(main())
