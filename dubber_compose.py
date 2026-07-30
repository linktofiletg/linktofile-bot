"""
Dubber Step 2: Compose Dubbed Video
- Downloads: video, TTS audio, TTS transcript JSON, original SRT, mapping JSON
- Cuts TTS audio into segments per mapping
- Speed-adjusts each chunk with atempo to fit original timing
- Pads each chunk with silence to full video duration
- Mixes all padded chunks together (normalize=0 = keep full volume)
- Replaces video's audio with the dubbed track
"""
import os
import sys
import json
import logging
import subprocess
import urllib.request
import shutil

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Parse payload ──
DISPATCH_PAYLOAD = os.environ.get("DISPATCH_PAYLOAD", "{}")
try:
    payload = json.loads(DISPATCH_PAYLOAD)
except Exception:
    payload = {}

VIDEO_URL = payload.get("video_url", "")
TTS_AUDIO_URL = payload.get("tts_audio_url", "")
TTS_TRANSCRIPT_URL = payload.get("tts_transcript_url", "")
ORIGINAL_SRT_URL = payload.get("original_srt_url", "")
MAPPING_URL = payload.get("mapping_url", "")
JOB_ID = payload.get("job_id", "dub")

if not all([VIDEO_URL, TTS_AUDIO_URL, TTS_TRANSCRIPT_URL, ORIGINAL_SRT_URL, MAPPING_URL]):
    logger.error("Missing required inputs!")
    sys.exit(1)

WORKDIR = f"/tmp/dub_{JOB_ID}"
os.makedirs(WORKDIR, exist_ok=True)

# ── Download files ──
def download(url, path):
    logger.info(f"Downloading {url}...")
    urllib.request.urlretrieve(url, path)
    logger.info(f"  → {os.path.getsize(path)/1e6:.1f} MB")

video_file = f"{WORKDIR}/video.mp4"
tts_audio_file = f"{WORKDIR}/tts_audio.wav"
tts_json_file = f"{WORKDIR}/tts_transcript.json"
orig_srt_file = f"{WORKDIR}/orig.srt"
mapping_file = f"{WORKDIR}/mapping.json"

download(VIDEO_URL, video_file)
download(TTS_AUDIO_URL, tts_audio_file)
download(TTS_TRANSCRIPT_URL, tts_json_file)
download(ORIGINAL_SRT_URL, orig_srt_file)
download(MAPPING_URL, mapping_file)

# ── Parse files ──
with open(tts_json_file, encoding="utf-8") as f:
    tts_data = json.load(f)
with open(mapping_file, encoding="utf-8") as f:
    mapping = json.load(f)

def parse_srt(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    i = 0
    while i < len(lines):
        if lines[i].strip().isdigit():
            idx = int(lines[i]); i += 1
            time_line = lines[i].strip(); i += 1
            text = ""
            while i < len(lines) and lines[i].strip():
                text += lines[i].strip() + " "; i += 1
            i += 1
            parts = time_line.split(" --> ")
            def srt_time(t):
                t = t.replace(",", "."); h, m, s = t.split(":")
                return int(h)*3600 + int(m)*60 + float(s)
            entries.append({"id": idx, "start": srt_time(parts[0]),
                           "end": srt_time(parts[1]), "text": text.strip()})
    return entries

orig = parse_srt(orig_srt_file)
tts_segments = tts_data["segments"]

# ── Get video duration ──
probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                       "-of", "default=noprint_wrappers=1:nokey=1", video_file],
                      capture_output=True, text=True)
video_duration = float(probe.stdout.strip())
logger.info(f"Video duration: {video_duration:.1f}s")

# ── Process each mapping: cut TTS chunk + speed-adjust ──
logger.info(f"Processing {len(mapping)} mapped segments...")
padded_files = []

