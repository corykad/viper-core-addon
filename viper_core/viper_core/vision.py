import base64
import json
import logging
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory


LOGGER = logging.getLogger(__name__)
DEFAULT_OPENAI_VISION_MODEL = "gpt-6-astra"
LEGACY_OPENAI_VISION_MODELS = {
    "gpt-5.5",
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
}
DOORBELL_PROMPT = (
    "Describe this doorbell camera image for a blind homeowner in one concise, natural sentence. "
    "Mention people, actions, vehicles, animals, packages, and anything unusual. "
    "If nothing important is visible, say the area appears clear."
)
DOORBELL_VIDEO_PROMPT = (
    "Describe this short live doorbell camera sequence for a blind homeowner. "
    "Mention people, motion, direction of travel, vehicles, animals, packages, and anything that needs attention. "
    "Use two or three complete sentences."
)
AI_DESCRIPTION_STYLE_LABELS = {
    "balanced": "Balanced",
    "fast_security": "Fast security summary",
    "people_movement": "People and movement",
    "packages_deliveries": "Packages and deliveries",
    "detailed_blind": "Detailed for blind user",
    "custom": "Custom",
}
DEFAULT_AI_DESCRIPTION_STYLES = {
    "front_photo": "balanced",
    "back_photo": "balanced",
    "manual_video": "detailed_blind",
    "smart_video": "fast_security",
    "detailed_video": "detailed_blind",
}
AI_DESCRIPTION_STYLE_PROMPTS = {
    "balanced": {
        "photo": DOORBELL_PROMPT,
        "video": DOORBELL_VIDEO_PROMPT,
    },
    "fast_security": {
        "photo": "Give a fast security summary of this doorbell image. Focus on people, vehicles, packages, animals, and anything urgent. Use one concise sentence.",
        "video": "Give a fast security summary of this doorbell video. Focus on people, movement, direction of travel, and anything urgent. Use one or two complete sentences.",
    },
    "people_movement": {
        "photo": "Describe people and movement in this doorbell image. Mention where people are, what they are doing, and whether they appear to approach or leave.",
        "video": "Describe people and movement in this doorbell video. Include direction of travel, what changed, and whether anyone approaches the door. Use complete sentences.",
    },
    "packages_deliveries": {
        "photo": "Check this doorbell image for deliveries, packages, bags, vehicles, and people carrying items. Say whether something appears dropped off or picked up.",
        "video": "Check this doorbell video for deliveries, packages, bags, vehicles, and people carrying items. Say whether something was dropped off or picked up.",
    },
    "detailed_blind": {
        "photo": "Describe this doorbell camera image for a blind homeowner. Include people, vehicles, animals, packages, motion clues, spatial details, and safety concerns. Use one or two complete sentences.",
        "video": "Describe this doorbell video for a blind homeowner. Include people, vehicles, animals, packages, motion, direction of travel, spatial details, and safety concerns. Use two to four complete sentences.",
    },
}


def describe_doorbell(config, ha_client, door):
    provider = _ai_provider(config)
    api_key = _api_key(config, provider)
    if not api_key:
        return ""
    stream_url = _stream_url(config, door)
    if not stream_url or not shutil.which("ffmpeg"):
        LOGGER.warning("Doorbell AI requires an RTSP stream and ffmpeg.")
        return ""
    try:
        live_stream = _prepare_live_stream(config, ha_client, door)
        try:
            frames = _capture_stream_frames(stream_url, 2)
        finally:
            _cleanup_live_stream(ha_client, live_stream)
        if not frames:
            return ""
        prompt = _with_door_context(_photo_prompt(config, door), door)
        if provider == "openai":
            return describe_images_with_openai(
                frames[:2],
                prompt,
                api_key,
                _openai_vision_model(getattr(config, "openai_vision_model", "")),
            )
        return describe_images_with_gemini(
            frames[:2],
            prompt,
            api_key,
            getattr(config, "gemini_vision_model", "gemini-3.5-flash"),
        )
    except Exception as exc:
        LOGGER.warning("Doorbell AI description failed: %s", exc)
        return ""


