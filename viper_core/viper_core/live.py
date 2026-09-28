import json
import logging
import asyncio
import base64
import collections
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote

from . import tts, vision


LOGGER = logging.getLogger(__name__)
GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"
LIVE_REPLAY_SECONDS = 3600
LIVE_SPEAKER_CHUNK_SECONDS = 2
LIVE_RADIO_PREROLL_SECONDS = 4.0
LIVE_RADIO_KEEPALIVE_SECONDS = 0.5
LIVE_AUDIO_FLUSH_SECONDS = 4.0
LIVE_DESCRIBE_NOW_EXTENSION_SECONDS = 10.0
LIVE_TEXT_AUDIO_FALLBACK_MIN_CHARS = 30
LIVE_VIDEO_FPS = 1
LIVE_VIDEO_MAX_BUFFER_BYTES = 2_000_000
LIVE_VIDEO_WARMUP_SECONDS = 2.5
LIVE_VIDEO_WARMUP_FRAMES = 2
LIVE_INITIAL_STABLE_FRAMES = 4
LIVE_EXACT_FRAME_SAVE_LIMIT = 8
LIVE_VISUAL_TASK_INTERVAL_SECONDS = 5.0
LIVE_DEBUG_DIR = Path("/data/doorbell_debug")
LIVE_DEBUG_KEEP_SECONDS = 3600

_LIVE_SESSIONS = {}
_LIVE_SESSIONS_LOCK = threading.Lock()
_LIVE_SESSION_COMMANDS = {}
_LIVE_SESSION_COMMANDS_LOCK = threading.Lock()
_LIVE_AUDIO_RELAYS = {}
_LIVE_AUDIO_RELAYS_LOCK = threading.Lock()


def create_live_session(door):
    session_id = f"{_door_key(door)}-{int(time.time() * 1000)}"
    now = int(time.time())
    with _LIVE_SESSIONS_LOCK:
        _cleanup_live_sessions_locked(now)
        _LIVE_SESSIONS[session_id] = {
            "id": session_id,
            "door": _door_key(door),
            "status": "starting",
            "started_at": now,
            "completed_at": 0,
            "expires_at": 0,
            "filename": "",
            "audio_url": "",
            "message": "",
            "speaker_chunks": 0,
            "last_speaker_result": "",
            "live_audio_url": "",
            "live_audio_targets": 0,
            "video_frames_sent": 0,
            "video_frames_discarded": 0,
            "visual_prompts_sent": 0,
            "raw_transcript": "",
            "raw_transcript_update": "",
            "scene_change": "unknown",
            "scene_summary": "",
            "scene_history": [],
            "scene_confidence": "unknown",
            "scene_risky_claim": "",
            "verification_status": "not needed",
            "verification_summary": "",
            "debug_contact_sheet_url": "",
            "debug_frame_count": 0,
        }
    return session_id


def get_live_session(session_id):
    session_id = str(session_id or "").strip()
    now = int(time.time())
    with _LIVE_SESSIONS_LOCK:
        _cleanup_live_sessions_locked(now)
        session = dict(_LIVE_SESSIONS.get(session_id) or {})
    return session


def describe_live_session_now(session_id):
    session_id = str(session_id or "").strip()
    if not session_id:
        return {"ok": False, "message": "Live session id is required."}
    session = get_live_session(session_id)
    if not session:
        return {"ok": False, "message": "Live session is not available."}
    if str(session.get("status") or "") != "live":
        return {"ok": False, "message": "Live session is not running."}
    with _LIVE_SESSION_COMMANDS_LOCK:
        commands = _LIVE_SESSION_COMMANDS.get(session_id)
    if not commands:
        return {"ok": False, "message": "Live session command channel is not available."}
    commands.put({"command": "describe_now", "timestamp": int(time.time())})
    _update_live_session(session_id, last_command="Describe now requested.")
    return {"ok": True, "message": "Describe-now request sent.", "session": get_live_session(session_id)}


def stop_live_session(session_id):
    session_id = str(session_id or "").strip()
    if not session_id:
        return {"ok": False, "message": "Live session id is required."}
    session = get_live_session(session_id)
    if not session:
        return {"ok": False, "message": "Live session is not available."}
    with _LIVE_SESSION_COMMANDS_LOCK:
        commands = _LIVE_SESSION_COMMANDS.get(session_id)
    if not commands:
        _update_live_session(session_id, status="done", last_command="Stop requested.", completed_at=int(time.time()), expires_at=int(time.time()) + LIVE_REPLAY_SECONDS)
        return {"ok": True, "message": "Live session was already stopping.", "session": get_live_session(session_id)}
    commands.put({"command": "stop", "timestamp": int(time.time())})
    _update_live_session(session_id, last_command="Stop requested.")
    return {"ok": True, "message": "Stop request sent.", "session": get_live_session(session_id)}


def _update_live_session(session_id, **changes):
    session_id = str(session_id or "").strip()
    if not session_id:
        return
    with _LIVE_SESSIONS_LOCK:
        session = _LIVE_SESSIONS.get(session_id)
        if not session:
            return
        session.update(changes)


def _cleanup_live_sessions_locked(now):
    expired = []
    for session_id, session in _LIVE_SESSIONS.items():
        expires_at = int(session.get("expires_at") or 0)
        started_at = int(session.get("started_at") or 0)
        if (expires_at and expires_at < now) or (not expires_at and started_at and now - started_at > LIVE_REPLAY_SECONDS):
            expired.append(session_id)
    for session_id in expired:
        _LIVE_SESSIONS.pop(session_id, None)
        with _LIVE_SESSION_COMMANDS_LOCK:
            _LIVE_SESSION_COMMANDS.pop(session_id, None)


def live_audio_stream(session_id):
    session_id = str(session_id or "").strip()
    with _LIVE_AUDIO_RELAYS_LOCK:
        relay = _LIVE_AUDIO_RELAYS.get(session_id)
    return relay.stream() if relay else None


def _session_id_or_create(door, session_id):
    session_id = str(session_id or "").strip()
    return session_id or create_live_session(door)


def doorbell_live_events(config, ha_client, control_state, event_handler, door, total_seconds=30, chunk_seconds=3, speak=False):
    door = _door_key(door)
    total_seconds = _clamped_int(total_seconds, 30, 5, 120)
    chunk_seconds = _clamped_int(chunk_seconds, 3, 2, 8)
    effective_config = control_state.effective_config(config) if control_state else config
    deadline = time.monotonic() + total_seconds
    last_text = ""
    yield _event("status", f"Started live description for {_door_label(door).lower()}.")
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        window = min(chunk_seconds, remaining)
        try:
            text = vision.describe_live_doorbell(effective_config, ha_client, door, seconds=window, mode="manual")
        except Exception as exc:
            LOGGER.warning("Live doorbell description failed: %s", exc)
            yield _event("error", f"Live description failed: {exc}")
            return
        text = _door_alert_message(door, text)
        if text and _meaningfully_new(last_text, text):
            last_text = text
            yield _event("update", text)
            if speak and event_handler:
                try:
                    event_handler("doorbell_live_update", {"door": door, "message": text, "all_speakers": False})
                except Exception as exc:
                    LOGGER.warning("Live doorbell speaker update failed: %s", exc)
                    yield _event("warning", f"Speaker update failed: {exc}")
    yield _event("done", f"Finished live description for {_door_label(door).lower()}.")


def gemini_true_live_doorbell_events(config, ha_client, control_state, event_handler, door, total_seconds=30, speak=False, all_speakers=False, force_confirmation=False, session_id=""):
    door = _door_key(door)
    session_id = _session_id_or_create(door, session_id)
    item_queue = queue.Queue()
    command_queue = queue.Queue()
    stop = object()
    with _LIVE_SESSION_COMMANDS_LOCK:
        _LIVE_SESSION_COMMANDS[session_id] = command_queue

    def runner():
        try:
            asyncio.run(_run_gemini_true_live(config, ha_client, control_state, event_handler, door, total_seconds, speak, all_speakers, force_confirmation, session_id, item_queue, command_queue))
        except Exception as exc:
            LOGGER.exception("Gemini true live doorbell session failed.")
            _update_live_session(session_id, status="error", message=str(exc), completed_at=int(time.time()), expires_at=int(time.time()) + LIVE_REPLAY_SECONDS)
            item_queue.put(_event("error", f"Gemini true live failed: {exc}"))
        finally:
            with _LIVE_SESSION_COMMANDS_LOCK:
                _LIVE_SESSION_COMMANDS.pop(session_id, None)
            item_queue.put(stop)

    thread = threading.Thread(target=runner, name=f"gemini-live-{_door_key(door)}", daemon=True)
    thread.start()
    yield {
        "event": "session",
        "session_id": session_id,
        "door": door,
        "message": f"Live session started for {_door_label(door).lower()}.",
        "timestamp": int(time.time()),
    }
    while True:
        item = item_queue.get()
        if item is stop:
            break
        yield item