for m in mapping:
    tid = m["tts"]
    oids = m["orig"]
    t = tts_segments[tid - 1]

    o_start = orig[oids[0] - 1]["start"]
    o_end = orig[oids[-1] - 1]["end"]
    target_dur = o_end - o_start

    t_start = t["start"]
    t_end = t["end"]
    tts_dur = t_end - t_start

    if tts_dur <= 0 or target_dur <= 0:
        logger.warning(f"  T{tid}: zero duration, skipping")
        continue

    # Speed ratio (atempo: 0.5x-2.0x per filter, chain if needed)
    ratio = target_dur / tts_dur
    if ratio < 0.5:
        atempo = f"atempo=0.5,atempo={ratio/0.5:.4f}"
    elif ratio > 2.0:
        atempo = f"atempo=2.0,atempo={ratio/2.0:.4f}"
    else:
        atempo = f"atempo={ratio:.4f}"

    logger.info(f"  T{tid:2d} → O{oids} | {tts_dur:.1f}s→{target_dur:.1f}s | {ratio:.2f}x | {t['text'][:40]}")

    # Extract and speed-adjust chunk
    chunk_file = f"{WORKDIR}/chunk_{tid:03d}.wav"
    subprocess.run([
        "ffmpeg", "-y", "-i", tts_audio_file,
        "-ss", str(t_start), "-to", str(t_end),
        "-af", atempo,
        "-ar", "44100", "-ac", "2",
        chunk_file
    ], check=True, capture_output=True)

    # Pad chunk with silence to full video duration (place at o_start offset)
    padded_file = f"{WORKDIR}/padded_{tid:03d}.wav"
    delay_ms = int(o_start * 1000)
    subprocess.run([
        "ffmpeg", "-y",
        "-i", chunk_file,
        "-af", f"adelay={delay_ms}|{delay_ms},apad=whole_dur={video_duration}",
        "-t", str(video_duration),
        "-ar", "44100", "-ac", "2",
        "-c:a", "pcm_s16le",
        padded_file
    ], check=True, capture_output=True)

    padded_files.append(padded_file)
    os.remove(chunk_file)

# ── Mix all padded tracks together (normalize=0 = keep full volume) ──
logger.info(f"Mixing {len(padded_files)} padded tracks...")

if len(padded_files) == 1:
    shutil.copy(padded_files[0], f"{WORKDIR}/dubbed_audio.wav")
elif len(padded_files) > 1:
    mix_inputs = "".join(f"[{j}:a]" for j in range(len(padded_files)))
    filter_complex = f"{mix_inputs}amix=inputs={len(padded_files)}:normalize=0[a]"

    args = ["ffmpeg", "-y"]
    for pf in padded_files:
        args += ["-i", pf]
    args += ["-filter_complex", filter_complex, "-map", "[a]",
             "-c:a", "pcm_s16le", f"{WORKDIR}/dubbed_audio.wav"]
    subprocess.run(args, check=True, capture_output=True)

# Clean up padded files
for pf in padded_files:
    try:
        os.remove(pf)
    except:
        pass

dubbed_audio = f"{WORKDIR}/dubbed_audio.wav"

# ── Replace video audio ──
output_file = "output/dubbed_video.mp4"
os.makedirs("output", exist_ok=True)

# Keep original audio for the part after dubbing ends
# Find where dubbing ends
last_dub_end = max(orig[m["orig"][-1] - 1]["end"] for m in mapping)
logger.info(f"Last dub ends at: {last_dub_end:.1f}s, video: {video_duration:.1f}s")

# Extract original audio from video
orig_audio = f"{WORKDIR}/orig_audio.wav"
subprocess.run([
    "ffmpeg", "-y", "-i", video_file,
    "-vn", "-ac", "2", "-ar", "44100",
    "-c:a", "pcm_s16le", orig_audio
], check=True, capture_output=True)

# Mix: dubbed audio (full volume) + original audio (muted during dub, full after)
# Use sidechaincompress or simpler: just use dubbed audio entirely
# The dubbed track already has silence in second half (from apad)

subprocess.run([
    "ffmpeg", "-y",
    "-i", video_file,
    "-i", dubbed_audio,
    "-map", "0:v", "-map", "1:a",
    "-c:v", "copy",
    "-c:a", "aac", "-b:a", "192k",
    "-shortest",
    "-movflags", "+faststart",
    output_file
], check=True, capture_output=True)

output_size = os.path.getsize(output_file)
logger.info(f"✅ Output: {output_file} ({output_size/1e6:.1f} MB)")

# Verify volume
vol_check = subprocess.run([
    "ffmpeg", "-i", output_file,
    "-af", "volumedetect", "-f", "null", "-"
], capture_output=True, text=True, timeout=30)
for line in vol_check.stderr.split("\n"):
    if "Volume" in line or "mean" in line or "max" in line:
        logger.info(f"  {line.strip()}")

# Cleanup
shutil.rmtree(WORKDIR, ignore_errors=True)