def describe_live_doorbell(config, ha_client, door, seconds=None, mode="manual"):
    provider = _ai_provider(config)
    api_key = _api_key(config, provider)
    if not api_key:
        return ""
    seconds = _bounded_int(seconds, getattr(config, "doorbell_live_video_seconds", 4), 2, 10)
    prompt = _with_door_context(_video_prompt(config, mode), door)
    stream_url = _stream_url(config, door)
    if stream_url and shutil.which("ffmpeg"):
        live_stream = _prepare_live_stream(config, ha_client, door)
        try:
            if provider == "gemini":
                return describe_stream_video_with_gemini(
                    stream_url,
                    seconds,
                    prompt,
                    api_key,
                    getattr(config, "gemini_vision_model", "gemini-3.5-flash"),
                )
            frames = _capture_stream_frames(stream_url, seconds)
            if frames:
                return describe_images_with_openai(
                    frames,
                    prompt,
                    api_key,
                    _openai_vision_model(getattr(config, "openai_vision_model", "")),
                )
        except Exception as exc:
            LOGGER.warning("Doorbell RTSP analysis failed: %s", exc)
        finally:
            _cleanup_live_stream(ha_client, live_stream)
    LOGGER.warning("Doorbell live analysis requires an RTSP stream and ffmpeg.")
    return ""


def describe_stream_video_with_gemini(stream_url, seconds, prompt, api_key, model):
    with TemporaryDirectory(prefix="viper_core_video_") as temp_dir:
        path = Path(temp_dir) / f"doorbell_{int(time.time() * 1000)}.mp4"
        _capture_stream_video(stream_url, path, seconds)
        video_bytes = path.read_bytes()
        if not video_bytes:
            raise RuntimeError("FFmpeg produced an empty video.")
        try:
            return describe_video_with_gemini(video_bytes, "video/mp4", prompt, api_key, model)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOGGER.debug("Could not delete temporary doorbell video %s", path)


def _capture_stream_video(stream_url, output_path, seconds):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if str(stream_url).lower().startswith("rtsp://"):
        command += ["-rtsp_transport", "tcp"]
    command += [
        "-i",
        str(stream_url),
        "-t",
        str(max(2, min(10, int(seconds or 4)))),
        "-an",
        "-vf",
        "fps=1,scale=640:-2",
        "-c:v",
        "mpeg4",
        "-q:v",
        "5",
        str(output_path),
    ]
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, timeout=max(20, int(seconds or 4) + 12))
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "FFmpeg failed").strip()[:500])
    LOGGER.info("Doorbell FFmpeg mp4 capture took %.2fs.", time.monotonic() - started)


def _capture_stream_frames(stream_url, seconds):
    with TemporaryDirectory(prefix="viper_core_frames_") as temp_dir:
        directory = Path(temp_dir)
        output_pattern = directory / "frame_%02d.jpg"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if str(stream_url).lower().startswith("rtsp://"):
            command += ["-rtsp_transport", "tcp"]
        command += [
            "-i",
            str(stream_url),
            "-t",
            str(max(2, min(10, int(seconds or 4)))),
            "-an",
            "-vf",
            "fps=1,scale=640:-2",
            "-q:v",
            "4",
            str(output_pattern),
        ]
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, text=True, timeout=max(20, int(seconds or 4) + 12))
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "FFmpeg failed").strip()[:500])
        frames = []
        for path in sorted(directory.glob("frame_*.jpg")):
            image_bytes = path.read_bytes()
            if image_bytes:
                frames.append((image_bytes, "image/jpeg"))
        LOGGER.info("Doorbell FFmpeg frame capture took %.2fs and produced %s frame(s).", time.monotonic() - started, len(frames))
        return frames


def describe_image_with_gemini(image_bytes, mime_type, prompt, api_key, model):
    started = time.monotonic()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": _safe_mime(mime_type),
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {"temperature": 0.2},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail[:200]}") from exc
    text = _extract_text(data)
    LOGGER.info("Doorbell Gemini description took %.2fs.", time.monotonic() - started)
    return text or "Activity detected."