async def _run_gemini_true_live(config, ha_client, control_state, event_handler, door, total_seconds, speak, all_speakers, force_confirmation, session_id, item_queue, command_queue=None):
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("Gemini Live needs the google-genai package in the Viper Core add-on image.") from exc
    door = _door_key(door)
    total_seconds = _clamped_int(total_seconds, 30, 25, 120)
    effective_config = control_state.effective_config(config) if control_state else config
    _update_live_session(session_id, door=door, status="live", message=f"Live narration is running for {_door_label(door).lower()}.")
    api_key = str(getattr(effective_config, "gemini_api_key", "") or "").strip()
    if not api_key:
        raise RuntimeError("Gemini API key is not configured.")
    native = getattr(effective_config, f"{door}_door_video_source", "rtsp") == "ring_native"
    stream_url = _stream_url(effective_config, door) if not native else ""
    camera_entity = getattr(effective_config, f"{door}_door_camera_entity", "") if native else ""
    if native and (not camera_entity or not ha_client.available()):
        raise RuntimeError(f"{_door_label(door)} Ring live camera is not available.")
    if not native and not stream_url:
        raise RuntimeError(f"{_door_label(door)} RTSP stream URL is not configured.")
    if not native and not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not available.")
    model = str(getattr(effective_config, "gemini_live_model", "") or GEMINI_LIVE_MODEL).strip()
    client = genai.Client(api_key=api_key)
    prompt = (
        f"You are Viper Vision watching the {_door_label(door).lower()} camera live for a blind homeowner. "
        "Never acknowledge these instructions, never say you understand, and never announce that you are watching. "
        "Only narrate what is visibly happening in the camera view. Be concise, practical, and calm. "
        "Accuracy is more important than confidence. Do not guess. If the image is unclear, partially blocked, too dark, or ambiguous, say you cannot tell. "
        "Use plain confidence wording: say clearly visible only when certain, say possibly when uncertain, and say cannot tell when the view is not reliable. "
        "Treat the curved fisheye edges, shadows, furniture, distant yards, sheds, garages, and driveway objects as low priority background. "
        "Focus first on the door, deck, steps, walkway, and any nearby person or package. "
        "Only identify an animal, package, person, face, hand gesture, vehicle, blockage, darkness, water, fog, or condensation when it is clearly visible in multiple recent frames. "
        "If a vehicle or distant object is not clearly relevant to the door area, do not mention it. "
        "If a person is visible, describe the person and their action before mentioning anything else. "
        "If a person is visible when the session begins, immediately describe the person and what they are doing. "
        "If nothing is happening, stay quiet until something meaningful changes. "
        f"Always call this location the {_door_label(door).lower()}."
    )
    if force_confirmation:
        prompt += (
            " This is a manual test. Do not announce that the test started. "
            "Begin by describing only what is visible in the camera view."
        )
    config_payload = {
        "response_modalities": ["AUDIO"],
        "system_instruction": prompt,
        "output_audio_transcription": {},
    }
    item_queue.put(_event("status", f"Started Gemini true live for {_door_label(door).lower()}."))
    audio_buffer = bytearray()
    state = {
        "last_transcript": "",
        "raw_transcript_parts": [],
        "allow_output": False,
        "force_confirmation": bool(force_confirmation),
        "session_id": session_id,
        "scene_summary": "",
        "scene_history": [],
        "config": effective_config,
        "ha_client": ha_client,
        "door": door,
        "event_handler": event_handler,
        "all_speakers": all_speakers,
        "speak": speak,
        "text_audio_fallback_spoken": False,
    }
    speaker_streamer = _LiveSpeakerStreamer(ha_client, control_state, effective_config, all_speakers, item_queue, session_id) if speak else None
    live_stream = vision._prepare_live_stream(effective_config, ha_client, door) if not native else None
    try:
        async with client.aio.live.connect(model=model, config=config_payload) as session:
            receiver = asyncio.create_task(_receive_gemini_live(session, item_queue, audio_buffer, state, speaker_streamer))
            deadline = time.monotonic() + total_seconds
            video_stats = await _send_live_video_stream(
                session,
                types,
                stream_url,
                door,
                force_confirmation,
                deadline,
                audio_buffer,
                session_id=session_id,
                state=state,
                effective_config=effective_config,
                ha_client=ha_client,
                command_queue=command_queue,
                item_queue=item_queue,
            )
            frame_count = int(video_stats.get("sent_frames") or 0)
            _update_live_session(
                session_id,
                video_frames_sent=frame_count,
                video_frames_discarded=int(video_stats.get("discarded_frames") or 0),
                visual_prompts_sent=int(video_stats.get("visual_prompts") or 0),
            )
            if _raw_live_transcript(state) and not audio_buffer:
                LOGGER.info(
                    "Gemini true live produced transcript but no audio yet for %s; waiting %.1fs for late audio.",
                    _door_label(door).lower(),
                    LIVE_AUDIO_FLUSH_SECONDS,
                )
                flush_deadline = time.monotonic() + LIVE_AUDIO_FLUSH_SECONDS
                while time.monotonic() < flush_deadline and not audio_buffer:
                    await asyncio.sleep(0.1)
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass
            if not audio_buffer:
                LOGGER.warning(
                    "Gemini true live finished without audio for %s after %s frame(s), %ss duration.",
                    _door_label(door).lower(),
                    frame_count,
                    total_seconds,
                )
    finally:
        vision._cleanup_live_stream(ha_client, live_stream)
        if speaker_streamer:
            speaker_streamer.close()
    transcript = _full_live_transcript(state)
    raw_transcript = _raw_live_transcript(state)
    fallback_text = await asyncio.to_thread(_describe_now_fallback_if_needed, state, session_id)
    if fallback_text:
        _record_raw_live_transcript(state, fallback_text)
        _record_live_transcript(state, fallback_text)
        scene_update = _record_scene_observation(state, fallback_text)
        if scene_update:
            _update_live_session(
                session_id,
                scene_change=scene_update["change"],
                scene_summary=scene_update["summary"],
                scene_history=scene_update["history"],
                scene_confidence=scene_update["confidence"],
                scene_risky_claim=scene_update["risky_claim"],
                verification_status=scene_update["verification_status"],
                verification_summary=scene_update["verification_summary"],
            )
        transcript = _full_live_transcript(state)
        raw_transcript = _raw_live_transcript(state)
        item_queue.put({"event": "raw_transcript", "message": raw_transcript, "chunk": fallback_text, "timestamp": int(time.time())})
        item_queue.put(_event("update", fallback_text))
    await asyncio.to_thread(_verify_latest_scene_if_needed, state, session_id)
    LOGGER.info(
        "Gemini true live transcript for %s: final=%r raw=%r",
        _door_label(door).lower(),
        transcript,
        raw_transcript,
    )
    _update_live_session(session_id, message=transcript or raw_transcript, raw_transcript=raw_transcript)
    if raw_transcript:
        item_queue.put({"event": "final_transcript", "message": transcript or raw_transcript, "raw": raw_transcript, "timestamp": int(time.time())})
    if audio_buffer and _live_text_speech_allowed_after_verification(state, session_id, transcript or raw_transcript):
        try:
            filename = _write_live_wav(audio_buffer)
            replay_url = _live_audio_url(filename, effective_config)
            _update_live_session(
                session_id,
                status="replay",
                filename=filename,
                audio_url=replay_url,
                message=transcript or raw_transcript or f"{_door_label(door)} live narration replay is ready.",
                completed_at=int(time.time()),
                expires_at=int(time.time()) + LIVE_REPLAY_SECONDS,
            )
            played = speaker_streamer.played if speaker_streamer else 0
            item_queue.put({
                "event": "speaker_audio",
                "message": f"Live narration replay is ready. Streamed audio to {played} speaker target(s).",
                "filename": filename,
                "url": replay_url,
                "timestamp": int(time.time()),
            })
        except Exception as exc:
            _update_live_session(session_id, status="error", message=f"Could not prepare replay audio: {exc}", completed_at=int(time.time()), expires_at=int(time.time()) + LIVE_REPLAY_SECONDS)
            item_queue.put(_event("warning", f"Could not prepare speaker playback: {exc}"))
    elif audio_buffer:
        safe_message = _unverified_live_result_message(door, session_id)
        _update_live_session(
            session_id,
            status="done",
            message=safe_message,
            completed_at=int(time.time()),
            expires_at=int(time.time()) + LIVE_REPLAY_SECONDS,
        )
        if speak and event_handler and not state.get("text_audio_fallback_spoken"):
            event_handler("doorbell_live_update", {"door": door, "message": safe_message, "all_speakers": all_speakers})
        item_queue.put(_event("status", safe_message))
    elif raw_transcript:
        item_queue.put(_event("update", transcript or raw_transcript))
        if speak and event_handler and not state.get("text_audio_fallback_spoken"):
            spoken_text = transcript or raw_transcript
            if _live_text_speech_allowed_after_verification(state, session_id, spoken_text):
                event_handler("doorbell_live_update", {"door": door, "message": spoken_text, "all_speakers": all_speakers})
            else:
                safe_message = _unverified_live_result_message(door, session_id)
                _update_live_session(
                    session_id,
                    status="done",
                    message=safe_message,
                    completed_at=int(time.time()),
                    expires_at=int(time.time()) + LIVE_REPLAY_SECONDS,
                )
                event_handler("doorbell_live_update", {"door": door, "message": safe_message, "all_speakers": all_speakers})
                item_queue.put(_event("status", safe_message))
    else:
        _update_live_session(session_id, status="done", message="Live narration finished without saved audio.", completed_at=int(time.time()), expires_at=int(time.time()) + LIVE_REPLAY_SECONDS)
    item_queue.put(_event("done", f"Finished Gemini true live for {_door_label(door).lower()}."))


