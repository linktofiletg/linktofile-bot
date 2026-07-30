"""
Runner-side bot: runs on GitHub Actions.
- Connects with inSell user session
- Scans @ytbdwnmasebot chat for video with matching UUID
- Downloads video (parallel chunks for speed)
- Uploads to GitHub Release
- Sends download link to server bot → server bot replies to user
"""
import os
import json
import asyncio
import logging
import time
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

logger.info(f"Job: {JOB_ID} | File: {FILENAME} | User: {USER_CHAT_ID}")

# Use faster connection settings
client = TelegramClient(
    SESSION_FILE, API_ID, API_HASH,
    connection_retries=10,
    request_retries=10,
    use_ipv6=False,
)


def _upload_to_release(file_path):
    """Upload to GitHub Release, return download URL."""
    tag = f"v{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    t0 = time.time()
    subprocess.run(
        ["gh", "release", "create", tag, file_path,
         "--title", f"File {tag}",
         "--notes", f"Runner (job: {JOB_ID})",
         "--repo", REPO],
        check=True, env={**os.environ, "GH_TOKEN": GH_PAT}
    )
    logger.info(f"Release: {tag} ({time.time()-t0:.1f}s)")

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
    """Scan @ytbdwnmasebot chat for video with matching UUID."""
    logger.info(f"Scanning @{SERVER_BOT_USERNAME} for UUID={JOB_ID}...")
    entity = await client.get_entity(SERVER_BOT_USERNAME)

    for attempt in range(3):
        msgs = await client.get_messages(entity, limit=20)
        for i, m in enumerate(msgs):
            if m.text and JOB_ID in m.text:
                logger.info(f"UUID at msg {m.id} (index {i})")
                for check_idx in [i + 1, i - 1]:
                    if 0 <= check_idx < len(msgs):
                        v = msgs[check_idx]
                        if v.video or (v.document and v.document.mime_type and
                                       v.document.mime_type.startswith("video/")):
                            logger.info(f"Video msg {v.id}, size={v.file.size}")
                            return v
        if attempt < 2:
            await asyncio.sleep(3)
    return None


async def download_fast(video_msg, path):
    """Parallel download — multiple connections downloading different ranges."""
    t0 = time.time()
    size = video_msg.file.size
    logger.info(f"Downloading {size/1e6:.1f} MB with parallel connections...")

    N_WORKERS = 8  # parallel connections
    part_size = 1024 * 1024  # 1MB per chunk (Telegram max)
    total_parts = (size + part_size - 1) // part_size
    parts_per_worker = (total_parts + N_WORKERS - 1) // N_WORKERS

    # Pre-create file with correct size
    with open(path, 'wb') as f:
        f.truncate(size)

    downloaded = [0]
    last_log = [t0]

    def log_progress():
        now = time.time()
        if now - last_log[0] >= 5:
            pct = downloaded[0] * 100 / size
            speed = downloaded[0] / (now - t0) / 1e6
            logger.info(f"  {pct:.0f}% — {downloaded[0]/1e6:.0f}/{size/1e6:.0f} MB — {speed:.1f} MB/s")
            last_log[0] = now

    async def download_range(worker_id, start_part, end_part):
        """Download assigned parts to correct file offset."""
        async for chunk in client.iter_download(
            video_msg,
            offset=start_part * part_size,
            limit=(end_part - start_part) * part_size,
            chunk_size=part_size,
        ):
            offset = start_part * part_size + downloaded_offset[worker_id]
            with open(path, 'r+b') as f:
                f.seek(offset)
                f.write(chunk)
            downloaded[0] += len(chunk)
            downloaded_offset[worker_id] += len(chunk)
            log_progress()

    # Initialize per-worker offset tracking
    downloaded_offset = {i: 0 for i in range(N_WORKERS)}

    # Assign parts to workers
    tasks = []
    for w in range(N_WORKERS):
        start = w * parts_per_worker
        end = min((w + 1) * parts_per_worker, total_parts)
        if start < end:
            tasks.append(download_range(w, start, end))

    # Run all workers in parallel
    await asyncio.gather(*tasks)

    elapsed = time.time() - t0
    speed = size / elapsed / 1e6 if elapsed > 0 else 0
    logger.info(f"Downloaded {size/1e6:.1f} MB in {elapsed:.1f}s ({speed:.1f} MB/s)")
    return size


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
    size = await download_fast(video_msg, tmp)

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
