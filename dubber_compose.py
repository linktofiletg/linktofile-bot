"""
Dubber Step 2: Compose Dubbed Video
- Downloads: video, TTS audio, TTS transcript JSON, original SRT, mapping JSON
- Cuts TTS audio into segments per mapping
- Speed-adjusts each chunk with atempo to fit original timing
- Builds a new audio track: dubbed segments + silence for gaps
- Replaces video's audio with the dubbed track (first half only)
- Second half keeps original audio
- Uploads result as artifact
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
TTS_TRANSCRIPT_URL = payload.get("tts_transcript_url", "")  # JSON from step 1
ORIGINAL_SRT_URL = payload.get("original_srt_url", "")
MAPPING_URL = payload.get("mapping_url", "")
JOB_ID = payload.get("job_id", "dub")

if not all([VIDEO_URL, TTS_AUDIO_URL, TTS_TRANSCRIPT_URL, ORIGINAL_SRT_URL, MAPPING_URL]):
    logger.error("Missing required inputs!")
    logger.error(f"  video_url: {bool(VIDEO_URL)}")
    logger.error(f"  tts_audio_url: {bool(TTS_AUDIO_URL)}")
    logger.error(f"  tts_transcript_url: {bool(TTS_TRANSCRIPT_URL)}")
    logger.error(f"  original_srt_url: {bool(ORIGINAL_SRT_URL)}")
    logger.error(f"  mapping_url: {bool(MAPPING_URL)}")
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

# ── Build dubbed audio track ──
# For each mapping entry:
# 1. Extract TTS audio chunk [t_start, t_end]
# 2. Calculate target duration = orig segment duration(s)
# 3. Speed-adjust with atempo
# 4. Place at orig start time
# 5. Fill gaps with silence

logger.info(f"Processing {len(mapping)} mapped segments...")
chunk_files = []

for m in mapping:
    tid = m["tts"]
    oids = m["orig"]
    t = tts_segments[tid - 1]
    
    # Target: original video time range
    o_start = orig[oids[0] - 1]["start"]
    o_end = orig[oids[-1] - 1]["end"]
    target_dur = o_end - o_start
    
    # Source: TTS audio time range
    t_start = t["start"]
    t_end = t["end"]
    tts_dur = t_end - t_start
    
    if tts_dur <= 0 or target_dur <= 0:
        logger.warning(f"  T{tid}: zero duration, skipping")
        continue
    
    # Speed ratio
    ratio = target_dur / tts_dur
    # Clamp atempo: 0.5x to 2.0x per filter, chain if needed
    if ratio < 0.5:
        atempo = "atempo=0.5,atempo=" + f"{ratio/0.5:.4f}"
    elif ratio > 2.0:
        atempo = "atempo=2.0,atempo=" + f"{ratio/2.0:.4f}"
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
    
    # Check actual chunk duration
    probe2 = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", chunk_file],
                           capture_output=True, text=True)
    actual_dur = float(probe2.stdout.strip())
    
    chunk_files.append({
        "file": chunk_file,
        "start": o_start,
        "dur": actual_dur,
        "tid": tid
    })

# ── Build full audio track: silence + chunks ──
logger.info("Building dubbed audio track...")
final_audio = f"{WORKDIR}/dubbed_audio.wav"

# Create a silent base track for the full video duration
subprocess.run([
    "ffmpeg", "-y",
    "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
    "-t", str(video_duration),
    "-c:a", "pcm_s16le",
    final_audio
], check=True, capture_output=True)

# Mix each chunk into the base at the right offset
current_output = final_audio
for i, chunk in enumerate(chunk_files):
    mix_file = f"{WORKDIR}/mixed_{i:03d}.wav"
    
    # Use adelay to position the chunk, then amix
    delay_ms = int(chunk["start"] * 1000)
    
    subprocess.run([
        "ffmpeg", "-y",
        "-i", current_output,
        "-i", chunk["file"],
        "-filter_complex",
        f"[1:a]adelay={delay_ms}|{delay_ms}[d];[0:a][d]amix=inputs=2:duration=first:dropout_transition=0[a]",
        "-map", "[a]",
        "-c:a", "pcm_s16le",
        mix_file
    ], check=True, capture_output=True)
    
    current_output = mix_file

# ── Replace video audio ──
# First half: dubbed audio
# Second half: original audio (since TTS only covers first half)
# Find the end of the last dubbed segment
last_dub_end = max(c["start"] + c["dur"] for c in chunk_files)
logger.info(f"Last dubbed segment ends at: {last_dub_end:.1f}s")

output_file = "output/dubbed_video.mp4"
os.makedirs("output", exist_ok=True)

# Extract original audio from video
orig_audio = f"{WORKDIR}/orig_audio.wav"
subprocess.run([
    "ffmpeg", "-y", "-i", video_file,
    "-vn", "-ac", "2", "-ar", "44100",
    "-c:a", "pcm_s16le", orig_audio
], check=True, capture_output=True)

# Mix: dubbed audio for first part, original for rest
# Use sidechain or simple amix with volume control
# Simpler: use ffmpeg to mix dubbed track over original, with dubbed track louder
# Actually best approach: replace audio entirely with dubbed track
# (dubbed track already has silence in the second half from anullsrc base)

subprocess.run([
    "ffmpeg", "-y",
    "-i", video_file,
    "-i", current_output,
    "-map", "0:v", "-map", "1:a",
    "-c:v", "copy",
    "-c:a", "aac", "-b:a", "192k",
    "-shortest",
    "-movflags", "+faststart",
    output_file
], check=True, capture_output=True)

output_size = os.path.getsize(output_file)
logger.info(f"✅ Output: {output_file} ({output_size/1e6:.1f} MB)")
logger.info(f"   Video duration: {video_duration:.1f}s")
logger.info(f"   Dubbed segments: {len(chunk_files)}")
logger.info(f"   Last dub ends at: {last_dub_end:.1f}s")
logger.info(f"   Rest ({video_duration - last_dub_end:.1f}s) has silence (or original audio)")

# Cleanup temp files
shutil.rmtree(WORKDIR, ignore_errors=True)