async def _send_live_visual_task(session, door, force_confirmation=False, first=True):
    if force_confirmation and first:
        text = (
            f"Describe the current {_door_label(door).lower()} camera view now. "
            "Base your answer on the recent stable frames, not the first startup frame. "
            "Only describe what is clearly visible near the door, deck, steps, walkway, or nearby yard. "
            "Do not mention vehicles, darkness, condensation, fog, water, or a blocked lens unless it is obvious across the recent frames. "
            "If you are not sure, say you cannot tell. "
            "Do not say understood, do not mention a test, and do not mention instructions."
        )
    elif first:
        text = (
            f"Watch the {_door_label(door).lower()} camera. If a person, package, vehicle, animal, or active movement is visible, "
            "briefly describe it now. Focus on the door, deck, steps, walkway, and nearby yard. "
            "Do not mention distant vehicles, shadows, darkness, condensation, fog, water, or a blocked lens unless it is obvious across the recent frames. "
            "Only identify things you can clearly see. If nothing meaningful is visible, stay silent."
        )
    else:
        if force_confirmation:
            text = (
                f"Check the {_door_label(door).lower()} camera again now. "
                "Give one short sentence describing the current visible state or the meaningful change. "
                "If nothing meaningful changed, say exactly: No meaningful change visible. "
                "Only identify people, packages, vehicles, animals, darkness, condensation, fog, or blockage if clearly visible across recent frames."
            )
        else:
            text = (
                f"Check the {_door_label(door).lower()} camera again. If the scene changed, a person is moving, or meaningful activity is visible now, "
                "briefly describe only the new visible activity near the door area. Only identify things you can clearly see across recent frames. "
                "If nothing changed or nothing meaningful is visible, stay silent."
            )
    try:
        await session.send_realtime_input(text=text)
    except Exception:
        LOGGER.debug("Gemini Live SDK did not accept visual task text.", exc_info=True)


