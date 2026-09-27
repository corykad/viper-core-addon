import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path


LOGGER = logging.getLogger(__name__)
TTS_DIR = Path("/data/tts")
MEDIA_TTS_DIR = Path("/media/viper_core_tts")
DEFAULT_GEMINI_TTS_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_TTS_DELETE_DELAY_SECONDS = 300
ALLOWED_SPEEDS = {"slow", "normal", "fast", "very_fast"}
GEMINI_TTS_VOICES = {
    "Zephyr": "Bright",
    "Puck": "Upbeat",
    "Charon": "Informative",
    "Kore": "Firm",
    "Fenrir": "Excitable",
    "Leda": "Youthful",
    "Orus": "Firm",
    "Aoede": "Breezy",
    "Callirrhoe": "Easy-going",
    "Autonoe": "Bright",
    "Enceladus": "Breathy",
    "Iapetus": "Clear",
    "Umbriel": "Easy-going",
    "Algieba": "Smooth",
    "Despina": "Smooth",
    "Erinome": "Clear",
    "Algenib": "Gravelly",
    "Rasalgethi": "Informative",
    "Laomedeia": "Upbeat",
    "Achernar": "Soft",
    "Alnilam": "Firm",
    "Schedar": "Even",
    "Gacrux": "Mature",
    "Pulcherrima": "Forward",
    "Achird": "Friendly",
    "Zubenelgenubi": "Casual",
    "Vindemiatrix": "Gentle",
    "Sadachbia": "Lively",
    "Sadaltager": "Knowledgeable",
    "Sulafat": "Warm",
}
OPENAI_TTS_VOICES = {
    "alloy": "Neutral",
    "ash": "Calm",
    "ballad": "Expressive",
    "coral": "Warm",
    "echo": "Clear",
    "fable": "Storytelling",
    "nova": "Bright",
    "onyx": "Deep",
    "sage": "Smooth",
    "shimmer": "Light",
    "verse": "Natural",
    "marin": "Conversational",
    "cedar": "Warm deep",
}


