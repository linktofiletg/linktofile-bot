"""
Dubber Step 1: TTS Audio → Timestamped SRT via Whisper large-v3
- Downloads TTS audio from VPS HTTP
- Runs Whisper large-v3 (best for Persian) with word timestamps
- Uploads SRT + JSON as artifact
"""
import os
import sys
import json
import time
import logging
import subprocess
import urllib.request

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Parse payload
DISPATCH_PAYLOAD = os.environ.get("DISPATCH_PAYLOAD", "{}")
try:
    payload = json.loads(DISPATCH_PAYLOAD)
except Exception:
    payload = {}

AUDIO_URL = payload.get("audio_url", "")
JOB_ID = payload.get("job_id", "dub")

if not AUDIO_URL:
    logger.error("No audio_url in payload!")
    sys.exit(1)

# Download audio
local_audio = f"/tmp/tts_audio_{JOB_ID}"
logger.info(f"Downloading audio from {AUDIO_URL}...")
urllib.request.urlretrieve(AUDIO_URL, local_audio)
size = os.path.getsize(local_audio)
logger.info(f"Downloaded {size/1e6:.1f} MB")

# Convert to 16kHz mono WAV for Whisper
wav_file = f"/tmp/tts_audio_{JOB_ID}.wav"
logger.info("Converting to WAV 16kHz mono...")
subprocess.run(
    ["ffmpeg", "-y", "-i", local_audio, "-ac", "1", "-ar", "16000", wav_file],
    check=True, capture_output=True
)
os.remove(local_audio)
logger.info("WAV ready")

# Run Whisper large-v3
logger.info("Loading Whisper large-v3 (this takes ~30s to download model)...")
t0 = time.time()

from faster_whisper import WhisperModel
model = WhisperModel("large-v3", device="cpu", compute_type="int8")

logger.info("Transcribing with language=fa, word_timestamps=True...")
segments, info = model.transcribe(
    wav_file,
    language="fa",
    beam_size=5,
    word_timestamps=True,
    vad_filter=True
)

# Collect
result_segments = []
for seg in segments:
    words = []
    if seg.words:
        for w in seg.words:
            words.append({"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3)})
    result_segments.append({
        "id": len(result_segments),
        "start": round(seg.start, 3),
        "end": round(seg.end, 3),
        "text": seg.text.strip(),
        "words": words
    })

elapsed = time.time() - t0
logger.info(f"Whisper done: {len(result_segments)} segments in {elapsed:.1f}s")
logger.info(f"Detected language: {info.language} ({info.language_probability:.2f})")

os.remove(wav_file)

# Save JSON transcript
output = {
    "job_id": JOB_ID,
    "duration": round(info.duration, 3),
    "language": info.language,
    "model": "large-v3",
    "segments": result_segments
}

json_path = f"output/tts_transcript_{JOB_ID}.json"
os.makedirs("output", exist_ok=True)
with open(json_path, "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
logger.info(f"JSON saved: {json_path}")

# Save SRT
def fmt_time(s):
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".", ",")

srt_path = f"output/tts_transcript_{JOB_ID}.srt"
srt_lines = []
for seg in result_segments:
    srt_lines.append(str(seg["id"] + 1))
    srt_lines.append(f"{fmt_time(seg['start'])} --> {fmt_time(seg['end'])}")
    srt_lines.append(seg["text"])
    srt_lines.append("")

with open(srt_path, "w", encoding="utf-8") as f:
    f.write("\n".join(srt_lines))
logger.info(f"SRT saved: {srt_path} ({len(result_segments)} entries)")

# Print summary for log
logger.info("=== TRANSCRIPT SUMMARY ===")
for s in result_segments[:10]:
    logger.info(f"  [{s['start']:.1f}s - {s['end']:.1f}s] {s['text'][:60]}")
if len(result_segments) > 10:
    logger.info(f"  ... and {len(result_segments) - 10} more")
logger.info(f"Total: {len(result_segments)} segments, {info.duration:.1f}s duration")