async def _send_live_video_stream(
    session,
    types,
    stream_url,
    door,
    force_confirmation,
    deadline,
    audio_buffer,
    session_id=None,
    state=None,
    effective_config=None,
    ha_client=None,
    command_queue=None,
    item_queue=None,
):
    native = getattr(effective_config, f"{door}_door_video_source", "rtsp") == "ring_native"
    process = None
    reader = None
    native_task = None
    frame_queue = None
    if native:
        from .ring_camera import stream

        frame_queue = asyncio.Queue(maxsize=4)

        async def receive_frame(item):
            if frame_queue.full():
                frame_queue.get_nowait()
            frame_queue.put_nowait(item["jpeg"])

        camera_entity = getattr(effective_config, f"{door}_door_camera_entity", "")
        native_task = asyncio.create_task(stream(
            ha_client.websocket_url(), ha_client.token, camera_entity,
            max(1, int(deadline - time.monotonic())), receive_frame, fps=LIVE_VIDEO_FPS,
        ))
    else:
        process = await asyncio.to_thread(_start_live_video_process, stream_url)
        reader = _MjpegFrameReader(process.stdout)
    frame_count = 0
    discarded_frames = 0
    stability_frames = 0
    visual_prompts_sent = 0
    sent_visual_task = False
    last_visual_task = 0
    saved_frames = []
    debug_dir = _live_session_debug_dir(session_id)
    warmup_deadline = min(deadline, time.monotonic() + LIVE_VIDEO_WARMUP_SECONDS)
    LOGGER.info(
        "Started Gemini true live video pipe for %s at %s fps with %.1fs/%s-frame warmup discard and %s-frame stable gate.",
        _door_label(door).lower(),
        LIVE_VIDEO_FPS,
        LIVE_VIDEO_WARMUP_SECONDS,
        LIVE_VIDEO_WARMUP_FRAMES,
        LIVE_INITIAL_STABLE_FRAMES,
    )
    try:
        if debug_dir:
            await asyncio.to_thread(_cleanup_debug_captures)
            await asyncio.to_thread(debug_dir.mkdir, parents=True, exist_ok=True)
        while time.monotonic() < deadline:
            command = _next_live_command(command_queue)
            if command == "stop":
                message = f"Stop prompt received for {_door_label(door).lower()} live session."
                LOGGER.info(message)
                if session_id:
                    _update_live_session(session_id, status="stopping", last_command=message)
                if item_queue:
                    item_queue.put(_event("status", message))
                break
            if native:
                if native_task.done() and frame_queue.empty():
                    native_task.result()
                    break
                try:
                    frame = await asyncio.wait_for(frame_queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
            else:
                frame = await asyncio.to_thread(reader.read_frame)
            if not frame:
                if process and process.poll() is not None:
                    stderr = _read_process_stderr(process)
                    LOGGER.warning("Gemini true live video pipe stopped after %s frame(s): %s", frame_count, stderr or "ffmpeg exited")
                    break
                await asyncio.sleep(0.05)
                continue
            if time.monotonic() < warmup_deadline or discarded_frames < LIVE_VIDEO_WARMUP_FRAMES:
                discarded_frames += 1
                continue
            if stability_frames < LIVE_INITIAL_STABLE_FRAMES:
                stability_frames += 1
                if stability_frames == LIVE_INITIAL_STABLE_FRAMES:
                    LOGGER.info(
                        "Skipped %s initial stable-gate frame(s) before sending Gemini live video for %s.",
                        stability_frames,
                        _door_label(door).lower(),
                    )
                continue
            if not sent_visual_task and state is not None:
                state["allow_output"] = True
            frame_count += 1
            if len(saved_frames) < LIVE_EXACT_FRAME_SAVE_LIMIT:
                saved_frames.append(frame)
            await session.send_realtime_input(video=types.Blob(data=frame, mime_type="image/jpeg"))
            if session_id:
                _update_live_session(
                    session_id,
                    video_frames_sent=frame_count,
                    video_frames_discarded=discarded_frames,
                    visual_prompts_sent=visual_prompts_sent,
                )
            if frame_count <= 5 or frame_count % 5 == 0:
                LOGGER.info(
                    "Sent Gemini true live video frame %s for %s.",
                    frame_count,
                    _door_label(door).lower(),
                )
            if not sent_visual_task:
                await _send_live_visual_task(session, door, force_confirmation, first=True)
                visual_prompts_sent += 1
                if session_id:
                    _update_live_session(session_id, visual_prompts_sent=visual_prompts_sent)
                sent_visual_task = True
                last_visual_task = time.monotonic()
            elif time.monotonic() - last_visual_task > LIVE_VISUAL_TASK_INTERVAL_SECONDS:
                await _send_live_visual_task(session, door, force_confirmation, first=False)
                visual_prompts_sent += 1
                if session_id:
                    _update_live_session(session_id, visual_prompts_sent=visual_prompts_sent)
                last_visual_task = time.monotonic()
            if command == "describe_now":
                deadline = max(deadline, time.monotonic() + LIVE_DESCRIBE_NOW_EXTENSION_SECONDS)
                if state is not None:
                    state["describe_now_pending"] = True
                    state["describe_now_started_at"] = time.monotonic()
                    state["describe_now_part_count"] = len(state.get("raw_transcript_parts") or [])
                await _send_live_visual_task(session, door, True, first=False)
                visual_prompts_sent += 1
                last_visual_task = time.monotonic()
                message = f"Describe-now prompt sent for {_door_label(door).lower()} and live session extended."
                LOGGER.info(message)
                if session_id:
                    _update_live_session(
                        session_id,
                        visual_prompts_sent=visual_prompts_sent,
                        last_command=message,
                    )
                if item_queue:
                    item_queue.put(_event("status", message))
    finally:
        if native_task:
            native_task.cancel()
            await asyncio.gather(native_task, return_exceptions=True)
        if process:
            await asyncio.to_thread(_stop_live_video_process, process)
        debug_artifacts = {}
        if debug_dir and saved_frames:
            debug_artifacts = await asyncio.to_thread(_write_live_session_debug_artifacts, debug_dir, saved_frames, effective_config)
            if session_id and debug_artifacts:
                _update_live_session(
                    session_id,
                    debug_contact_sheet_url=str(debug_artifacts.get("contact_sheet_url") or ""),
                    debug_frame_count=int(debug_artifacts.get("frame_count") or 0),
                )
        LOGGER.info(
            "Gemini true live video pipe finished for %s: sent_frames=%s discarded_frames=%s stability_frames=%s visual_prompts=%s debug_frames=%s contact_sheet=%s.",
            _door_label(door).lower(),
            frame_count,
            discarded_frames,
            stability_frames,
            visual_prompts_sent,
            len(saved_frames),
            debug_artifacts.get("contact_sheet_url", ""),
        )
    return {
        "sent_frames": frame_count,
        "discarded_frames": discarded_frames,
        "stability_frames": stability_frames,
        "visual_prompts": visual_prompts_sent,
    }


def _next_live_command(command_queue):
    if not command_queue:
        return ""
    command = ""
    while True:
        try:
            item = command_queue.get_nowait()
        except queue.Empty:
            return command
        if isinstance(item, dict):
            command = str(item.get("command") or "").strip()


def capture_diagnostic_frames(config, ha_client, door, seconds=8, frame_limit=6):
    door = _door_key(door)
    if getattr(config, f"{door}_door_video_source", "rtsp") == "ring_native":
        return _capture_native_diagnostic_frames(config, ha_client, door, seconds, frame_limit)
    stream_url = _stream_url(config, door)
    if not stream_url:
        return {"ok": False, "message": f"{_door_label(door)} RTSP stream URL is not configured."}
    if not shutil.which("ffmpeg"):
        return {"ok": False, "message": "ffmpeg is not available."}
    live_stream = vision._prepare_live_stream(config, ha_client, door)
    process = None
    frames = []
    discarded_frames = 0
    started = time.monotonic()
    capture_id = f"{door}_{int(time.time() * 1000)}"
    debug_dir = LIVE_DEBUG_DIR / capture_id
    try:
        _cleanup_debug_captures()
        debug_dir.mkdir(parents=True, exist_ok=True)
        mp4_path = debug_dir / "stream.mp4"
        _capture_debug_video(stream_url, mp4_path, seconds)
        process = _start_live_video_process(stream_url)
        reader = _MjpegFrameReader(process.stdout)
        total_seconds = _clamped_int(seconds, 8, 4, 20)
        limit = _clamped_int(frame_limit, 6, 1, 12)
        warmup_deadline = time.monotonic() + LIVE_VIDEO_WARMUP_SECONDS
        deadline = time.monotonic() + LIVE_VIDEO_WARMUP_SECONDS + total_seconds
        while time.monotonic() < deadline and len(frames) < limit:
            frame = reader.read_frame()
            if not frame:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            if time.monotonic() < warmup_deadline or discarded_frames < LIVE_VIDEO_WARMUP_FRAMES:
                discarded_frames += 1
                continue
            frames.append(frame)
        frame_paths = _write_debug_frames(debug_dir, frames)
        contact_sheet_path = _write_debug_contact_sheet(debug_dir, frame_paths)
    finally:
        if process:
            _stop_live_video_process(process)
        vision._cleanup_live_stream(ha_client, live_stream)
    artifacts = _debug_artifacts(capture_id, debug_dir, frame_paths if "frame_paths" in locals() else [], contact_sheet_path if "contact_sheet_path" in locals() else None, mp4_path if "mp4_path" in locals() else None, config)
    return {
        "ok": bool(frames),
        "door": door,
        "capture_id": capture_id,
        "stream_url": stream_url,
        "seconds": int(seconds or 8),
        "fps": LIVE_VIDEO_FPS,
        "warmup_seconds": LIVE_VIDEO_WARMUP_SECONDS,
        "warmup_frames": LIVE_VIDEO_WARMUP_FRAMES,
        "discarded_frames": discarded_frames,
        "captured_frames": len(frames),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "artifacts": artifacts,
        "message": f"Captured {len(frames)} diagnostic frame(s) for {_door_label(door).lower()}.",
    }


def _capture_native_diagnostic_frames(config, ha_client, door, seconds, frame_limit):
    started = time.monotonic()
    total_seconds = _clamped_int(seconds, 8, 4, 20)
    limit = _clamped_int(frame_limit, 6, 1, 12)
    capture_id = f"{door}_{int(time.time() * 1000)}"
    debug_dir = LIVE_DEBUG_DIR / capture_id
    _cleanup_debug_captures()
    debug_dir.mkdir(parents=True, exist_ok=True)
    frames = vision.capture_native_sequence(config, ha_client, door, total_seconds, fps=LIVE_VIDEO_FPS)
    discarded = min(LIVE_VIDEO_WARMUP_FRAMES, len(frames))
    kept = frames[discarded:discarded + limit]
    frame_paths = _write_debug_frames(debug_dir, kept)
    contact_sheet_path = _write_debug_contact_sheet(debug_dir, frame_paths)
    mp4_path = debug_dir / "stream.mp4"
    if frames:
        mp4_path.write_bytes(vision._encode_native_video(frames))
    artifacts = _debug_artifacts(capture_id, debug_dir, frame_paths, contact_sheet_path, mp4_path if frames else None, config)
    return {
        "ok": bool(kept), "door": door, "capture_id": capture_id, "stream_url": "",
        "camera_entity": getattr(config, f"{door}_door_camera_entity", ""),
        "seconds": total_seconds, "fps": LIVE_VIDEO_FPS,
        "warmup_seconds": LIVE_VIDEO_WARMUP_SECONDS,
        "warmup_frames": LIVE_VIDEO_WARMUP_FRAMES, "discarded_frames": discarded,
        "captured_frames": len(kept), "elapsed_seconds": round(time.monotonic() - started, 2),
        "artifacts": artifacts,
        "message": f"Captured {len(kept)} diagnostic frame(s) for {_door_label(door).lower()}.",
    }


def diagnostic_file_path(filename):
    filename = str(filename or "").strip().replace("\\", "/").lstrip("/")
    if not filename or ".." in filename.split("/"):
        return None
    path = LIVE_DEBUG_DIR / filename
    try:
        resolved = path.resolve()
        root = LIVE_DEBUG_DIR.resolve()
        if root not in resolved.parents and resolved != root:
            return None
        if resolved.exists() and resolved.is_file():
            return resolved
    except OSError:
        return None
    return None


def _capture_debug_video(stream_url, output_path, seconds):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if str(stream_url).lower().startswith("rtsp://"):
        command += ["-rtsp_transport", "tcp"]
    command += [
        "-i",
        str(stream_url),
        "-t",
        str(_clamped_int(seconds, 8, 4, 20)),
        "-an",
        "-vf",
        "scale=640:-2",
        "-c:v",
        "mpeg4",
        "-q:v",
        "5",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=max(30, int(seconds or 8) + 20))
    if result.returncode != 0:
        LOGGER.warning("Doorbell debug MP4 capture failed: %s", (result.stderr or result.stdout or "FFmpeg failed").strip()[:300])


def _write_debug_frames(debug_dir, frames):
    paths = []
    for index, frame in enumerate(frames, start=1):
        path = debug_dir / f"gemini_frame_{index:02d}.jpg"
        path.write_bytes(frame)
        paths.append(path)
    return paths


def _write_debug_contact_sheet(debug_dir, frame_paths):
    if not frame_paths:
        return None
    list_path = debug_dir / "frames.txt"
    list_path.write_text("".join(f"file '{path.name}'\n" for path in frame_paths), encoding="utf-8")
    output_path = debug_dir / "contact_sheet.jpg"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-pattern_type",
        "glob",
        "-i",
        str(debug_dir / "gemini_frame_*.jpg"),
        "-vf",
        "scale=320:-2,tile=3x4",
        "-frames:v",
        "1",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        LOGGER.warning("Doorbell debug contact sheet failed: %s", (result.stderr or result.stdout or "FFmpeg failed").strip()[:300])
        return None
    return output_path if output_path.exists() else None


def _debug_artifacts(capture_id, debug_dir, frame_paths, contact_sheet_path, mp4_path, config):
    artifacts = {}
    for name, path in (("video", mp4_path), ("contact_sheet", contact_sheet_path)):
        if path and path.exists():
            artifacts[name] = {
                "path": str(path),
                "url": _debug_file_url(capture_id, path.name, config),
            }
    frames = []
    for path in frame_paths:
        if path.exists():
            frames.append({"path": str(path), "url": _debug_file_url(capture_id, path.name, config)})
    artifacts["frames"] = frames
    artifacts["directory"] = str(debug_dir)
    return artifacts


def _debug_file_url(capture_id, filename, config):
    base = str(getattr(config, "speaker_base_url", "") or getattr(config, "external_base_url", "") or "").rstrip("/")
    if not base:
        base = "http://homeassistant.local:8099"
    return f"{base}/doorbell-debug/{quote(str(capture_id), safe='')}/{quote(str(filename), safe='')}"


def _live_session_debug_dir(session_id):
    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in session_id)
    return LIVE_DEBUG_DIR / f"live_{safe}"


