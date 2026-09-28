import json
import logging
import mimetypes
import time
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import tts
from . import live
from .web_ui import render_page


LOGGER = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, host, port, state_provider, event_handler=None, control_handler=None, live_handler=None):
        self._server = ThreadingHTTPServer(
            (host, port),
            self._make_handler(state_provider, event_handler, control_handler, live_handler),
        )

    def serve_forever(self):
        self._server.serve_forever()

    def shutdown(self):
        self._server.shutdown()

    @staticmethod
    def _make_handler(state_provider, event_handler, control_handler, live_handler):
        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                parsed = urlparse(self.path)
                path = _viper_path(parsed.path)
                if path.startswith("/chimes/") and control_handler:
                    chime_path = control_handler.chime_path(unquote(path.rsplit("/", 1)[-1]))
                    if chime_path:
                        self._send_file(chime_path, head_only=True)
                        return
                if path.startswith("/tts/"):
                    tts_path = tts.tts_path(unquote(path.rsplit("/", 1)[-1]))
                    if tts_path:
                        self._send_file(tts_path, head_only=True)
                        return
                self._send_json({"ok": False, "message": "Not found."}, code=404)

            def do_GET(self):
                parsed = urlparse(self.path)
                path = _viper_path(parsed.path)
                if path == "/":
                    state = state_provider()
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    self._send_html(render_page(state, (query.get("page") or ["dashboard"])[-1]))
                    return
                if path.startswith("/ui/"):
                    page = path[len("/ui/") :].strip("/") or "dashboard"
                    if "/" not in page:
                        state = state_provider()
                        self._send_html(render_page(state, page))
                        return
                if path == "/health":
                    state = state_provider()
                    self._send_json(state)
                    return
                if path == "/ready":
                    state = state_provider()
                    code = 200 if state.get("home_assistant", {}).get("ok") else 503
                    self._send_json(state, code=code)
                    return
                if path.startswith("/api/live/doorbell/") and live_handler:
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    door = path.rsplit("/", 1)[-1]
                    self._send_sse(live_handler(door, query))
                    return
                if path.startswith("/doorbell/session/"):
                    session_id = unquote(path.rsplit("/", 1)[-1])
                    self._send_html(_doorbell_session_page(session_id))
                    return
                if path.startswith("/live-audio/"):
                    name = unquote(path.rsplit("/", 1)[-1])
                    session_id = name[:-4] if name.endswith(".mp3") else name
                    stream = live.live_audio_stream(session_id)
                    if stream:
                        self._send_audio_stream(stream)
                        return
                    self._send_json({"ok": False, "message": "Live audio stream is not available."}, code=404)
                    return
                if path.startswith("/doorbell-debug/") and control_handler:
                    debug_name = unquote(path[len("/doorbell-debug/") :])
                    debug_path = control_handler.diagnostic_file_path(debug_name)
                    if debug_path:
                        self._send_file(debug_path)
                        return
                    self._send_json({"ok": False, "message": "Doorbell debug file is not available."}, code=404)
                    return
                if path.startswith("/chimes/") and control_handler:
                    chime_path = control_handler.chime_path(unquote(path.rsplit("/", 1)[-1]))
                    if chime_path:
                        self._send_file(chime_path)
                        return
                if path.startswith("/tts/"):
                    tts_path = tts.tts_path(unquote(path.rsplit("/", 1)[-1]))
                    if tts_path:
                        self._send_file(tts_path)
                        return
                if control_handler:
                    result = control_handler.handle_get(path)
                    if result is not None:
                        self._send_json(result)
                        return
                self._send_json({"ok": False, "message": "Not found."}, code=404)

            def do_POST(self):
                parsed = urlparse(self.path)
                path = _viper_path(parsed.path)
                parts = [item for item in path.strip("/").split("/") if item]
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._send_json({"ok": False, "message": "Invalid Content-Length."}, code=400)
                    return
                if length < 0:
                    self._send_json({"ok": False, "message": "Invalid Content-Length."}, code=400)
                    return
                raw_bytes = self.rfile.read(length) if length else b""
                if path.startswith("/ui/"):
                    self._handle_ui_post(path, raw_bytes)
                    return
                content_type = self.headers.get("Content-Type", "")
                if content_type.startswith("application/x-www-form-urlencoded"):
                    payload = _form_payload(raw_bytes)
                else:
                    raw = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else "{}"
                    try:
                        payload = json.loads(raw or "{}")
                    except json.JSONDecodeError:
                        self._send_json({"ok": False, "message": "Request body must be JSON."}, code=400)
                        return
                if not isinstance(payload, dict):
                    self._send_json({"ok": False, "message": "Request body must be a JSON object."}, code=400)
                    return
                if path == "/api/test/pushover" and event_handler:
                    result = event_handler(
                        "pushover_test",
                        {
                            "title": payload.get("title") or "Viper Core Test",
                            "message": payload.get("message") or "Viper Core Pushover is working.",
                        },
                    )
                    self._send_json(result, code=200 if result.get("ok") else 400)
                    return
                if control_handler:
                    result = control_handler.handle_post(path, payload)
                    if result is not None:
                        code = 200 if result.get("ok", True) else (503 if path in ("/api/setup/front-test", "/api/setup/back-test") else 404)
                        self._send_json(result, code=code)
                        return
                if not event_handler:
                    self._send_json({"ok": False, "message": "Event handling is not configured."}, code=503)
                    return
                if len(parts) < 2 or parts[0] != "event":
                    legacy = _legacy_event(path, payload)
                    if not legacy:
                        LOGGER.warning("Unknown endpoint: raw path=%s normalized path=%s payload=%s", self.path, path, payload)
                        self._send_json({"ok": False, "message": "Unknown endpoint."}, code=404)
                        return
                    event_type, payload = legacy
                else:
                    event_type = parts[1]
                result = event_handler(event_type, payload)
                self._send_json(result, code=200 if result.get("ok") else 400)

            def log_message(self, format, *args):
                return

            def _handle_ui_post(self, path, raw_bytes):
                if not control_handler:
                    self._redirect("/")
                    return
                wants_json = str(self.headers.get("X-Viper-Async") or "").lower() == "true"
                if path == "/ui/chimes/upload":
                    filename, content = _multipart_file(raw_bytes, self.headers.get("Content-Type", ""))
                    result = control_handler.upload_chime(filename, content)
                    self._send_html(_simple_page("Chime Upload", f'<p role="status">{_html(result.get("message") or "Upload complete.")}</p><a href="{_html(self._return_path())}">Return to controls</a>'),
                                    code=200 if result.get("ok") else 400)
                    return
                if path == "/ui/chimes/upload-folder":
                    results = [control_handler.upload_chime(filename, content) for filename, content in _multipart_files(raw_bytes, self.headers.get("Content-Type", ""))]
                    message = " ".join(result.get("message") or "Upload complete." for result in results) or "No files were selected."
                    self._send_html(_simple_page("Chime Upload", f'<p role="status">{_html(message)}</p><a href="{_html(self._return_path())}">Return to controls</a>'))
                    return
                payload = _form_payload(raw_bytes)
                try:
                    result = _ui_action(path, payload, control_handler, event_handler)
                except ValueError as exc:
                    result = {"ok": False, "message": str(exc)}
                if result is None:
                    LOGGER.warning("Unknown UI action: raw path=%s normalized path=%s payload=%s", self.path, path, payload)
                    self._send_json({"ok": False, "message": "Unknown UI action."}, code=404)
                    return
                if wants_json:
                    self._send_json(result, code=200 if result.get("ok", True) else 400)
                    return
                message = result.get("message") or ("Saved." if result.get("ok", True) else "Command failed.")
                self._send_html(_simple_page("Command Result", f'<p role="status">{_html(message)}</p><a href="{_html(self._return_path())}">Return to controls</a>'),
                                code=200 if result.get("ok", True) else 400)

            def _send_json(self, payload, code=200):
                body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.debug("Client disconnected while streaming %s.", self.path)

            def _send_html(self, html, code=200):
                base = "/"
                ingress = self.headers.get("X-Ingress-Path") or urlparse(self.path).path
                parts = ingress.strip("/").split("/")
                if len(parts) >= 3 and parts[:2] == ["api", "hassio_ingress"]:
                    base = "/" + "/".join(parts[:3]) + "/"
                markup = str(html or "")
                markup = markup.replace('action="/ui/', f'action="{_html(base)}ui/')
                markup = markup.replace('action="ui/', f'action="{_html(base)}ui/')
                body = markup.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.debug("Client disconnected while streaming HTML.")

            def _send_file(self, path, head_only=False):
                try:
                    file_size = path.stat().st_size
                except OSError:
                    self._send_json({"ok": False, "message": "File is not available."}, code=404)
                    return
                content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
                range_header = self.headers.get("Range") or ""
                byte_range = _parse_byte_range(range_header, file_size) if range_header else None
                if range_header and byte_range is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if byte_range:
                    start, end = byte_range
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                else:
                    start, end = 0, max(0, file_size - 1)
                    length = file_size
                    self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "public, max-age=300")
                self.send_header("Connection", "close")
                self.end_headers()
                if head_only:
                    return
                try:
                    with path.open("rb") as file_obj:
                        file_obj.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk = file_obj.read(min(64 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.debug("Client disconnected while streaming %s.", path)

            def _send_sse(self, events):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for item in events:
                        self.wfile.write(live.sse_encode(item))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.debug("Client disconnected from live stream.")
                finally:
                    self.close_connection = True

            def _send_audio_stream(self, chunks):
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Accept-Ranges", "none")
                self.send_header("TransferMode.DLNA.ORG", "Streaming")
                self.send_header(
                    "ContentFeatures.DLNA.ORG",
                    "DLNA.ORG_PN=MP3;DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000",
                )
                self.send_header("icy-name", "Viper Doorbell Live")
                self.send_header("icy-metaint", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for chunk in chunks:
                        if chunk:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    LOGGER.debug("Client disconnected from live audio stream.")

            def _redirect(self, location):
                self.send_response(303)
                self.send_header("Location", location)
                self.end_headers()

            def _return_path(self):
                referer = self.headers.get("Referer") or "/"
                parsed_referer = urlparse(referer)
                normalized = _viper_path(parsed_referer.path)
                if normalized == "/" or normalized.startswith("/ui/"):
                    return parsed_referer.path + (f"?{parsed_referer.query}" if parsed_referer.query else "")
                return "/"

        return Handler


def _legacy_event(path, payload):
    normalized = str(path or "").strip().lower().rstrip("/")
    if normalized in {"/remote/broadcast", "/remote/broadcast_push"}:
        data = dict(payload or {})
        data["push"] = normalized.endswith("broadcast_push")
        data["message"] = data.get("message") or data.get("broadcast_text") or ""
        return "broadcast", data
    if normalized == "/cinderella":
        return "vacuum", dict(payload or {})
    if normalized in {"/doorbell-webhook", "/doorbell-webhook/front"}:
        data = dict(payload or {})
        data.setdefault("door", "front")
        data.setdefault("action", "pressed")
        return "doorbell", data
    if normalized == "/doorbell-webhook/back":
        data = dict(payload or {})
        data.setdefault("door", "back")
        data.setdefault("action", "pressed")
        return "doorbell", data
    return None


def _viper_path(path):
    path = str(path or "/")
    if path in {"", "/"}:
        return "/"
    if path.startswith("/api/hassio_ingress/"):
        parts = [item for item in path.split("/") if item]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "hassio_ingress":
            remainder = "/" + "/".join(parts[3:])
            return remainder or "/"
    for marker in ("/ui/", "/event/", "/api/live/", "/doorbell/session/", "/live-audio/", "/chimes/", "/tts/"):
        index = path.find(marker)
        if index >= 0:
            return path[index:]
    return path


def _parse_byte_range(range_header, file_size):
    text = str(range_header or "").strip().lower()
    if not text.startswith("bytes=") or "," in text:
        return None
    spec = text[len("bytes=") :].strip()
    if "-" not in spec or file_size < 0:
        return None
    start_text, end_text = spec.split("-", 1)
    try:
        if start_text == "":
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return None
            start = max(0, file_size - suffix_length)
            end = max(0, file_size - 1)
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= file_size:
        return None
    return start, min(end, file_size - 1)


def _doorbell_session_page(session_id):
    session = live.get_live_session(session_id)
    if not session:
        return _simple_page(
            "Doorbell Narration",
            "<p>This doorbell narration is no longer available.</p>"
            "<p>Viper keeps live narration audio for one hour.</p>",
        )
    door = str(session.get("door") or "front")
    label = "Back Door" if door == "back" else "Front Door"
    audio_url = str(session.get("audio_url") or "")
    status = str(session.get("status") or "starting")
    message = str(session.get("message") or "")
    started = _format_timestamp(session.get("started_at"))
    completed = _format_timestamp(session.get("completed_at"))
    expires = _format_timestamp(session.get("expires_at"))
    expires_in = _expires_in(session.get("expires_at"))
    speaker_chunks = str(session.get("speaker_chunks") or 0)
    live_audio_url = str(session.get("live_audio_url") or "")
    live_audio_targets = str(session.get("live_audio_targets") or 0)
    frames_sent = str(session.get("video_frames_sent") or 0)
    frames_discarded = str(session.get("video_frames_discarded") or 0)
    visual_prompts = str(session.get("visual_prompts_sent") or 0)
    raw_transcript = str(session.get("raw_transcript") or "")
    scene_change = str(session.get("scene_change") or "unknown")
    scene_summary = str(session.get("scene_summary") or "")
    scene_confidence = str(session.get("scene_confidence") or "unknown")
    risky_claim = str(session.get("scene_risky_claim") or "none")
    verification_status = str(session.get("verification_status") or "not needed")
    verification_summary = str(session.get("verification_summary") or "")
    debug_contact_sheet_url = str(session.get("debug_contact_sheet_url") or "")
    debug_frame_count = str(session.get("debug_frame_count") or 0)
    speaker_result = str(session.get("last_speaker_result") or "No speaker audio chunks have been reported yet.")
    debug_link = f'<p><a href="{_html(debug_contact_sheet_url)}">Open exact frames sent to AI</a></p>' if debug_contact_sheet_url else ""
    details = (
        f"<p><strong>Status:</strong> {_html(status)}</p>"
        f"<p><strong>Started:</strong> {_html(started)}</p>"
        f"<p><strong>Completed:</strong> {_html(completed)}</p>"
        f"<p><strong>Live audio stream:</strong> {_html(live_audio_url or 'not active')}</p>"
        f"<p><strong>Live stream speaker targets:</strong> {_html(live_audio_targets)}</p>"
        f"<p><strong>Video frames sent to AI:</strong> {_html(frames_sent)}</p>"
        f"<p><strong>Warmup frames discarded:</strong> {_html(frames_discarded)}</p>"
        f"<p><strong>Visual check prompts sent:</strong> {_html(visual_prompts)}</p>"
        f"<p><strong>Raw AI words:</strong> {_html(raw_transcript or 'not reported yet')}</p>"
        f"<p><strong>Scene change:</strong> {_html(scene_change)}</p>"
        f"<p><strong>Scene memory:</strong> {_html(scene_summary or 'not reported yet')}</p>"
        f"<p><strong>Confidence:</strong> {_html(scene_confidence)}</p>"
        f"<p><strong>Risky claim:</strong> {_html(risky_claim)}</p>"
        f"<p><strong>Verification:</strong> {_html(verification_status)}</p>"
        f"<p><strong>Verification summary:</strong> {_html(verification_summary or 'not reported yet')}</p>"
        f"<p><strong>Saved live frames:</strong> {_html(debug_frame_count)}</p>"
        f"{debug_link}"
        f"<p><strong>Speaker chunks:</strong> {_html(speaker_chunks)}</p>"
        f"<p><strong>Last speaker result:</strong> {_html(speaker_result)}</p>"
    )
    if audio_url and status == "replay":
        return _simple_page(
            f"{label} Narration Replay",
            details
            +
            f"<p>{_html(message or 'The saved narration is ready.')}</p>"
            f'<audio controls autoplay src="{_html(audio_url)}" style="width:100%;max-width:620px"></audio>'
            f'<p><a href="{_html(audio_url)}">Open audio file directly</a></p>'
            f"<p><strong>Replay expires:</strong> {_html(expires)} ({_html(expires_in)}).</p>",
        )
    query = (
        f"page=doorbells&live={_html(door)}&mode=gemini_true_live&speak=browser"
        f"&session_id={_html(session_id)}"
    )
    return _simple_page(
        f"{label} Live Narration",
        details
        +
        f"<p>{_html(message or 'Live narration is still running or starting.')}</p>"
        "<p>If this page does not move automatically, use the link below.</p>"
        f'<p><a href="/?{query}">Open live narration</a></p>'
        f'<script>window.setTimeout(function(){{ window.location.href = "/?{query}"; }}, 600);</script>',
    )


def _simple_page(title, body):
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(title)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #f6f7f8; color: #171717; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 24px; }}
    a, audio {{ display: block; margin: 12px 0; }}
      main {{ overflow-wrap: anywhere; }}
      audio {{ max-width: 100%; }}
      a {{ min-height: 24px; }}
      :focus-visible {{ outline: 3px solid #005fcc; outline-offset: 3px; }}
  </style>
</head>
<body><main><h1>{_html(title)}</h1>{body}</main></body>
</html>"""


def _html(value):
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _format_timestamp(value):
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    if not timestamp:
        return "not yet"
    return time.strftime("%Y-%m-%d %I:%M:%S %p", time.localtime(timestamp))


def _expires_in(value):
    try:
        seconds = int(value or 0) - int(time.time())
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return "expired"
    minutes = max(1, int((seconds + 59) / 60))
    return f"in about {minutes} minute{'s' if minutes != 1 else ''}"


def _form_payload(raw_bytes):
    parsed = parse_qs(raw_bytes.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values if len(values) > 1 else values[-1] if values else "" for key, values in parsed.items()}


def _ui_action(path, payload, control_handler, event_handler):
    if path.startswith("/ui/setup/"):
        return control_handler.handle_post(path.replace("/ui/", "/api/", 1), payload)
    if path == "/ui/control/armed":
        return control_handler.handle_post("/api/control/armed", payload)
    if path == "/ui/control/global_mute":
        return control_handler.handle_post("/api/control/global_mute", payload)
    if path == "/ui/control/ice_maker":
        return control_handler.handle_post("/api/control/ice_maker/enabled", payload)
    if path == "/ui/speakers":
        return control_handler.handle_post("/api/control/speakers", payload)
    if path.startswith("/ui/speakers/") and path.endswith("/enabled"):
        name = path[len("/ui/speakers/") : -len("/enabled")]
        return control_handler.handle_post(f"/api/control/speakers/{name}/enabled", payload)
    if path.startswith("/ui/speakers/") and path.endswith("/delete"):
        name = path[len("/ui/speakers/") : -len("/delete")]
        return control_handler.handle_post(f"/api/control/speakers/{name}/delete", payload)
    if path.startswith("/ui/speakers/") and path.endswith("/route"):
        name = path[len("/ui/speakers/") : -len("/route")]
        route_state = str(payload.get("route_state") or "")
        route, _sep, state = route_state.partition(":")
        return control_handler.handle_post(f"/api/control/speakers/{name}/route", {"route": route, "state": state})
    if path == "/ui/chimes/assign":
        return control_handler.handle_post("/api/control/chimes", payload)
    if path == "/ui/settings":
        return control_handler.handle_post("/api/control/settings", payload)
    if path == "/ui/voice/settings":
        return control_handler.handle_post("/api/control/settings", payload)
    if path == "/ui/voice/test" and event_handler:
        control_handler.handle_post("/api/control/settings", payload)
        message = payload.get("message") or "Viper Core voice test is working."
        return event_handler("voice_test", {"message": message, "channel": "utilities"})
    if path == "/ui/hvac":
        return control_handler.handle_post("/api/control/hvac", payload)
    if path == "/ui/vacuum":
        return control_handler.handle_post("/api/control/vacuum", payload)
    if path == "/ui/vacuum/control":
        return control_handler.handle_post("/api/control/vacuum/entity", payload)
    if path == "/ui/vacuum/rooms":
        return control_handler.handle_post("/api/control/vacuum/rooms", payload)
    if path == "/ui/vacuum/room_clean":
        return control_handler.handle_post("/api/control/vacuum/room_clean", payload)
    if path == "/ui/chimes/delete":
        return control_handler.handle_post("/api/chimes/delete", payload)
    if path.rstrip("/") == "/ui/chimes/test" and event_handler:
        event_name = str(payload.get("event") or "").strip().lower()
        category = "fridge" if event_name.startswith(("fridge", "freezer")) else "doorbell" if event_name.endswith("doorbell") else "utilities"
        return event_handler("chime", {"filename": payload.get("filename", ""), "category": category, "event": event_name})
    if path == "/ui/test/doorbell/front" and event_handler:
        return event_handler("doorbell", {"door": "front", "action": "pressed", "source": "manual_web", "test": True})
    if path == "/ui/test/doorbell/back" and event_handler:
        return event_handler("doorbell", {"door": "back", "action": "pressed", "source": "manual_web", "test": True})
    if path == "/ui/test/doorbell_video/front" and event_handler:
        return event_handler("doorbell_video", {"door": "front", "source": "manual_web"})
    if path == "/ui/test/doorbell_video/back" and event_handler:
        return event_handler("doorbell_video", {"door": "back", "source": "manual_web"})
    if path == "/ui/test/fridge/fridge" and event_handler:
        return event_handler("fridge", {"appliance": "fridge", "state": "open"})
    if path == "/ui/test/fridge/fridge_closed" and event_handler:
        return event_handler("fridge", {"appliance": "fridge", "state": "closed"})
    if path == "/ui/test/fridge/freezer" and event_handler:
        return event_handler("fridge", {"appliance": "freezer", "state": "open"})
    if path == "/ui/test/fridge/freezer_closed" and event_handler:
        return event_handler("fridge", {"appliance": "freezer", "state": "closed"})
    if path == "/ui/test/pushover" and event_handler:
        return event_handler("pushover_test", {"title": "Viper Core Test", "message": "Viper Core Pushover is working."})
    if path == "/ui/broadcast" and event_handler:
        return event_handler("broadcast", {"message": payload.get("message", ""), "channel": "manual"})
    return None


def _multipart_file(body, content_type):
    files = _multipart_files(body, content_type)
    return files[0] if files else ("", b"")


def _multipart_files(body, content_type):
    if "multipart/form-data" not in str(content_type or ""):
        return []
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    files = []
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if field_name not in {"file", "files"}:
            continue
        files.append((part.get_filename() or "", part.get_payload(decode=True) or b""))
    return files