def describe_images_with_gemini(frames, prompt, api_key, model):
    started = time.monotonic()
    parts = [{"text": prompt}]
    for image_bytes, mime_type in frames:
        parts.append(
            {
                "inline_data": {
                    "mime_type": _safe_mime(mime_type),
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }
            }
        )
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail[:200]}") from exc
    text = _extract_text(data)
    LOGGER.info("Doorbell live sequence Gemini description took %.2fs.", time.monotonic() - started)
    return text or "Live video did not add useful detail."


def describe_video_with_gemini(video_bytes, mime_type, prompt, api_key, model):
    started = time.monotonic()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "inline_data": {
                            "mime_type": _safe_video_mime(mime_type),
                            "data": base64.b64encode(video_bytes).decode("ascii"),
                        }
                    },
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {"temperature": 0.2},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail[:200]}") from exc
    text = _extract_text(data)
    LOGGER.info("Doorbell mp4 Gemini description took %.2fs.", time.monotonic() - started)
    return text or "Video did not add useful detail."


def describe_image_with_openai(image_bytes, mime_type, prompt, api_key, model):
    return describe_images_with_openai([(image_bytes, mime_type)], prompt, api_key, model)


def describe_images_with_openai(frames, prompt, api_key, model):
    started = time.monotonic()
    content = [{"type": "input_text", "text": prompt}]
    for image_bytes, mime_type in frames:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        content.append({"type": "input_image", "image_url": f"data:{_safe_mime(mime_type)};base64,{encoded}"})
    payload = {
        "model": _openai_vision_model(model),
        "input": [{"role": "user", "content": content}],
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI returned HTTP {exc.code}: {detail[:200]}") from exc
    text = _extract_openai_text(data)
    LOGGER.info("Doorbell OpenAI description took %.2fs.", time.monotonic() - started)
    return text or "Activity detected."


def _stream_url(config, door):
    door = str(door or "front").lower()
    if door.startswith("back"):
        return str(getattr(config, "back_door_stream_url", "") or "").strip()
    return str(getattr(config, "front_door_stream_url", "") or "").strip()


def _live_stream_switch(config, door):
    door = str(door or "front").lower()
    if door.startswith("back"):
        return str(getattr(config, "back_door_live_stream_switch", "") or "").strip()
    return str(getattr(config, "front_door_live_stream_switch", "") or "").strip()


def _prepare_live_stream(config, ha_client, door):
    entity_id = _live_stream_switch(config, door)
    if not entity_id or not getattr(ha_client, "available", lambda: False)():
        return {"entity_id": "", "turn_off_after": False}
    state = "unknown"
    try:
        payload = ha_client.get_state(entity_id) or {}
        state = str(payload.get("state") or "unknown").lower()
    except Exception as exc:
        LOGGER.warning("Could not read doorbell live stream switch %s: %s", entity_id, exc)
    if state == "on":
        return {"entity_id": entity_id, "turn_off_after": False}
    try:
        ha_client.call_service("switch/turn_on", {"entity_id": entity_id})
        LOGGER.info("Turned on doorbell live stream switch %s for video capture.", entity_id)
        time.sleep(2)
        return {"entity_id": entity_id, "turn_off_after": True}
    except Exception as exc:
        LOGGER.warning("Could not turn on doorbell live stream switch %s: %s", entity_id, exc)
        return {"entity_id": entity_id, "turn_off_after": False}


def _cleanup_live_stream(ha_client, live_stream):
    entity_id = str((live_stream or {}).get("entity_id") or "").strip()
    if not entity_id or not (live_stream or {}).get("turn_off_after"):
        return
    try:
        ha_client.call_service("switch/turn_off", {"entity_id": entity_id})
        LOGGER.info("Turned off doorbell live stream switch %s after video capture.", entity_id)
    except Exception as exc:
        LOGGER.warning("Could not turn off doorbell live stream switch %s: %s", entity_id, exc)


def _extract_text(data):
    for candidate in data.get("candidates") or []:
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        text = " ".join(str(part.get("text") or "").strip() for part in parts if part.get("text"))
        if text.strip():
            return text.strip()
    return ""


def _extract_openai_text(data):
    text = str((data or {}).get("output_text") or "").strip()
    if text:
        return text
    for item in (data or {}).get("output") or []:
        for content in item.get("content") or []:
            text = str(content.get("text") or "").strip()
            if text:
                return text
    return ""


def _ai_provider(config):
    provider = str(getattr(config, "ai_provider", "openai") or "openai").strip().lower()
    if provider not in {"gemini", "openai"}:
        provider = "openai"
    if provider == "openai" and not _api_key(config, "openai") and _api_key(config, "gemini"):
        return "gemini"
    if provider == "gemini" and not _api_key(config, "gemini") and _api_key(config, "openai"):
        return "openai"
    return provider


def _openai_vision_model(model):
    model = str(model or "").strip()
    if not model or model in LEGACY_OPENAI_VISION_MODELS:
        return DEFAULT_OPENAI_VISION_MODEL
    return model


def _api_key(config, provider):
    if provider == "openai":
        return str(getattr(config, "openai_api_key", "") or "").strip()
    return str(getattr(config, "gemini_api_key", "") or "").strip()


def _safe_mime(mime_type):
    mime_type = str(mime_type or "").split(";", 1)[0].strip().lower()
    if mime_type in {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}:
        return mime_type
    return "image/jpeg"


def _safe_video_mime(mime_type):
    mime_type = str(mime_type or "").split(";", 1)[0].strip().lower()
    if mime_type in {"video/mp4", "video/mpeg", "video/quicktime", "video/webm"}:
        return mime_type
    return "video/mp4"


def _photo_prompt(config, door):
    job = "back_photo" if str(door or "").lower().startswith("back") else "front_photo"
    legacy = getattr(config, "back_door_photo_prompt" if job == "back_photo" else "front_door_photo_prompt", "")
    return _prompt_for_job(config, job, legacy=legacy)


def _video_prompt(config, mode="manual"):
    job = {
        "smart": "smart_video",
        "detailed": "detailed_video",
    }.get(str(mode or "manual").lower(), "manual_video")
    return _prompt_for_job(config, job, legacy=getattr(config, "doorbell_video_prompt", ""))


def _prompt_for_job(config, job, legacy=""):
    styles = getattr(config, "ai_description_styles", {}) or {}
    custom = getattr(config, "ai_custom_descriptions", {}) or {}
    style = str(styles.get(job) or DEFAULT_AI_DESCRIPTION_STYLES.get(job) or "balanced").strip().lower()
    if style == "custom":
        custom_text = str(custom.get(job) or legacy or "").strip()
        if custom_text:
            return custom_text
    prompt_type = "video" if str(job).endswith("_video") else "photo"
    return AI_DESCRIPTION_STYLE_PROMPTS.get(style, AI_DESCRIPTION_STYLE_PROMPTS["balanced"])[prompt_type]


def _with_door_context(prompt, door):
    door_label = "back door" if str(door or "").strip().lower().startswith("back") else "front door"
    other_label = "front door" if door_label == "back door" else "back door"
    return (
        f"This camera is the {door_label}. Refer to this location as the {door_label}; "
        f"do not call it the {other_label}. {str(prompt or '').strip()}"
    ).strip()


def _bounded_int(value, fallback, minimum, maximum):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        try:
            parsed = int(float(fallback))
        except (TypeError, ValueError):
            parsed = minimum
    return max(minimum, min(maximum, parsed))


def description_needs_live_followup(text):
    lowered = str(text or "").strip().lower()
    if not lowered:
        return True
    markers = [
        "unclear",
        "blurry",
        "distorted",
        "corrupt",
        "cannot tell",
        "can't tell",
        "hard to see",
        "not clear",
        "activity detected",
        "appears clear",
    ]
    return any(marker in lowered for marker in markers)