def _write_live_session_debug_artifacts(debug_dir, frames, config):
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        for child in debug_dir.iterdir():
            if child.is_file():
                child.unlink()
        frame_paths = _write_debug_frames(debug_dir, frames)
        contact_sheet_path = _write_debug_contact_sheet(debug_dir, frame_paths)
        capture_id = debug_dir.name
        return {
            "frame_count": len(frame_paths),
            "contact_sheet_url": _debug_file_url(capture_id, contact_sheet_path.name, config) if contact_sheet_path else "",
            "directory": str(debug_dir),
        }
    except OSError:
        LOGGER.debug("Could not write live session debug frames for %s.", debug_dir, exc_info=True)
        return {}


def _cleanup_debug_captures():
    now = time.time()
    try:
        LIVE_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        for path in LIVE_DEBUG_DIR.iterdir():
            if not path.is_dir():
                continue
            try:
                if now - path.stat().st_mtime < LIVE_DEBUG_KEEP_SECONDS:
                    continue
                for child in path.iterdir():
                    child.unlink()
                path.rmdir()
            except OSError:
                LOGGER.debug("Could not clean old doorbell debug capture %s.", path, exc_info=True)
    except OSError:
        LOGGER.debug("Could not scan doorbell debug captures.", exc_info=True)


async def _receive_gemini_live(session, item_queue, audio_buffer, state, speaker_streamer=None):
    suppress_current_turn = False
    async for response in session.receive():
        server_content = getattr(response, "server_content", None)
        if not server_content:
            continue
        transcription = getattr(server_content, "output_transcription", None)
        text = str(getattr(transcription, "text", "") or "").strip() if transcription else ""
        if text:
            _record_raw_live_transcript(state, text)
            LOGGER.info("Gemini true live transcript chunk: %r", text)
            if not state.get("allow_output", True):
                LOGGER.info("Suppressed Gemini Live pre-gate transcript chunk: %r", text)
                suppress_current_turn = False
                continue
            if _is_live_setup_acknowledgement(text):
                suppress_current_turn = True
                LOGGER.info("Suppressed Gemini Live setup acknowledgement: %s", text)
                continue
            suppress_current_turn = False
            raw_text = _raw_live_transcript(state)
            _update_live_session(state.get("session_id", ""), raw_transcript=raw_text or text, raw_transcript_update=text)
            item_queue.put({"event": "raw_transcript", "message": raw_text or text, "chunk": text, "timestamp": int(time.time())})
            scene_update = _record_scene_observation(state, text)
            if scene_update:
                if scene_update["risky_claim"] and scene_update["verification_status"] == "pending":
                    state["live_speech_hold_for_verification"] = True
                _update_live_session(
                    state.get("session_id", ""),
                    scene_change=scene_update["change"],
                    scene_summary=scene_update["summary"],
                    scene_history=scene_update["history"],
                    scene_confidence=scene_update["confidence"],
                    scene_risky_claim=scene_update["risky_claim"],
                    verification_status=scene_update["verification_status"],
                    verification_summary=scene_update["verification_summary"],
                )
                LOGGER.info(
                    "Gemini live scene memory: change=%s confidence=%s risky=%s verification=%s summary=%r previous=%r",
                    scene_update["change"],
                    scene_update["confidence"],
                    scene_update["risky_claim"],
                    scene_update["verification_status"],
                    scene_update["summary"],
                    scene_update.get("previous", ""),
                )
                item_queue.put({
                    "event": "scene",
                    "message": scene_update["summary"],
                    "change": scene_update["change"],
                    "confidence": scene_update["confidence"],
                    "risky_claim": scene_update["risky_claim"],
                    "verification_status": scene_update["verification_status"],
                    "previous": scene_update.get("previous", ""),
                    "timestamp": int(time.time()),
                })
            if _record_live_transcript(state, text):
                item_queue.put({"event": "transcript", "message": state["last_transcript"], "timestamp": int(time.time())})
                await _speak_live_text_audio_fallback_if_needed(state, audio_buffer, item_queue)
        model_turn = getattr(server_content, "model_turn", None)
        for part in getattr(model_turn, "parts", []) or []:
            if suppress_current_turn:
                continue
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", b"") if inline else b""
            if not state.get("allow_output", True):
                if data:
                    LOGGER.info("Suppressed Gemini Live pre-gate audio chunk.")
                continue
            if isinstance(data, str):
                try:
                    data = base64.b64decode(data)
                except Exception:
                    data = b""
            if not data:
                continue
            audio_buffer.extend(data)
            if speaker_streamer and not state.get("text_audio_fallback_spoken") and not state.get("live_speech_hold_for_verification"):
                speaker_streamer.add(data)
            item_queue.put({
                "event": "audio",
                "audio": base64.b64encode(data).decode("ascii"),
                "mime_type": getattr(inline, "mime_type", "audio/pcm;rate=24000") or "audio/pcm;rate=24000",
                "sample_rate": 24000,
                "timestamp": int(time.time()),
            })


def _record_live_transcript(state, text):
    text = str(text or "").strip()
    if not _is_useful_live_transcript(text):
        return False
    current = str(state.get("last_transcript") or "").strip()
    if not current or _is_better_live_transcript(text, current):
        state["last_transcript"] = text
        return True
    return False


def _record_raw_live_transcript(state, text):
    text = str(text or "").strip()
    if not text:
        return
    parts = state.setdefault("raw_transcript_parts", [])
    if not parts or parts[-1] != text:
        parts.append(text)


async def _speak_live_text_audio_fallback_if_needed(state, audio_buffer, item_queue):
    if audio_buffer:
        return
    if state.get("text_audio_fallback_spoken"):
        return
    if not state.get("speak"):
        return
    event_handler = state.get("event_handler")
    if not event_handler:
        return
    text = str(state.get("last_transcript") or "").strip()
    if len(text) < LIVE_TEXT_AUDIO_FALLBACK_MIN_CHARS:
        return
    door = _door_key(state.get("door") or "front")
    risky_claim = _scene_risky_claim(text)
    if risky_claim:
        state["text_audio_fallback_blocked_risky"] = True
        LOGGER.info(
            "Held Gemini live transcript fallback for %s until %s claim is verified: %r",
            _door_label(door).lower(),
            risky_claim,
            text,
        )
        item_queue.put(_event("status", f"Holding live speech until the {risky_claim} claim is verified."))
        return
    all_speakers = bool(state.get("all_speakers"))
    try:
        await asyncio.to_thread(event_handler, "doorbell_live_update", {"door": door, "message": text, "all_speakers": all_speakers})
    except Exception as exc:
        LOGGER.warning("Live transcript TTS fallback failed for %s: %s", _door_label(door).lower(), exc)
        item_queue.put(_event("warning", f"Live transcript speech fallback failed: {exc}"))
        return
    state["text_audio_fallback_spoken"] = True
    LOGGER.info("Spoke Gemini live transcript fallback for %s because no Gemini audio arrived yet.", _door_label(door).lower())
    item_queue.put(_event("speaker_audio", "Spoke live transcript using TTS because Gemini returned text without audio."))


def _live_text_speech_allowed_after_verification(state, session_id, text):
    risky_claim = _scene_risky_claim(text)
    if not risky_claim:
        return True
    session = get_live_session(session_id)
    status = str(session.get("verification_status") or "").strip().lower()
    if status == "confirmed":
        return True
    door = _door_key(state.get("door") or "front")
    LOGGER.info(
        "Suppressed Gemini live speech for %s because %s claim was not verified: status=%s text=%r verification=%r",
        _door_label(door).lower(),
        risky_claim,
        status or "unknown",
        text,
        session.get("verification_summary") or "",
    )
    return False


def _unverified_live_result_message(door, session_id):
    session = get_live_session(session_id)
    risky_claim = str(session.get("scene_risky_claim") or "activity").strip()
    verification = str(session.get("verification_summary") or "").strip()
    base = f"{_door_label(door)} live check could not verify the reported {risky_claim}."
    if verification:
        return f"{base} {verification}"
    return base


def _full_live_transcript(state):
    return str(state.get("last_transcript", "") or "").strip()


def _raw_live_transcript(state):
    return " ".join(str(part or "").strip() for part in state.get("raw_transcript_parts", []) if str(part or "").strip()).strip()


