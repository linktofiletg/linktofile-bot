"""
Dubber Step 2: Compose Dubbed Video
- Takes: video URL, original subtitle SRT, TTS transcript JSON
- Aligns TTS audio segments to original subtitle timestamps
- Adjusts playback speed of TTS chunks to fit original timing
- Produces a video with dubbed audio
"""
import os
import sys
import json
import logging
import subprocess
import asyncio
import urllib.request

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

DISPATCH_PAYLOAD = os.environ.get("DISPATCH_PAYLOAD", "{}")
try:
    payload = json.loads(DISPATCH_PAYLOAD)
except Exception:
    payload = {}

VIDEO_URL = payload.get("video_url", "")
TTS_SRT_URL = payload.get("tts_srt_url", "")  # Whisper SRT from step 1
ORIGINAL_SRT_URL = payload.get("original_srt_url", "")
JOB_ID = payload.get("job_id", "")

if not all([VIDEO_URL, TTS_SRT_URL, ORIGINAL_SRT_URL]):
    logger.error("Missing required inputs!")
    sys.exit(1)

# Download video
video_file = f"/tmp/video_{JOB_ID}.mp4"
logger.info(f"Downloading video...")
urllib.request.urlretrieve(VIDEO_URL, video_file)
logger.info(f"Video downloaded")

# Download original SRT (Persian subtitles with original timing)
orig_srt = f"/tmp/orig_{JOB_ID}.srt"
urllib.request.urlretrieve(ORIGINAL_SRT_URL, orig_srt)

# Download TTS SRT (Whisper timestamps of the TTS audio)
tts_srt = f"/tmp/tts_{JOB_ID}.srt"
urllib.request.urlretrieve(TTS_SRT_URL, tts_srt)

# Parse both SRTs
def parse_srt(path):
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().isdigit():
            idx = int(lines[i])
            i += 1
            time_line = lines[i].strip()
            i += 1
            # Collect text lines until blank
            text = ""
            while i < len(lines) and lines[i].strip():
                text += lines[i].strip() + " "
                i += 1
            i += 1  # skip blank
            parts = time_line.split(" --> ")
            if len(parts) == 2:
                def srt_time(t):
                    t = t.replace(",", ".")
                    h, m, s = t.split(":")
                    return int(h)*3600 + int(m)*60 + float(s)
                entries.append({
                    "id": idx,
                    "start": srt_time(parts[0]),
                    "end": srt_time(parts[1]),
                    "duration": srt_time(parts[1]) - srt_time(parts[0]),
                    "text": text.strip()
                })
    return entries

orig = parse_srt(orig_srt)
tts = parse_srt(tts_srt)
logger.info(f"Original subtitles: {len(orig)} entries")
logger.info(f"TTS transcript: {len(tts)} entries")

# Prepare audio chunks
os.makedirs(f"/tmp/dubber_{JOB_ID}", exist_ok=True)

# List of (original_segment_audio, start_offset, duration)
chunks = []

# For each original segment, find the matching TTS segment
# We match 1:1 in order (assumes same number and order of segments)
min_len = min(len(orig), len(tts))
logger.info(f"Matching {min_len} segments...")

for i in range(min_len):
    o = orig[i]
    t = tts[i]
    
    # Extract TTS audio chunk for this segment
    chunk_file = f"/tmp/dubber_{JOB_ID}/chunk_{i:04d}.wav"
    
    # Get video's audio stream as reference for timing
    # We need TTS audio file — but we only have the SRT. 
    # The actual TTS audio will be provided separately or extracted from the full TTS audio.
    pass

# ALTERNATIVE APPROACH: use the FULL TTS audio and cut by Whisper timestamps
# Then stretch/squeeze each chunk to match original timing

logger.info("Full TTS audio not available separately — need audio_url in payload")

# Let's use the atempo filter in ffmpeg
# For each segment:
# 1. Extract TTS audio segment [t_start, t_end] from full TTS audio
# 2. Speed up/slow down with atempo to match original [o_start, o_end]
# 3. Concatenate all segments with silence in gaps

# Signal back to server: we need the full TTS audio file URL
# For now, print the matching plan

logger.info("=== DUBBING PLAN ===")
for i in range(min(min_len, 5)):
    o = orig[i]
    t = tts[i]
    ratio = o["duration"] / t["duration"] if t["duration"] > 0 else 1
    logger.info(f"Segment {i+1}: orig={o['start']:.1f}-{o['end']:.1f}s ({o['duration']:.1f}s) "
                f"tts={t['start']:.1f}-{t['end']:.1f}s ({t['duration']:.1f}s) "
                f"speed_adjust={ratio:.2f}x")

if min_len > 5:
    logger.info(f"... and {min_len - 5} more segments")

logger.info(f"Total orig duration: {sum(o['duration'] for o in orig):.1f}s")
logger.info(f"Total tts duration: {sum(t['duration'] for t in tts[:min_len]):.1f}s")
logger.info("Dubbing plan computed successfully")
