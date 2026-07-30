"""
Dubber Step 1: TTS Audio → Timestamped SRT
- Takes a TTS-generated Persian audio file
- Runs Whisper on it to get accurate timestamps per segment
- Outputs a Whisper JSON transcript with per-segment timing
"""
import os
import sys
import json
import time
import logging
import subprocess

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Input: audio file URL from dispatch payload
DISPATCH_PAYLOAD = os.environ.get("DISPATCH_PAYLOAD", "{}")
try:
    payload = json.loads(DISPATCH_PAYLOAD)
except Exception:
    payload = {}

AUDIO_URL = payload.get("audio_url", "")
JOB_ID = payload.get("job_id", "")

if not AUDIO_URL:
    logger.error("No audio_url in payload!")
    sys.exit(1)

# Download audio from VPS
import urllib.request
local_audio = f"/tmp/tts_audio_{JOB_ID}.ogg"
logger.info(f"Downloading audio from {AUDIO_URL}...")
try:
    urllib.request.urlretrieve(AUDIO_URL, local_audio)
except Exception as e:
    logger.error(f"Download failed: {e}")
    sys.exit(1)
size = os.path.getsize(local_audio)
logger.info(f"Downloaded {size/1e6:.1f} MB")

# Convert to WAV for Whisper
wav_file = f"/tmp/tts_audio_{JOB_ID}.wav"
subprocess.run(["ffmpeg", "-y", "-i", local_audio, "-ac", "1", "-ar", "16000", wav_file],
               check=True, capture_output=True)
os.remove(local_audio)

# Run faster-whisper with language='fa' for Persian TTS
logger.info("Running Whisper (model=small, language=fa)...")
t0 = time.time()

from faster_whisper import WhisperModel
model = WhisperModel("small", device="cpu", compute_type="int8")
segments, info = model.transcribe(wav_file, language="fa", beam_size=5, word_timestamps=True)

# Collect segments
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

# Save transcript
output = {
    "job_id": JOB_ID,
    "duration": info.duration,
    "language": info.language,
    "segments": result_segments
}

with open(f"/tmp/tts_transcript_{JOB_ID}.json", "w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

logger.info(f"Transcript saved: {len(result_segments)} segments")

# Also save as SRT for reference
srt_lines = []
for seg in result_segments:
    start = seg["start"]
    end = seg["end"]
    # Format to SRT time
    def fmt_time(s):
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = s % 60
        return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".", ",")
    srt_lines.append(f"{seg['id'] + 1}")
    srt_lines.append(f"{fmt_time(start)} --> {fmt_time(end)}")
    srt_lines.append(seg["text"])
    srt_lines.append("")

with open(f"/tmp/tts_transcript_{JOB_ID}.srt", "w", encoding="utf-8") as f:
    f.write("\n".join(srt_lines))

logger.info(f"SRT saved: {len(result_segments)} entries")

# Output metadata for next step
print(json.dumps({
    "job_id": JOB_ID,
    "segments": len(result_segments),
    "duration": info.duration,
    "transcript_json": f"/tmp/tts_transcript_{JOB_ID}.json"
}))
os.remove(wav_file)