def _record_scene_observation(state, text):
    text = str(text or "").strip()
    if not _is_useful_live_transcript(text):
        return {}
    previous = str(state.get("scene_summary") or "").strip()
    change = _classify_scene_change(previous, text)
    confidence = _scene_confidence(text)
    risky_claim = _scene_risky_claim(text)
    verification_status = "not needed"
    verification_summary = ""
    if risky_claim:
        verification_status = "pending"
    state["scene_summary"] = text
    history = list(state.get("scene_history") or [])
    history.append({
        "change": change,
        "confidence": confidence,
        "risky_claim": risky_claim,
        "verification_status": verification_status,
        "summary": text,
        "previous": previous,
        "timestamp": int(time.time()),
    })
    state["scene_history"] = history[-6:]
    return {
        "change": change,
        "confidence": confidence,
        "risky_claim": risky_claim,
        "verification_status": verification_status,
        "verification_summary": verification_summary,
        "summary": text,
        "previous": previous,
        "history": state["scene_history"],
    }


def _classify_scene_change(previous, current):
    previous = str(previous or "").strip()
    current = str(current or "").strip()
    if not current:
        return "unknown"
    if not previous:
        return "new"
    previous_norm = _scene_tokens(previous)
    current_norm = _scene_tokens(current)
    if not previous_norm or not current_norm:
        return "new"
    overlap = len(previous_norm & current_norm) / max(1, len(previous_norm | current_norm))
    if _scene_mentions_contradiction(previous_norm, current_norm):
        return "contradiction"
    if overlap >= 0.55:
        return "same"
    return "new"


def _scene_tokens(text):
    words = []
    for raw in str(text or "").lower().replace("-", " ").split():
        token = "".join(ch for ch in raw if ch.isalnum())
        if token and token not in {"a", "an", "and", "are", "at", "by", "for", "in", "is", "it", "no", "of", "on", "or", "the", "to", "with"}:
            words.append(token)
    return set(words)


def _scene_mentions_contradiction(previous_tokens, current_tokens):
    groups = [
        {"person", "people", "visitor", "someone"},
        {"package", "box", "delivery"},
        {"vehicle", "car", "truck", "pickup"},
        {"animal", "cat", "dog"},
        {"dark", "darkness", "night"},
        {"condensation", "fog", "foggy", "blocked", "obscured", "water"},
        {"empty", "clear", "nothing"},
    ]
    previous_groups = {index for index, group in enumerate(groups) if previous_tokens & group}
    current_groups = {index for index, group in enumerate(groups) if current_tokens & group}
    if not previous_groups or not current_groups:
        return False
    if previous_groups == current_groups:
        return False
    harmless = previous_groups | current_groups
    if harmless <= {0, 1}:
        return False
    return True


def _scene_confidence(text):
    lowered = str(text or "").lower()
    uncertain = {
        "appears",
        "appear",
        "possibly",
        "probably",
        "maybe",
        "might",
        "could",
        "seems",
        "unclear",
        "cannot tell",
        "can't tell",
        "not sure",
    }
    if any(token in lowered for token in uncertain):
        return "low"
    risky = _scene_risky_claim(text)
    return "high" if risky else "normal"


def _scene_risky_claim(text):
    tokens = _scene_tokens(text)
    checks = [
        ("person", {"person", "people", "visitor", "someone"}),
        ("package", {"package", "box", "delivery"}),
        ("vehicle", {"vehicle", "car", "truck", "pickup"}),
        ("animal", {"animal", "cat", "dog"}),
        ("blocked", {"dark", "darkness", "condensation", "fog", "foggy", "blocked", "obscured", "water"}),
    ]
    for label, group in checks:
        if tokens & group:
            return label
    return ""


def _verify_live_scene_claim(state, text, risky_claim):
    config = state.get("config")
    ha_client = state.get("ha_client")
    door = state.get("door")
    if not config or not ha_client or not door:
        return "unavailable", ""
    try:
        prompt = (
            f"Verify this live doorbell claim for a blind homeowner: {text} "
            "Look only at the current camera frames. Answer with CONFIRMED, UNCLEAR, or NOT CONFIRMED, followed by one short reason."
        )
        verifier_config = _PromptOverrideConfig(config, prompt)
        summary = vision.describe_live_doorbell(verifier_config, ha_client, door, seconds=2, mode="manual")
    except Exception as exc:
        LOGGER.warning("Live scene verification failed for %s claim: %s", risky_claim, exc)
        return "failed", str(exc)[:160]
    lowered = str(summary or "").lower()
    if "not confirmed" in lowered:
        status = "not confirmed"
    elif "confirmed" in lowered:
        status = "confirmed"
    elif "unclear" in lowered:
        status = "unclear"
    else:
        status = "unknown"
    return status, str(summary or "").strip()


def _verify_latest_scene_if_needed(state, session_id):
    session = get_live_session(session_id)
    risky_claim = str(session.get("scene_risky_claim") or "").strip()
    status = str(session.get("verification_status") or "").strip().lower()
    summary = str(session.get("scene_summary") or state.get("scene_summary") or "").strip()
    if not risky_claim or status not in {"pending", "needed"} or not summary:
        return
    _update_live_session(session_id, verification_status="running")
    verification_status, verification_summary = _verify_live_scene_claim(state, summary, risky_claim)
    _update_live_session(
        session_id,
        verification_status=verification_status,
        verification_summary=verification_summary,
    )
    LOGGER.info(
        "Gemini live scene verification: risky=%s status=%s summary=%r",
        risky_claim,
        verification_status,
        verification_summary,
    )


def _describe_now_fallback_if_needed(state, session_id):
    if not state.get("describe_now_pending"):
        return ""
    started_count = int(state.get("describe_now_part_count") or 0)
    current_count = len(state.get("raw_transcript_parts") or [])
    if current_count > started_count:
        _update_live_session(session_id, last_command="Describe-now answered by Gemini Live.")
        return ""
    config = state.get("config")
    ha_client = state.get("ha_client")
    door = state.get("door")
    if not config or not ha_client or not door:
        _update_live_session(session_id, last_command="Describe-now fallback unavailable.")
        return ""
    try:
        prompt = (
            "Describe the current doorbell camera view now for a blind homeowner. "
            "Use one concise sentence. Say only what is clearly visible. "
            "If nothing meaningful is happening, say: No meaningful change visible."
        )
        fallback_config = _PromptOverrideConfig(config, prompt)
        text = vision.describe_live_doorbell(fallback_config, ha_client, door, seconds=2, mode="manual")
    except Exception as exc:
        LOGGER.warning("Describe-now fallback failed for %s: %s", _door_label(door).lower(), exc)
        _update_live_session(session_id, last_command=f"Describe-now fallback failed: {exc}")
        return ""
    text = str(text or "").strip()
    if not text:
        _update_live_session(session_id, last_command="Describe-now fallback produced no description.")
        return ""
    LOGGER.info("Describe-now fallback produced description for %s: %s", _door_label(door).lower(), text)
    _update_live_session(session_id, last_command="Describe-now fallback answered.")
    return text


class _PromptOverrideConfig:
    def __init__(self, base, prompt):
        self._base = base
        self._prompt = prompt

    def __getattr__(self, name):
        return getattr(self._base, name)

    @property
    def ai_description_styles(self):
        styles = dict(getattr(self._base, "ai_description_styles", {}) or {})
        styles["manual_video"] = "custom"
        return styles

    @property
    def ai_custom_descriptions(self):
        custom = dict(getattr(self._base, "ai_custom_descriptions", {}) or {})
        custom["manual_video"] = self._prompt
        return custom


def _is_useful_live_transcript(text):
    text = str(text or "").strip()
    if not text:
        return False
    words = text.split()
    if len(words) >= 5:
        return True
    if len(text) >= 28:
        return True
    return False


def _is_better_live_transcript(candidate, current):
    candidate = str(candidate or "").strip()
    current = str(current or "").strip()
    if not current:
        return True
    candidate_norm = candidate.lower()
    current_norm = current.lower()
    if candidate_norm == current_norm:
        return False
    if current_norm in candidate_norm and len(candidate) >= len(current):
        return True
    if len(candidate.split()) >= 5 and len(candidate) > len(current) + 8:
        return True
    return False


def _is_live_setup_acknowledgement(text):
    cleaned = " ".join(str(text or "").strip().lower().split())
    if not cleaned:
        return False
    setup_phrases = (
        "understood",
        "i'll watch",
        "i will watch",
        "i'll only speak",
        "i will only speak",
        "only speak when",
        "meaningful to report",
        "meaningful changes",
    )
    scene_words = ("person", "someone", "package", "vehicle", "animal", "faceplate", "removing", "standing")
    return any(phrase in cleaned for phrase in setup_phrases) and not any(word in cleaned for word in scene_words)