def generate_gemini_tts(message, settings):
    api_key = str(settings.get("gemini_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("Gemini API key is not configured.")
    message = str(message or "").strip()
    if not message:
        raise RuntimeError("No message was provided for Gemini TTS.")
    model = str(settings.get("gemini_tts_model") or DEFAULT_GEMINI_TTS_MODEL).strip()
    voice = str(settings.get("gemini_tts_voice") or "Sulafat").strip()
    speed = _clean_speed(settings.get("gemini_tts_speed") or "normal")
    style = str(settings.get("gemini_tts_style") or "warm, clear, friendly").strip()
    filename = f"gemini_{int(time.time() * 1000)}.wav"
    data, mime_type = _request_gemini_tts(api_key, model, voice, _tts_prompt(message, style, speed))
    path = _write_audio(filename, data, mime_type)
    _copy_to_media(path)
    return {
        "filename": filename,
        "path": path,
        "media_source": f"media-source://media_source/local/viper_core_tts/{urllib.parse.quote(filename, safe='')}",
        "mime_type": "audio/wav",
    }


def generate_openai_tts(message, settings):
    api_key = str(settings.get("openai_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("OpenAI API key is not configured.")
    message = str(message or "").strip()
    if not message:
        raise RuntimeError("No message was provided for OpenAI TTS.")
    model = str(settings.get("openai_tts_model") or "gpt-4o-mini-tts").strip()
    voice = str(settings.get("openai_tts_voice") or "coral").strip().lower()
    if voice not in OPENAI_TTS_VOICES:
        voice = "coral"
    filename = f"openai_{int(time.time() * 1000)}.mp3"
    payload = {
        "model": model,
        "voice": voice,
        "input": message,
        "response_format": "mp3",
        "speed": _openai_speed(settings.get("openai_tts_speed") or settings.get("gemini_tts_speed") or "normal"),
    }
    instructions = str(settings.get("openai_tts_instructions") or settings.get("gemini_tts_style") or "").strip()
    if instructions:
        payload["instructions"] = instructions
    request = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI TTS returned HTTP {exc.code}: {detail[:180]}") from exc
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TTS_DIR / filename
    path.write_bytes(audio)
    _copy_to_media(path)
    return {
        "filename": filename,
        "path": path,
        "media_source": f"media-source://media_source/local/viper_core_tts/{urllib.parse.quote(filename, safe='')}",
        "mime_type": "audio/mpeg",
    }


def media_url(filename, external_base_url):
    base = str(external_base_url or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/tts/{urllib.parse.quote(str(filename or ''), safe='')}"


def tts_path(filename):
    filename = Path(str(filename or "").replace("\\", "/")).name
    path = TTS_DIR / filename
    try:
        if path.exists() and path.is_file():
            return path
    except OSError:
        return None
    return None


def cleanup_old_tts(max_age_seconds=86400):
    now = time.time()
    for directory in (TTS_DIR, MEDIA_TTS_DIR):
        try:
            if not directory.exists():
                continue
            for item in directory.iterdir():
                if item.is_file() and now - item.stat().st_mtime > max_age_seconds:
                    item.unlink()
        except OSError as exc:
            LOGGER.debug("Could not clean old TTS files from %s: %s", directory, exc)


def delete_tts_file(filename):
    filename = Path(str(filename or "").replace("\\", "/")).name
    if not filename:
        return
    for directory in (TTS_DIR, MEDIA_TTS_DIR):
        path = directory / filename
        try:
            if path.exists() and path.is_file():
                path.unlink()
                LOGGER.info("Deleted temporary TTS file %s.", path)
        except OSError as exc:
            LOGGER.warning("Could not delete temporary TTS file %s: %s", path, exc)


def delete_tts_later(filename, delay_seconds=DEFAULT_TTS_DELETE_DELAY_SECONDS):
    filename = Path(str(filename or "").replace("\\", "/")).name
    if not filename:
        return None
    delay = max(1, int(delay_seconds or DEFAULT_TTS_DELETE_DELAY_SECONDS))
    timer = threading.Timer(delay, delete_tts_file, args=(filename,))
    timer.daemon = True
    timer.start()
    LOGGER.info("Scheduled temporary TTS file %s for deletion in %s seconds.", filename, delay)
    return timer


def _request_gemini_tts(api_key, model, voice, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(model, safe='')}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice},
                },
            },
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini TTS returned HTTP {exc.code}: {detail[:180]}") from exc
    data = json.loads(body)
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data") or {}
        encoded = inline.get("data")
        if encoded:
            return base64.b64decode(encoded), inline.get("mimeType") or inline.get("mime_type") or "audio/wav"
    raise RuntimeError("Gemini TTS returned no audio data.")


def _write_audio(filename, data, mime_type):
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TTS_DIR / filename
    mime = str(mime_type or "").lower()
    if "wav" in mime:
        path.write_bytes(data)
    else:
        _write_pcm_wav(path, data, _pcm_rate(mime))
    return path


def _write_pcm_wav(path, data, rate):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(data)


def _copy_to_media(path):
    try:
        if not (MEDIA_TTS_DIR.exists() or MEDIA_TTS_DIR.parent.exists()):
            return
        MEDIA_TTS_DIR.mkdir(parents=True, exist_ok=True)
        (MEDIA_TTS_DIR / path.name).write_bytes(path.read_bytes())
    except OSError as exc:
        LOGGER.warning("Could not copy TTS file to Home Assistant media: %s", exc)


def _pcm_rate(mime_type):
    text = str(mime_type or "").lower()
    for marker in ("rate=", "rate:"):
        if marker in text:
            value = text.split(marker, 1)[1].split(";", 1)[0].strip()
            try:
                return int(value)
            except ValueError:
                pass
    return 24000


def _tts_prompt(message, style, speed):
    instructions = {
        "slow": "Speak slowly and clearly, with extra space between phrases.",
        "normal": "Speak very quickly and clearly, with crisp articulation. Do not drag out words.",
        "fast": "Speak extremely fast and clearly, like a concise urgent home alert. Keep the pace moving and do not drag out words.",
        "very_fast": "Speak as fast as you can while staying understandable. Use clipped, efficient phrasing and do not drag out words.",
    }[_clean_speed(speed)]
    return (
        "You are Viper Vision, a home assistant voice. "
        f"Delivery style: {style or 'warm, clear, friendly'}. "
        f"{instructions} "
        f"Say exactly this message and do not add extra words: {message}"
    )


def _clean_speed(speed):
    speed = str(speed or "normal").strip().lower()
    return speed if speed in ALLOWED_SPEEDS else "normal"


def _openai_speed(speed):
    return {"slow": 1.0, "normal": 1.75, "fast": 2.0, "very_fast": 2.5}.get(_clean_speed(speed), 1.75)