def _start_live_video_process(stream_url):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if str(stream_url).lower().startswith("rtsp://"):
        command += ["-rtsp_transport", "tcp"]
    command += [
        "-i",
        str(stream_url),
        "-an",
        "-vf",
        f"fps={LIVE_VIDEO_FPS},scale=640:-2",
        "-q:v",
        "4",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class _MjpegFrameReader:
    def __init__(self, stdout):
        self.stdout = stdout
        self.buffer = bytearray()

    def read_frame(self):
        if not self.stdout:
            return b""
        while True:
            frame = self._pop_frame()
            if frame:
                return frame
            chunk = self.stdout.read(4096)
            if not chunk:
                return b""
            self.buffer.extend(chunk)
            self._trim_buffer()

    def _pop_frame(self):
        start = self.buffer.find(b"\xff\xd8")
        if start < 0:
            return b""
        end = self.buffer.find(b"\xff\xd9", start + 2)
        if end < 0:
            return b""
        end += 2
        frame = bytes(self.buffer[start:end])
        del self.buffer[:end]
        return frame

    def _trim_buffer(self):
        if len(self.buffer) <= LIVE_VIDEO_MAX_BUFFER_BYTES:
            return
        start = self.buffer.find(b"\xff\xd8")
        if start > 0:
            del self.buffer[:start]
        if len(self.buffer) > LIVE_VIDEO_MAX_BUFFER_BYTES:
            del self.buffer[:-LIVE_VIDEO_MAX_BUFFER_BYTES]


def _stop_live_video_process(process):
    if not process:
        return
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
        try:
            if stream:
                stream.close()
        except Exception:
            pass


def _read_process_stderr(process):
    stderr = getattr(process, "stderr", None)
    if not stderr:
        return ""
    try:
        data = stderr.read() or b""
    except Exception:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "ignore").strip()[:300]
    return str(data).strip()[:300]


def _stream_url(config, door):
    if _door_key(door) == "back":
        return str(getattr(config, "back_door_stream_url", "") or "").strip()
    return str(getattr(config, "front_door_stream_url", "") or "").strip()


def _write_live_wav(audio_buffer, prefix="gemini_live", delete_delay=LIVE_REPLAY_SECONDS):
    filename = f"{prefix}_{int(time.time() * 1000)}.wav"
    path = tts.TTS_DIR / filename
    tts.TTS_DIR.mkdir(parents=True, exist_ok=True)
    tts._write_pcm_wav(path, bytes(audio_buffer), 24000)
    tts._copy_to_media(path)
    tts.delete_tts_later(filename, delay_seconds=delete_delay)
    return filename


def _live_audio_url(filename, effective_config):
    return tts.media_url(filename, getattr(effective_config, "external_base_url", ""))


def _play_live_audio_on_speakers(
    ha_client,
    control_state,
    effective_config,
    filename,
    transcript,
    all_speakers,
    sonos_targets=None,
):
    if not control_state:
        return 0
    from . import events
    targets = control_state.speaker_targets("all" if all_speakers else "doorbell")
    media_source = f"media-source://media_source/local/viper_core_tts/{filename}"
    played = 0
    only_sonos = sonos_targets is not None
    for target in [] if only_sonos else targets.get("ha", []):
        try:
            ha_client.call_service(
                "media_player/play_media",
                {
                    "entity_id": target,
                    "media_content_id": media_source,
                    "media_content_type": "audio/wav",
                },
            )
            played += 1
            LOGGER.info("Live doorbell audio accepted by Home Assistant speaker %s using %s.", target, media_source)
        except Exception as exc:
            LOGGER.warning("Live doorbell audio rejected by Home Assistant speaker %s using %s: %s", target, media_source, exc)
    url = tts.media_url(filename, _speaker_media_base_url(effective_config))
    selected_sonos = list(sonos_targets) if sonos_targets is not None else targets.get("sonos", [])
    for target in selected_sonos if url else []:
        try:
            events._play_sonos_url(target, url)
            played += 1
            LOGGER.info("Live doorbell audio accepted by Sonos %s using %s.", target, url)
        except Exception as exc:
            LOGGER.warning("Live doorbell audio rejected by Sonos %s using %s: %s", target, url, exc)
    alexa_targets = [] if only_sonos else targets.get("alexa", [])
    if alexa_targets and transcript:
        try:
            ha_client.call_service(
                getattr(effective_config, "alexa_notify_service", "notify.alexa_media"),
                {
                    "message": transcript,
                    "title": "Viper Live Doorbell",
                    "target": alexa_targets,
                    "data": {"type": "announce"},
                },
            )
            played += len(alexa_targets)
            LOGGER.info("Live doorbell transcript accepted by %s Alexa target(s).", len(alexa_targets))
        except Exception as exc:
            LOGGER.warning("Live doorbell transcript rejected by Alexa targets %s: %s", alexa_targets, exc)
    return played


class _LiveSpeakerStreamer:
    def __init__(self, ha_client, control_state, effective_config, all_speakers, item_queue, session_id=""):
        self.ha_client = ha_client
        self.control_state = control_state
        self.effective_config = effective_config
        self.all_speakers = all_speakers
        self.item_queue = item_queue
        self.session_id = session_id
        self.chunk_seconds = _live_speaker_chunk_seconds(effective_config)
        self.chunk_bytes = 24000 * 2 * self.chunk_seconds
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._queue = queue.Queue()
        self._stop = object()
        self.played = 0
        self.chunk_count = 0
        self.chunk_sonos_targets = None
        self.started_at = time.monotonic()
        self.relay = self._start_relay()
        self.thread = None
        if not self.relay or self.chunk_sonos_targets:
            self.thread = threading.Thread(target=self._worker, name="gemini-live-speaker-audio", daemon=True)
            self.thread.start()
            if self.relay and self.chunk_sonos_targets:
                LOGGER.info(
                    "Live doorbell speaker streamer started radio relay plus %ss Sonos fallback chunks for %s target(s).",
                    self.chunk_seconds,
                    len(self.chunk_sonos_targets),
                )
            else:
                LOGGER.info("Live doorbell speaker streamer started with %ss fallback chunks.", self.chunk_seconds)

    def add(self, data):
        if not data:
            return
        if self.relay:
            self.relay.add(data)
            if not self.thread:
                return
        with self._lock:
            self._buffer.extend(data)
            if len(self._buffer) >= self.chunk_bytes:
                self._enqueue_locked()

    def close(self):
        if self.relay:
            self.relay.close()
            if not self.thread:
                return
        with self._lock:
            self._enqueue_locked()
        self._queue.put(self._stop)
        if self.thread:
            self.thread.join(timeout=12)

    def _start_relay(self):
        base = _speaker_media_base_url(self.effective_config)
        if not self.session_id or not base or not shutil.which("ffmpeg"):
            return None
        relay_url = f"{base.rstrip('/')}/live-audio/{quote(self.session_id, safe='')}.mp3"
        try:
            relay = _LiveAudioRelay(self.session_id)
            with _LIVE_AUDIO_RELAYS_LOCK:
                _LIVE_AUDIO_RELAYS[self.session_id] = relay
            result = _play_live_radio_on_speakers(
                self.ha_client,
                self.control_state,
                self.effective_config,
                relay_url,
                self.all_speakers,
            )
            if isinstance(result, dict):
                targets = result["played"]
                self.chunk_sonos_targets = result["sonos_fallback"]
            else:
                targets = int(result or 0)
                self.chunk_sonos_targets = []
            if not targets:
                relay.close()
                with _LIVE_AUDIO_RELAYS_LOCK:
                    _LIVE_AUDIO_RELAYS.pop(self.session_id, None)
                return None
            self.played = targets
            elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
            message = f"Started live audio relay for {targets} speaker target(s)."
            LOGGER.info("%s URL=%s startup_ms=%s.", message, relay_url, elapsed_ms)
            _update_live_session(
                self.session_id,
                live_audio_url=relay_url,
                live_audio_targets=targets,
                last_speaker_result=message,
            )
            self.item_queue.put({
                "event": "speaker_audio",
                "message": message,
                "url": relay_url,
                "played_targets": targets,
                "timestamp": int(time.time()),
            })
            return relay
        except Exception as exc:
            LOGGER.warning("Live audio relay could not start; using chunk fallback: %s", exc)
            _update_live_session(self.session_id, last_speaker_result=f"Live audio relay could not start; using chunk fallback: {exc}")
            return None

    def _enqueue_locked(self):
        if not self._buffer:
            return
        chunk = bytes(self._buffer)
        self._buffer.clear()
        self._queue.put(chunk)

    def _worker(self):
        while True:
            chunk = self._queue.get()
            if chunk is self._stop:
                return
            try:
                started = time.monotonic()
                self.chunk_count += 1
                chunk_number = self.chunk_count
                filename = _write_live_wav(chunk, prefix="gemini_live_chunk", delete_delay=300)
                if self.relay:
                    count = _play_live_audio_on_speakers(
                        self.ha_client,
                        self.control_state,
                        self.effective_config,
                        filename,
                        "",
                        self.all_speakers,
                        sonos_targets=self.chunk_sonos_targets,
                    )
                else:
                    count = _play_live_audio_on_speakers(
                        self.ha_client,
                        self.control_state,
                        self.effective_config,
                        filename,
                        "",
                        self.all_speakers,
                    )
                self.played += count
                elapsed_ms = int((time.monotonic() - started) * 1000)
                message = f"Played live audio chunk {chunk_number} on {count} speaker target(s) in {elapsed_ms}ms."
                LOGGER.info("%s File=%s bytes=%s chunk_seconds=%s.", message, filename, len(chunk), self.chunk_seconds)
                _update_live_session(
                    self.session_id,
                    speaker_chunks=chunk_number,
                    last_speaker_result=message,
                )
                self.item_queue.put({
                    "event": "speaker_audio",
                    "message": message,
                    "filename": filename,
                    "chunk": chunk_number,
                    "played_targets": count,
                    "elapsed_ms": elapsed_ms,
                    "timestamp": int(time.time()),
                })
            except Exception as exc:
                LOGGER.warning("Live speaker audio chunk failed: %s", exc)
                _update_live_session(self.session_id, last_speaker_result=f"Live speaker audio chunk failed: {exc}")
                self.item_queue.put(_event("warning", f"Live speaker audio chunk failed: {exc}"))


class _LiveAudioRelay:
    HISTORY_BYTES = 256 * 1024

    def __init__(self, session_id):
        self.session_id = str(session_id or "").strip()
        self.subscribers = set()
        self.history = collections.deque()
        self.history_size = 0
        self.remember_startup_audio = True
        self.lock = threading.Lock()
        self.input_lock = threading.Lock()
        self.closed = False
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-vn",
            "-f",
            "mp3",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-write_xing",
            "0",
            "-flush_packets",
            "1",
            "pipe:1",
        ]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.reader = threading.Thread(target=self._read_output, name=f"live-audio-relay-{self.session_id}", daemon=True)
        self.reader.start()
        self.add_silence(LIVE_RADIO_PREROLL_SECONDS)
        self.keepalive = threading.Thread(target=self._keepalive_silence, name=f"live-audio-keepalive-{self.session_id}", daemon=True)
        self.keepalive.start()
        LOGGER.info("Started live audio relay ffmpeg process for session %s with %.1fs preroll.", self.session_id, LIVE_RADIO_PREROLL_SECONDS)

    def add(self, pcm_bytes, startup=False):
        if self.closed or not pcm_bytes or not self.process.stdin:
            return
        if not startup:
            self.remember_startup_audio = False
        try:
            with self.input_lock:
                if self.closed or not self.process.stdin:
                    return
                self.process.stdin.write(pcm_bytes)
                self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            LOGGER.warning("Live audio relay input failed for session %s: %s", self.session_id, exc)
            self.close()

    def add_silence(self, seconds, startup=True):
        try:
            sample_count = max(1, int(float(seconds) * 24000))
        except (TypeError, ValueError):
            sample_count = 12000
        self.add(b"\x00" * sample_count * 2, startup=startup)

    def _keepalive_silence(self):
        while not self.closed:
            time.sleep(LIVE_RADIO_KEEPALIVE_SECONDS)
            if self.closed:
                break
            self.add_silence(LIVE_RADIO_KEEPALIVE_SECONDS, startup=True)

    def stream(self):
        subscriber = queue.Queue(maxsize=200)
        with self.lock:
            if self.closed:
                return
            history = list(self.history)
            self.subscribers.add(subscriber)
        for item in history:
            subscriber.put(item)
        LOGGER.info("Live audio relay client connected for session %s.", self.session_id)
        try:
            while True:
                item = subscriber.get()
                if item is None:
                    break
                yield item
        finally:
            with self.lock:
                self.subscribers.discard(subscriber)
            LOGGER.info("Live audio relay client disconnected for session %s.", self.session_id)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            with self.input_lock:
                if self.process.stdin:
                    self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
        with _LIVE_AUDIO_RELAYS_LOCK:
            if _LIVE_AUDIO_RELAYS.get(self.session_id) is self:
                _LIVE_AUDIO_RELAYS.pop(self.session_id, None)
        self._broadcast(None)
        LOGGER.info("Closed live audio relay for session %s.", self.session_id)

    def _read_output(self):
        try:
            while True:
                data = self.process.stdout.read(4096) if self.process.stdout else b""
                if not data:
                    break
                self._broadcast(data)
        except Exception as exc:
            LOGGER.warning("Live audio relay output failed for session %s: %s", self.session_id, exc)
        finally:
            self._broadcast(None)

    def _broadcast(self, data):
        if data and self.remember_startup_audio:
            self._remember(data)
        with self.lock:
            subscribers = list(self.subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(data)
            except queue.Full:
                LOGGER.warning("Live audio relay subscriber queue full for session %s.", self.session_id)

    def _remember(self, data):
        with self.lock:
            self.history.append(data)
            self.history_size += len(data)
            while self.history_size > self.HISTORY_BYTES and self.history:
                removed = self.history.popleft()
                self.history_size -= len(removed)


def _play_live_radio_on_speakers(ha_client, control_state, effective_config, relay_url, all_speakers):
    if not control_state:
        return {"played": 0, "sonos_fallback": []}
    from . import events
    targets = control_state.speaker_targets("all" if all_speakers else "doorbell")
    played = 0
    sonos_fallback = []
    for target in targets.get("sonos", []):
        try:
            events._play_sonos_url(
                target,
                relay_url,
                tolerate_set_timeout=True,
                set_timeout=3,
                radio_title="Viper Doorbell Live",
                tolerate_play_timeout=True,
                play_timeout=3,
            )
            played += 1
            LOGGER.info("Live audio relay accepted by Sonos %s using %s.", target, relay_url)
        except Exception as exc:
            sonos_fallback.append(target)
            LOGGER.warning("Live audio relay rejected by Sonos %s using %s: %s", target, relay_url, exc)
    ha_targets = list(targets.get("ha", []))
    for target in ha_targets:
        threading.Thread(
            target=_play_live_radio_on_ha_speaker,
            args=(ha_client, target, relay_url),
            name=f"live-radio-ha-{target}",
            daemon=True,
        ).start()
    played += len(ha_targets)
    alexa_targets = targets.get("alexa", [])
    if alexa_targets:
        LOGGER.info("Live audio relay skipped %s Alexa target(s); Alexa announcements cannot subscribe to a raw Viper audio stream.", len(alexa_targets))
    return {"played": played, "sonos_fallback": sonos_fallback}


def _play_live_radio_on_ha_speaker(ha_client, target, relay_url):
    try:
        started = time.monotonic()
        ha_client.call_service(
            "media_player/play_media",
            {
                "entity_id": target,
                "media_content_id": relay_url,
                "media_content_type": "audio/mpeg",
            },
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        LOGGER.info("Live audio relay accepted by Home Assistant speaker %s using %s in %sms.", target, relay_url, elapsed_ms)
    except Exception as exc:
        LOGGER.warning("Live audio relay rejected by Home Assistant speaker %s using %s: %s", target, relay_url, exc)


def sse_encode(item):
    event = str((item or {}).get("event") or "message").strip() or "message"
    data = json.dumps(item or {}, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode("utf-8")


def _event(event, message):
    return {"event": event, "message": str(message or ""), "timestamp": int(time.time())}


def _door_key(value):
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return "back" if text.startswith("back") else "front"


def _door_label(door):
    return "Back door" if _door_key(door) == "back" else "Front door"


def _door_alert_message(door, message):
    text = _door_specific_text(door, message)
    if not text:
        return ""
    label = _door_label(door)
    if text.lower().startswith(label.lower()):
        return text
    return f"{label}: {text}"


def _speaker_media_base_url(config):
    base = str(getattr(config, "speaker_base_url", "") or "").strip().rstrip("/")
    if base:
        return base
    base = str(getattr(config, "external_base_url", "") or "").strip().rstrip("/")
    if base.startswith("http://100.") or base.startswith("https://100."):
        return "http://homeassistant.local:8099"
    return base


def _live_speaker_chunk_seconds(config):
    try:
        seconds = int(float(getattr(config, "live_speaker_chunk_seconds", LIVE_SPEAKER_CHUNK_SECONDS) or LIVE_SPEAKER_CHUNK_SECONDS))
    except (TypeError, ValueError):
        seconds = LIVE_SPEAKER_CHUNK_SECONDS
    return max(1, min(5, seconds))


def _door_specific_text(door, message):
    text = str(message or "").strip()
    if _door_key(door) == "back":
        return text.replace("Front door", "Back door").replace("front door", "back door")
    return text.replace("Back door", "Front door").replace("back door", "front door")


def _meaningfully_new(previous, current):
    if not current:
        return False
    previous_key = _normalize(previous)
    current_key = _normalize(current)
    if not previous_key:
        return True
    return previous_key != current_key


def _normalize(value):
    return " ".join(str(value or "").strip().lower().split())


def _clamped_int(value, fallback, minimum, maximum):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        try:
            parsed = int(float(fallback))
        except (TypeError, ValueError):
            parsed = minimum
    return max(minimum, min(maximum, parsed))
