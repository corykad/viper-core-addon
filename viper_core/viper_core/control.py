import json
import logging
import math
import os
import re
import shutil
import time
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

from . import live
from .config import DEFAULT_OPENAI_VISION_MODEL, normalize_openai_vision_model
from .features import FEATURES, DOORBELL_FEATURES


LOGGER = logging.getLogger(__name__)
CONTROL_STATE_PATH = Path("/data/control_state.json")
CHIMES_DIR = Path("/data/chimes")
MEDIA_CHIMES_DIR = Path("/media/viper_core_chimes")
ALLOWED_CHIME_SUFFIXES = {".mp3", ".wav", ".ogg", ".m4a"}
HEAT_PUMP_UNITS = [
    ("climate.office_heat_pump_alexa", "Office"),
    ("climate.living_room_heat_pump_alexa", "Living Room"),
    ("climate.kitchen_heat_pump_alexa", "Kitchen"),
    ("climate.jamie_s_room_heat_pump_alexa", "Jamie's Room"),
    ("climate.master_bedroom_heat_pump_alexa", "Master Bedroom"),
]
HEAT_PUMP_CLIMATES = [entity_id for entity_id, _name in HEAT_PUMP_UNITS]
HEAT_PUMP_AIRFLOW = [
    ("fan.office_airflow", "Airflow Office"),
    ("fan.living_room_airflow", "Airflow Living Room"),
    ("fan.kitchen_airflow", "Airflow Kitchen"),
    ("fan.jamie_s_room_airflow", "Airflow Jamie's Room"),
    ("fan.master_bedroom_airflow", "Airflow Master Bedroom"),
]
VACUUM_ENTITY = "vacuum.cinderella"
VACUUM_STATUS_ENTITY = "sensor.cinderella_status"
VACUUM_CONTROL_DOMAINS = {"select", "number", "switch", "button", "sensor", "binary_sensor", "fan"}
VACUUM_CONFIRM_TIMEOUT_SECONDS = 3.0
VACUUM_CONFIRM_POLL_SECONDS = 0.35
DOCK_EMPTY_MODE_VALUES = {
    "smart": 0,
    "light": 1,
    "balanced": 2,
    "max": 4,
}
GEMINI_TTS_VOICES = [
    "Zephyr",
    "Puck",
    "Charon",
    "Kore",
    "Fenrir",
    "Leda",
    "Orus",
    "Aoede",
    "Callirrhoe",
    "Autonoe",
    "Enceladus",
    "Iapetus",
    "Umbriel",
    "Algieba",
    "Despina",
    "Erinome",
    "Algenib",
    "Rasalgethi",
    "Laomedeia",
    "Achernar",
    "Alnilam",
    "Schedar",
    "Gacrux",
    "Pulcherrima",
    "Achird",
    "Zubenelgenubi",
    "Vindemiatrix",
    "Sadachbia",
    "Sadaltager",
    "Sulafat",
]
OPENAI_TTS_VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar"]
CINDERELLA_MESSAGE_BUCKETS = (
    "departure",
    "washing",
    "emptying",
    "drying",
    "returning",
    "victory",
    "paused",
    "status_update",
    "vacuum_error_templates",
    "dock_error_templates",
)
DEFAULT_CINDERELLA_MESSAGES = {
    "departure": [
        "The floor goblin has been released.",
        "Dust, your time has come.",
        "Cinderella has chosen violence against dirt.",
    ],
    "washing": [
        "Mop spa day has begun.",
        "She is rinsing off the evidence.",
        "The mop is being reborn.",
    ],
    "emptying": [
        "Dumping today's bad decisions.",
        "The dirt vault is full.",
        "She is disposing of the evidence.",
    ],
    "drying": [
        "Drying cycle engaged.",
        "The mop is becoming socially acceptable again.",
        "Moisture is being aggressively removed.",
    ],
    "returning": [
        "Returning home like she pays rent.",
        "Cinderella is done being brave.",
        "Retreating with dignity... barely.",
    ],
    "victory": [
        "The floor has been defeated.",
        "Cinderella demands recognition.",
        "Victory has been achieved.",
    ],
    "paused": [
        "Paused for existential reasons.",
        "Cinderella is buffering.",
        "The robot has stopped and is judging silently.",
    ],
    "status_update": [
        "Cinderella has entered a weird little robot state.",
        "The vacuum has changed modes and would like attention.",
        "Cinderella reports a status change from the floor front.",
    ],
    "vacuum_error_templates": [
        "Cinderella has entered her villain arc. Error: {error}.",
        "The robot is having a moment. Error: {error}.",
        "This is not going well. Error: {error}.",
    ],
    "dock_error_templates": [
        "The dock is being weird again. Problem: {error}.",
        "Cinderella's parking spot has opinions. Problem: {error}.",
        "Dock drama detected. Issue: {error}.",
    ],
    "specific_errors": {},
}


DEFAULT_SPEAKERS = {
    "entry way speaker": {
        "id": "media_player.entryway_speaker",
        "type": "ha",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "office sonos": {
        "id": "192.168.4.34",
        "type": "sonos",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "cory's sonos beam": {
        "id": "media_player.cory_s_sonos_beam",
        "type": "alexa",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "bathroom": {
        "id": "media_player.bathroom",
        "type": "alexa",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "master bedroom": {
        "id": "media_player.daisy_cory_master_bedroom",
        "type": "alexa",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "daisies loft echo": {
        "id": "media_player.daisies_loft_echo",
        "type": "alexa",
        "enabled": True,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
    "office": {
        "id": "media_player.kitchen_3",
        "type": "alexa",
        "enabled": False,
        "doorbell": True,
        "fridge": True,
        "utilities": True,
    },
}


class ControlState:
    def __init__(self, path=CONTROL_STATE_PATH, profile=None):
        self.path = Path(path)
        self.profile = profile
        self.state = self._load()

    def feature_enabled(self, name):
        return bool(self.state.get("features", DOORBELL_FEATURES).get(name, False))

    def public_state(self):
        state = deepcopy(self.state)
        state["ready"] = True
        state["chimes"]["available"] = self.available_chimes()
        settings = state.setdefault("settings", {})
        settings["gemini_configured"] = bool(settings.get("gemini_api_key"))
        settings["openai_configured"] = bool(settings.get("openai_api_key"))
        settings["pushover_configured"] = bool(settings.get("pushover_user_key") and settings.get("pushover_api_token"))
        settings.pop("gemini_api_key", None)
        settings.pop("openai_api_key", None)
        settings.pop("pushover_user_key", None)
        settings.pop("pushover_api_token", None)
        for key in ("front_door_stream_url", "back_door_stream_url"):
            settings[key] = "configured" if settings.get(key) else ""
        return state

    def effective_config(self, base_config):
        settings = self.state.get("settings") or {}
        base = getattr(base_config, "__dict__", {})
        return SimpleNamespace(
            **{
                **base,
                "enabled_features": dict(self.state.get("features", DOORBELL_FEATURES)),
                "tts_entity": settings.get("tts_entity", ""),
                "external_base_url": settings.get("external_base_url") or getattr(base_config, "external_base_url", ""),
                "speaker_base_url": settings.get("speaker_base_url") or getattr(base_config, "speaker_base_url", "") or _speaker_base_url(settings.get("external_base_url") or getattr(base_config, "external_base_url", "")),
                "gemini_api_key": settings.get("gemini_api_key") or getattr(base_config, "gemini_api_key", ""),
                "openai_api_key": settings.get("openai_api_key") or getattr(base_config, "openai_api_key", ""),
                "pushover_user_key": settings.get("pushover_user_key") or getattr(base_config, "pushover_user_key", ""),
                "pushover_api_token": settings.get("pushover_api_token") or getattr(base_config, "pushover_api_token", ""),
                "ai_provider": settings.get("ai_provider") or "openai",
                "gemini_vision_model": settings.get("gemini_vision_model") or getattr(base_config, "gemini_vision_model", "gemini-3.5-flash"),
                "gemini_live_model": settings.get("gemini_live_model") or "gemini-3.1-flash-live-preview",
                "openai_vision_model": normalize_openai_vision_model(
                    settings.get("openai_vision_model") or getattr(base_config, "openai_vision_model", "")
                ),
                "front_door_stream_url": settings.get("front_door_stream_url") or "",
                "back_door_stream_url": settings.get("back_door_stream_url") or "",
                "front_door_video_source": settings.get("front_door_video_source") or "rtsp",
                "back_door_video_source": settings.get("back_door_video_source") or "rtsp",
                "front_door_camera_entity": settings.get("front_door_camera_entity") or "",
                "back_door_camera_entity": settings.get("back_door_camera_entity") or "",
                "front_door_live_stream_switch": settings.get("front_door_live_stream_switch") or "",
                "back_door_live_stream_switch": settings.get("back_door_live_stream_switch") or "",
                "front_door_photo_prompt": settings.get("front_door_photo_prompt") or "",
                "back_door_photo_prompt": settings.get("back_door_photo_prompt") or "",
                "doorbell_video_prompt": settings.get("doorbell_video_prompt") or "",
                "ai_description_styles": dict(settings.get("ai_description_styles") or {}),
                "ai_custom_descriptions": dict(settings.get("ai_custom_descriptions") or {}),
                "doorbell_video_mode": settings.get("doorbell_video_mode") or "fast",
                "doorbell_live_video_seconds": int(settings.get("doorbell_live_video_seconds") or 30),
                "live_speaker_chunk_seconds": int(settings.get("live_speaker_chunk_seconds") or 2),
                "doorbell_live_video_frames": int(settings.get("doorbell_live_video_frames") or 4),
                "doorbell_dedupe_seconds": int(settings.get("doorbell_dedupe_seconds") or 30),
                "fridge_stale_minutes": int(settings.get("fridge_stale_minutes") or 45),
                "vacuum_repeat_quiet_minutes": int(settings.get("vacuum_repeat_quiet_minutes") or 20),
                "vacuum_announce_events": list(settings.get("vacuum_announce_events") or []),
                "cinderella_messages": _normalize_cinderella_messages(settings.get("cinderella_messages")),
                "tts_engine": settings.get("tts_engine") or "home_assistant",
                "gemini_tts_model": settings.get("gemini_tts_model") or "gemini-3.1-flash-tts-preview",
                "gemini_tts_voice": settings.get("gemini_tts_voice") or "Sulafat",
                "gemini_tts_speed": settings.get("gemini_tts_speed") or "fast",
                "gemini_tts_style": settings.get("gemini_tts_style") or "warm, clear, friendly",
                "gemini_tts_keep_warm": bool(settings.get("gemini_tts_keep_warm", False)),
                "gemini_tts_min_interval_seconds": int(settings.get("gemini_tts_min_interval_seconds") or 0),
                "openai_tts_model": settings.get("openai_tts_model") or "gpt-4o-mini-tts",
                "openai_tts_voice": settings.get("openai_tts_voice") or "coral",
                "openai_tts_speed": settings.get("openai_tts_speed") or "fast",
                "openai_tts_instructions": settings.get("openai_tts_instructions") or "Warm, clear, friendly home assistant voice.",
            }
        )

    def speaker_targets(self, category):
        category = str(category or "utilities").strip().lower()
        if self.state.get("global_mute"):
            return {"ha": [], "sonos": [], "alexa": []}
        targets = {"ha": [], "sonos": [], "alexa": []}
        for _name, speaker in (self.state.get("speakers") or {}).items():
            if not speaker.get("enabled", True):
                continue
            if category == "all":
                pass
            elif category == "doorbell" and not speaker.get("doorbell", True):
                continue
            elif category == "fridge" and not speaker.get("fridge", True):
                continue
            elif category not in {"doorbell", "fridge"} and not speaker.get("utilities", True):
                continue
            speaker_id = str(speaker.get("id") or "").strip()
            speaker_type = str(speaker.get("type") or "").strip().lower()
            if speaker_id and speaker_type in targets:
                targets[speaker_type].append(speaker_id)
        return targets

    def set_armed(self, armed):
        self.state["armed"] = bool(armed)
        self._save()
        return self.public_state()

    def set_global_mute(self, muted):
        self.state["global_mute"] = bool(muted)
        self._save()
        return self.public_state()

    def set_speaker_enabled(self, speaker_name, enabled):
        name = unquote(str(speaker_name or "")).strip().lower()
        speakers = self.state.setdefault("speakers", {})
        if name not in speakers:
            return None
        speakers[name]["enabled"] = bool(enabled)
        self._save()
        return self.public_state()

    def set_speaker_route(self, speaker_name, route, enabled):
        route = str(route or "").strip().lower()
        if route not in {"doorbell", "fridge", "utilities"}:
            return None
        name = unquote(str(speaker_name or "")).strip().lower()
        speakers = self.state.setdefault("speakers", {})
        if name not in speakers:
            return None
        speakers[name][route] = bool(enabled)
        self._save()
        return self.public_state()

    def upsert_speaker(self, name, speaker_id, speaker_type, enabled=True, routes=None):
        name = str(name or "").strip().lower()
        speaker_id = str(speaker_id or "").strip()
        speaker_type = str(speaker_type or "").strip().lower()
        if not name or not speaker_id or speaker_type not in {"ha", "sonos", "alexa"}:
            return None
        routes = routes if isinstance(routes, dict) else {}
        self.state.setdefault("speakers", {})[name] = {
            "id": speaker_id,
            "type": speaker_type,
            "enabled": bool(enabled),
            "doorbell": bool(routes.get("doorbell", True)),
            "fridge": bool(routes.get("fridge", True)),
            "utilities": bool(routes.get("utilities", True)),
        }
        self._save()
        return self.public_state()

    def delete_speaker(self, speaker_name):
        name = unquote(str(speaker_name or "")).strip().lower()
        speakers = self.state.setdefault("speakers", {})
        if name not in speakers:
            return None
        speakers.pop(name)
        self._save()
        return self.public_state()

    def set_ice_maker_enabled(self, enabled):
        self.state.setdefault("ice_maker", {})["enabled"] = bool(enabled)
        self._save()
        return self.public_state()

    def set_chime(self, event_name, filename):
        event_name = str(event_name or "").strip().lower()
        if event_name not in {
            "front_doorbell",
            "back_doorbell",
            "fridge_open",
            "fridge_closed",
            "freezer_open",
            "freezer_closed",
        }:
            return None
        filename = _safe_filename(filename)
        if filename and filename not in self.available_chimes():
            return None
        self.state.setdefault("chimes", {}).setdefault("events", {})[event_name] = filename
        self._save()
        return self.public_state()

    def set_settings(self, payload):
        settings = self.state.setdefault("settings", {})
        door_keys = (
            "front_door_video_source", "back_door_video_source",
            "front_door_camera_entity", "back_door_camera_entity",
            "front_door_trigger", "back_door_trigger",
            "back_door_enabled", "doorbell_listener_enabled",
        )
        proposed = {**settings, **{key: payload[key] for key in door_keys if key in payload}}
        for door in ("front", "back"):
            source = str(proposed.get(f"{door}_door_video_source") or "rtsp").strip().lower()
            if source not in {"rtsp", "ring_native"}:
                raise ValueError("Select an available doorbell video source.")
            if source != "ring_native" or (door == "back" and not _payload_bool(proposed.get("back_door_enabled"))):
                continue
            camera = str(proposed.get(f"{door}_door_camera_entity") or "").strip()
            trigger = str(proposed.get(f"{door}_door_trigger") or "").strip()
            if camera and (not camera.startswith("camera.") or not camera.endswith("_live_view")):
                raise ValueError(f"Select a Ring live-view camera for the {door} door.")
            if trigger and (not trigger.startswith("event.") or not trigger.endswith("_ding")):
                raise ValueError(f"Select a Ring ding event for the {door} door.")
            if _payload_bool(proposed.get("doorbell_listener_enabled")) and (not camera or not trigger):
                raise ValueError(f"Select a Ring live-view camera and ding event for the {door} door before enabling automatic alerts.")
        requested_mode = str(payload.get("doorbell_video_mode") or "fast").strip().lower()
        if "doorbell_video_mode" in payload and requested_mode != "fast" and any(
            str(proposed.get(f"{door}_door_video_source") or "rtsp") == "ring_native"
            for door in ("front", "back") if door == "front" or _payload_bool(proposed.get("back_door_enabled"))
        ):
            raise ValueError("Native Ring video currently supports Fast mode. Switch to RTSP before selecting another mode.")
        updated_messages = _cinderella_messages_from_payload(payload, settings.get("cinderella_messages"))
        for key in [
            "external_base_url",
            "speaker_base_url",
            "gemini_vision_model",
            "gemini_live_model",
            "front_door_stream_url",
            "back_door_stream_url",
            "front_door_camera_entity",
            "back_door_camera_entity",
            "front_door_trigger",
            "back_door_trigger",
            "front_door_live_stream_switch",
            "back_door_live_stream_switch",
            "front_door_photo_prompt",
            "back_door_photo_prompt",
            "doorbell_video_prompt",
            "gemini_tts_model",
            "gemini_tts_style",
            "openai_tts_model",
            "openai_tts_instructions",
        ]:
            if key in payload:
                settings[key] = str(payload.get(key) or "").strip().rstrip("/")
        if "openai_vision_model" in payload:
            settings["openai_vision_model"] = normalize_openai_vision_model(payload.get("openai_vision_model"))
        if "ai_provider" in payload:
            provider = str(payload.get("ai_provider") or "gemini").strip().lower()
            settings["ai_provider"] = provider if provider in {"gemini", "openai"} else "gemini"
        if "tts_engine" in payload:
            engine = str(payload.get("tts_engine") or "home_assistant").strip().lower()
            settings["tts_engine"] = engine if engine in {"home_assistant", "gemini", "openai"} else "home_assistant"
        if "gemini_tts_voice" in payload:
            voice = str(payload.get("gemini_tts_voice") or "Sulafat").strip()
            settings["gemini_tts_voice"] = voice if voice in GEMINI_TTS_VOICES else "Sulafat"
        if "gemini_tts_speed" in payload:
            speed = str(payload.get("gemini_tts_speed") or "normal").strip().lower()
            settings["gemini_tts_speed"] = speed if speed in {"slow", "normal", "fast", "very_fast"} else "normal"
        if "openai_tts_voice" in payload:
            voice = str(payload.get("openai_tts_voice") or "coral").strip().lower()
            settings["openai_tts_voice"] = voice if voice in OPENAI_TTS_VOICES else "coral"
        if "openai_tts_speed" in payload:
            speed = str(payload.get("openai_tts_speed") or "normal").strip().lower()
            settings["openai_tts_speed"] = speed if speed in {"slow", "normal", "fast", "very_fast"} else "normal"
        if "gemini_tts_keep_warm" in payload:
            settings["gemini_tts_keep_warm"] = _payload_bool(payload.get("gemini_tts_keep_warm"))
        if "doorbell_video_mode" in payload:
            mode = str(payload.get("doorbell_video_mode") or "fast").strip().lower()
            settings["doorbell_video_mode"] = mode if mode in {"fast", "smart", "live", "detailed", "manual"} else "fast"
        for key in ("back_door_enabled", "doorbell_listener_enabled"):
            if key in payload:
                settings[key] = _payload_bool(payload[key])
        for door in ("front", "back"):
            key = f"{door}_door_video_source"
            if key in payload:
                source = str(payload[key] or "").strip().lower()
                if source not in {"rtsp", "ring_native"}:
                    raise ValueError("Select an available doorbell video source.")
                settings[key] = source
        styles = dict(settings.get("ai_description_styles") or {})
        custom = dict(settings.get("ai_custom_descriptions") or {})
        if "ai_style_default" in payload:
            style = str(payload.get("ai_style_default") or "balanced").strip().lower()
            if style not in {"balanced", "fast_security", "people_movement", "packages_deliveries", "detailed_blind", "custom"}:
                style = "balanced"
            custom_text = str(payload.get("ai_custom_default") or "").strip()
            for job in ("front_photo", "back_photo", "manual_video", "smart_video", "detailed_video"):
                styles[job] = style
                custom[job] = custom_text
        for job in ("front_photo", "back_photo", "manual_video", "smart_video", "detailed_video"):
            style_key = f"ai_style_{job}"
            custom_key = f"ai_custom_{job}"
            if style_key in payload:
                style = str(payload.get(style_key) or "balanced").strip().lower()
                styles[job] = style if style in {"balanced", "fast_security", "people_movement", "packages_deliveries", "detailed_blind", "custom"} else "balanced"
            if custom_key in payload:
                custom[job] = str(payload.get(custom_key) or "").strip()
        if styles:
            settings["ai_description_styles"] = styles
        if custom:
            settings["ai_custom_descriptions"] = custom
        for key, minimum, maximum in [
            ("doorbell_dedupe_seconds", 5, 180),
            ("doorbell_live_video_seconds", 25, 120),
            ("live_speaker_chunk_seconds", 1, 5),
            ("doorbell_live_video_frames", 2, 6),
            ("fridge_stale_minutes", 5, 240),
            ("vacuum_repeat_quiet_minutes", 1, 240),
            ("gemini_tts_min_interval_seconds", 0, 600),
        ]:
            if key in payload:
                settings[key] = _clamped_int(payload.get(key), settings.get(key), minimum, maximum)
        if "vacuum_cleaning_mode" in payload:
            settings["vacuum_cleaning_mode"] = _normalize_vacuum_cleaning_mode(payload.get("vacuum_cleaning_mode"))
        if "vacuum_announce_events" in payload:
            settings["vacuum_announce_events"] = _csv_list(payload.get("vacuum_announce_events"))
        if updated_messages is not None:
            settings["cinderella_messages"] = updated_messages
        if str(payload.get("gemini_api_key") or "").strip():
            settings["gemini_api_key"] = str(payload.get("gemini_api_key") or "").strip()
        if _payload_bool(payload.get("clear_gemini_api_key", False)):
            settings["gemini_api_key"] = ""
        if str(payload.get("openai_api_key") or "").strip():
            settings["openai_api_key"] = str(payload.get("openai_api_key") or "").strip()
        if _payload_bool(payload.get("clear_openai_api_key", False)):
            settings["openai_api_key"] = ""
        if str(payload.get("pushover_user_key") or "").strip():
            settings["pushover_user_key"] = str(payload.get("pushover_user_key") or "").strip()
        if str(payload.get("pushover_api_token") or "").strip():
            settings["pushover_api_token"] = str(payload.get("pushover_api_token") or "").strip()
        if _payload_bool(payload.get("clear_pushover", False)):
            settings["pushover_user_key"] = ""
            settings["pushover_api_token"] = ""
        self._save()
        return self.public_state()

    def chime_for_event(self, event_type, payload):
        payload = payload if isinstance(payload, dict) else {}
        event_name = ""
        if event_type == "doorbell":
            door = str(payload.get("door") or "front").strip().lower()
            event_name = "back_doorbell" if door.startswith("back") else "front_doorbell"
        elif event_type == "fridge":
            appliance = str(payload.get("appliance") or "fridge").strip().lower()
            state = str(payload.get("state") or "").strip().lower()
            if state in {"open", "opened", "on"}:
                event_name = "freezer_open" if "freezer" in appliance else "fridge_open"
            elif state in {"closed", "close", "off"}:
                event_name = "freezer_closed" if "freezer" in appliance else "fridge_closed"
        if not event_name:
            return ""
        filename = self.state.get("chimes", {}).get("events", {}).get(event_name, "")
        filename = _safe_filename(filename)
        return filename if filename in self.available_chimes() else ""

    def available_chimes(self):
        try:
            CHIMES_DIR.mkdir(parents=True, exist_ok=True)
            _sync_media_chimes()
            return _available_chime_names()
        except OSError as exc:
            LOGGER.warning("Could not list chimes: %s", exc)
            return []

    def save_chime_file(self, filename, content):
        filename = _safe_filename(filename)
        if not filename:
            return {"ok": False, "message": "Choose an MP3, WAV, OGG, or M4A chime file."}
        try:
            CHIMES_DIR.mkdir(parents=True, exist_ok=True)
            path = CHIMES_DIR / filename
            path.write_bytes(content or b"")
            _sync_media_chimes()
        except OSError as exc:
            return {"ok": False, "message": f"Could not save chime: {exc}"}
        return {"ok": True, "filename": filename, "state": self.public_state()}

    def delete_chime_file(self, filename):
        filename = _safe_filename(filename)
        if not filename:
            return {"ok": False, "message": "Missing chime filename."}
        try:
            path = CHIMES_DIR / filename
            if path.exists():
                path.unlink()
            media_path = MEDIA_CHIMES_DIR / filename
            if media_path.exists():
                media_path.unlink()
            for event, selected in list(self.state.setdefault("chimes", {}).setdefault("events", {}).items()):
                if selected == filename:
                    self.state["chimes"]["events"][event] = ""
            self._save()
        except OSError as exc:
            return {"ok": False, "message": f"Could not delete chime: {exc}"}
        return {"ok": True, "state": self.public_state()}

    def _load(self):
        state = {
            "armed": True,
            "global_mute": False,
            "ice_maker": {"enabled": False},
            "settings": {
                "external_base_url": "",
                "speaker_base_url": "",
                "gemini_api_key": "",
                "openai_api_key": "",
                "pushover_user_key": "",
                "pushover_api_token": "",
                "ai_provider": "openai",
                "gemini_vision_model": "gemini-3.5-flash",
                "openai_vision_model": DEFAULT_OPENAI_VISION_MODEL,
                "front_door_stream_url": "",
                "back_door_stream_url": "",
                "front_door_video_source": "rtsp",
                "back_door_video_source": "rtsp",
                "front_door_camera_entity": "",
                "back_door_camera_entity": "",
                "front_door_live_stream_switch": "switch.front_door_live_stream",
                "back_door_live_stream_switch": "switch.back_door_live_stream",
                "front_door_photo_prompt": "",
                "back_door_photo_prompt": "",
                "doorbell_video_prompt": "",
                "ai_description_styles": {
                    "front_photo": "balanced",
                    "back_photo": "balanced",
                    "manual_video": "detailed_blind",
                    "smart_video": "fast_security",
                    "detailed_video": "detailed_blind",
                },
                "ai_custom_descriptions": {
                    "front_photo": "",
                    "back_photo": "",
                    "manual_video": "",
                    "smart_video": "",
                    "detailed_video": "",
                },
                "doorbell_video_mode": "fast",
                "doorbell_live_video_seconds": 30,
                "live_speaker_chunk_seconds": 2,
                "doorbell_live_video_frames": 4,
                "doorbell_dedupe_seconds": 30,
                "fridge_stale_minutes": 45,
                "vacuum_cleaning_mode": "vacuum_mop",
                "vacuum_repeat_quiet_minutes": 20,
                "vacuum_announce_events": [
                    "departure",
                    "washing",
                    "emptying",
                    "returning",
                    "victory",
                    "paused",
                    "drying",
                    "error",
                ],
                "cinderella_messages": deepcopy(DEFAULT_CINDERELLA_MESSAGES),
                "tts_engine": "home_assistant",
                "gemini_tts_model": "gemini-3.1-flash-tts-preview",
                "gemini_tts_voice": "Sulafat",
                "gemini_tts_speed": "fast",
                "gemini_tts_style": "warm, clear, friendly",
                "gemini_tts_keep_warm": False,
                "gemini_tts_min_interval_seconds": 0,
                "openai_tts_model": "gpt-4o-mini-tts",
                "openai_tts_voice": "coral",
                "openai_tts_speed": "fast",
                "openai_tts_instructions": "Warm, clear, friendly home assistant voice.",
            },
            "chimes": {
                "events": {
                    "front_doorbell": "",
                    "back_doorbell": "",
                    "fridge_open": "",
                    "fridge_closed": "",
                    "freezer_open": "",
                    "freezer_closed": "",
                },
                "available": [],
            },
            "speakers": deepcopy(DEFAULT_SPEAKERS),
            "vacuum_rooms": {},
        }
        loaded_successfully = False
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded:
                    for key in ("settings", "speakers", "chimes", "features"):
                        if loaded.get(key) is not None and not isinstance(loaded[key], dict):
                            raise ValueError(f"Invalid {key} in saved state")
                    if not isinstance((loaded.get("chimes") or {}).get("events", {}), dict):
                        raise ValueError("Invalid chime events in saved state")
                    state["features"] = loaded.get("features") or {name: True for name in FEATURES}
                    state["setup_complete"] = loaded.get("setup_complete", True)
                    state["installation_profile"] = loaded.get("installation_profile", "legacy")
                    state.update({k: v for k, v in loaded.items() if k not in {"speakers", "settings", "chimes"}})
                    speakers = {}
                    for name, speaker in (loaded.get("speakers") or {}).items():
                        normalized = str(name or "").strip().lower()
                        if normalized:
                            speakers.setdefault(normalized, {}).update(speaker if isinstance(speaker, dict) else {})
                    state["speakers"] = speakers
                    chimes = deepcopy(state["chimes"])
                    if isinstance(loaded.get("chimes"), dict):
                        chimes["events"].update(loaded["chimes"].get("events") or {})
                    state["chimes"] = chimes
                    settings = deepcopy(state["settings"])
                    if isinstance(loaded.get("settings"), dict):
                        settings.update(loaded["settings"])
                    state["settings"] = settings
                    loaded_successfully = True
            except (OSError, ValueError) as exc:
                LOGGER.warning("Could not read Viper Core control state: %s", exc)
        if not loaded_successfully and self.profile != "legacy":
            state["features"] = dict(DOORBELL_FEATURES)
            state["installation_profile"] = "doorbells"
            state["setup_complete"] = False
            state["speakers"] = {}
            state["settings"].update({
                "front_door_video_source": "ring_native", "back_door_video_source": "ring_native",
                "front_door_live_stream_switch": "", "back_door_live_stream_switch": "",
                "doorbell_listener_enabled": False, "back_door_enabled": False,
                "front_door_trigger": "", "back_door_trigger": "", "tts_entity": "",
                "cinderella_messages": {key: ["Vacuum status changed."] for key in CINDERELLA_MESSAGE_BUCKETS},
            })
            state["settings"]["cinderella_messages"]["specific_errors"] = {}
        elif not loaded_successfully:
            state["features"] = {name: True for name in FEATURES}
            state["installation_profile"] = "legacy"
            state["setup_complete"] = True
        return state

    def vacuum_rooms(self, entity_id):
        rooms = (self.state.get("vacuum_rooms") or {}).get(str(entity_id or "").strip(), [])
        return _sanitize_vacuum_rooms(rooms)

    def set_vacuum_rooms(self, entity_id, rooms):
        entity_id = str(entity_id or "").strip()
        if not entity_id:
            return []
        sanitized = _sanitize_vacuum_rooms(rooms)
        self.state.setdefault("vacuum_rooms", {})[entity_id] = sanitized
        self._save()
        return sanitized

    def _save(self):
        temporary_path = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(self.state, indent=2, sort_keys=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
                temporary_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            return True
        except OSError as exc:
            LOGGER.warning("Could not save Viper Core control state: %s", exc)
            return False
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


class ControlApi:
    def __init__(self, control_state, ha_client):
        self.control_state = control_state
        self.ha = ha_client
        self._hvac_commands = {}
        self._hvac_history = []
        self._hvac_lock = threading.Lock()
        self._hvac_status_lock = threading.Lock()

    def hvac_commands(self):
        with self._hvac_status_lock:
            return deepcopy(self._hvac_commands)

    def hvac_history(self):
        with self._hvac_status_lock:
            return deepcopy(self._hvac_history)

    def _record_hvac_command(self, entity_id, result):
        with self._hvac_status_lock:
            self._hvac_commands[entity_id] = deepcopy(result)

    def handle_get(self, path):
        if path == "/api/control/hvac/status":
            return {"ok": True, "commands": self.hvac_commands()}
        if path == "/api/control/state":
            return self.control_state.public_state()
        if path == "/api/chimes":
            return {"ok": True, "chimes": self.control_state.available_chimes()}
        if path.startswith("/api/doorbell/debug/"):
            door = path.rsplit("/", 1)[-1]
            config = self.control_state.effective_config(SimpleNamespace())
            return live.capture_diagnostic_frames(config, self.ha, door)
        if path.startswith("/api/live/session/"):
            session_id = unquote(path.rsplit("/", 1)[-1])
            session = live.get_live_session(session_id)
            if not session:
                return {"ok": False, "message": "Live session is not available."}
            return {"ok": True, "session": session}
        return None

    def chime_path(self, filename):
        filename = _safe_filename(filename)
        if not filename:
            return None
        for path in (CHIMES_DIR / filename, MEDIA_CHIMES_DIR / filename):
            try:
                if path.exists() and path.is_file():
                    return path
            except OSError:
                continue
        return None

    def diagnostic_file_path(self, filename):
        return live.diagnostic_file_path(filename)

    def upload_chime(self, filename, content):
        return self.control_state.save_chime_file(filename, content)

    def handle_post(self, path, payload):
        if path.startswith("/api/setup/") and getattr(self, "setup_service", None):
            return self.setup_service.handle(path.rsplit("/", 1)[-1], payload)
        for prefix, feature in (("/api/control/hvac", "hvac"), ("/api/control/vacuum", "vacuum"), ("/api/control/ice_maker", "ice_maker")):
            if path.startswith(prefix) and not self.control_state.feature_enabled(feature):
                return {"ok": False, "message": "This feature is disabled for this installation."}
        state = _payload_bool(payload)
        if path == "/api/control/armed":
            return {"ok": True, "state": self.control_state.set_armed(state)}
        if path == "/api/control/global_mute":
            return {"ok": True, "state": self.control_state.set_global_mute(state)}
        if path == "/api/control/ice_maker/enabled":
            result = self._set_ice_maker(state)
            actual_state = result.get("state_matches", state)
            return {"ok": bool(result.get("ok")), "message": result.get("message", ""), "state": self.control_state.set_ice_maker_enabled(actual_state)}
        if path == "/api/control/speakers":
            updated = self.control_state.upsert_speaker(
                payload.get("name"),
                payload.get("id"),
                payload.get("type"),
                _payload_bool(payload.get("enabled", True)),
                payload.get("routes") or payload,
            )
            if updated is None:
                return {"ok": False, "message": "Speaker needs a name, id, and type of ha, sonos, or alexa."}
            return {"ok": True, "state": updated}
        if path == "/api/control/chimes":
            updated = self.control_state.set_chime(payload.get("event"), payload.get("filename", ""))
            if updated is None:
                return {"ok": False, "message": "Unknown chime event or chime file."}
            return {"ok": True, "state": updated}
        if path == "/api/control/settings":
            try:
                return {"ok": True, "state": self.control_state.set_settings(payload)}
            except ValueError as exc:
                field = "doorbell_video_mode" if str(exc).startswith("Native Ring video") else "cinderella_specific_errors_json"
                return {"ok": False, "message": str(exc), "field": field}
        if path == "/api/control/hvac":
            return self._set_hvac(payload)
        if path == "/api/control/vacuum":
            return self._vacuum_action(payload)
        if path == "/api/control/vacuum/entity":
            return self._vacuum_entity_action(payload)
        if path == "/api/control/vacuum/rooms":
            return self._vacuum_refresh_rooms(payload)
        if path == "/api/control/vacuum/room_clean":
            return self._vacuum_room_clean(payload)
        if path.startswith("/api/live/session/") and path.endswith("/describe"):
            session_id = unquote(path[len("/api/live/session/") : -len("/describe")])
            return live.describe_live_session_now(session_id)
        if path.startswith("/api/live/session/") and path.endswith("/stop"):
            session_id = unquote(path[len("/api/live/session/") : -len("/stop")])
            return live.stop_live_session(session_id)
        if path == "/api/chimes/delete":
            return self.control_state.delete_chime_file(payload.get("filename"))
        prefix = "/api/control/speakers/"
        suffix = "/enabled"
        if path.startswith(prefix) and path.endswith(suffix):
            speaker_name = path[len(prefix) : -len(suffix)]
            updated = self.control_state.set_speaker_enabled(speaker_name, state)
            if updated is None:
                return {"ok": False, "message": f"Unknown speaker: {unquote(speaker_name)}"}
            return {"ok": True, "state": updated}
        route_suffix = "/route"
        if path.startswith(prefix) and path.endswith(route_suffix):
            speaker_name = path[len(prefix) : -len(route_suffix)]
            updated = self.control_state.set_speaker_route(speaker_name, payload.get("route"), state)
            if updated is None:
                return {"ok": False, "message": f"Unknown speaker or route: {unquote(speaker_name)}"}
            return {"ok": True, "state": updated}
        delete_suffix = "/delete"
        if path.startswith(prefix) and path.endswith(delete_suffix):
            speaker_name = path[len(prefix) : -len(delete_suffix)]
            updated = self.control_state.delete_speaker(speaker_name)
            if updated is None:
                return {"ok": False, "message": f"Unknown speaker: {unquote(speaker_name)}"}
            return {"ok": True, "state": updated}
        return None

    def device_status(self):
        if not self.ha.available():
            return {
                "ok": False,
                "message": "Home Assistant API is not configured.",
                "heat_pumps": [],
                "vacuum": {},
                "refrigerator": {},
            }
        heat_pumps = []
        ok = True
        for entity_id, name in HEAT_PUMP_UNITS if self.control_state.feature_enabled("hvac") else []:
            item = _read_entity(self.ha, entity_id)
            if not item.get("ok"):
                ok = False
            heat_pumps.append({
                "name": name,
                "entity_id": entity_id,
                **item,
            })
        vacuum = _read_entity(self.ha, VACUUM_ENTITY) if self.control_state.feature_enabled("vacuum") else {}
        vacuum_controls = self._vacuum_controls(VACUUM_ENTITY) if self.control_state.feature_enabled("vacuum") else []
        refrigerator = {
            "ice_maker": _read_entity(self.ha, "switch.refrigerator_cubed_ice"),
            "fridge_door": _read_entity(self.ha, "binary_sensor.refrigerator_fridge_door"),
            "freezer_door": _read_entity(self.ha, "binary_sensor.refrigerator_freezer_door"),
            "filter_usage": _read_entity(self.ha, "sensor.refrigerator_water_filter_usage"),
            "filter_status": _read_entity(self.ha, "sensor.refrigerator_filter_status"),
        } if self.control_state.feature_enabled("fridge") or self.control_state.feature_enabled("ice_maker") else {}
        airflow = []
        for entity_id, name in HEAT_PUMP_AIRFLOW if self.control_state.feature_enabled("hvac") else []:
            airflow.append({"name": name, "entity_id": entity_id, **_read_entity(self.ha, entity_id)})
        ok = ok and (not self.control_state.feature_enabled("vacuum") or vacuum.get("ok", False))
        return {
            "ok": ok,
            "message": "Device status refreshed." if ok else "One or more devices need attention.",
            "heat_pumps": heat_pumps,
            "airflow": airflow,
            "vacuum": vacuum,
            "vacuum_status": _read_entity(self.ha, VACUUM_STATUS_ENTITY) if self.control_state.feature_enabled("vacuum") else {},
            "vacuum_controls": vacuum_controls,
            "refrigerator": refrigerator,
            "timestamp": int(time.time()),
        }

    def _set_ice_maker(self, enabled):
        if not self.ha.available():
            return {"ok": False, "message": "Home Assistant is not available.", "state_matches": False}
        switch_entity = "switch.refrigerator_cubed_ice"
        keep_on_entity = "input_boolean.keep_ice_maker_on"
        refill_entity = "input_boolean.ice_maker_auto_refill_running"
        counter_entity = "counter.ice_usage_counter"
        try:
            if enabled:
                self.ha.call_service("input_boolean/turn_on", {"entity_id": keep_on_entity})
                self.ha.call_service("input_boolean/turn_off", {"entity_id": refill_entity})
                self.ha.call_service("counter/reset", {"entity_id": counter_entity})
                service = "switch/turn_on"
            else:
                self.ha.call_service("input_boolean/turn_off", {"entity_id": keep_on_entity})
                self.ha.call_service("input_boolean/turn_off", {"entity_id": refill_entity})
                service = "switch/turn_off"
            desired = "on" if enabled else "off"
            for attempt in range(1, 4):
                self.ha.call_service(service, {"entity_id": switch_entity})
                time.sleep(2)
                self.ha.call_service("homeassistant/update_entity", {"entity_id": switch_entity})
                time.sleep(1)
                current = str((self.ha.get_state(switch_entity) or {}).get("state") or "unknown").lower()
                if current == desired:
                    return {"ok": True, "message": f"Ice maker is {desired}.", "state_matches": bool(enabled)}
                LOGGER.info("Ice maker command attempt %s left %s at %s, wanted %s.", attempt, switch_entity, current, desired)
            if enabled:
                self.ha.call_service("input_boolean/turn_off", {"entity_id": keep_on_entity})
            return {"ok": False, "message": f"Ice maker command did not stick; {switch_entity} is not {desired}.", "state_matches": not bool(enabled)}
        except Exception as exc:
            LOGGER.warning("Ice maker control failed: %s", exc)
            return {"ok": False, "message": f"Ice maker control failed: {exc}", "state_matches": False}

    def _set_hvac(self, payload):
        mode = str(payload.get("mode") or "").strip().lower()
        temp = payload.get("temperature")
        target = str(payload.get("entity_id") or "all").strip()
        entities = HEAT_PUMP_CLIMATES if target in {"", "all"} else [target]
        calls = []
        if mode not in {"", "off", "cool", "heat"}:
            return {"ok": False, "message": "Unsupported heat pump mode.", "entities": []}
        if not mode and (temp is None or temp == ""):
            return {"ok": False, "message": "Choose a heat pump mode or temperature.", "entities": []}
        if mode != "off" and temp is not None and temp != "":
            try:
                temp = float(temp)
                if not math.isfinite(temp):
                    raise ValueError("Temperature must be finite.")
            except (TypeError, ValueError):
                return {"ok": False, "message": "Enter a valid heat pump temperature.", "entities": []}
        if any(entity not in HEAT_PUMP_CLIMATES for entity in entities):
            return {"ok": False, "message": "Choose a configured heat pump.", "entities": []}
        if not self._hvac_lock.acquire(blocking=False):
            return {"ok": False, "message": "A heat pump command is still running. Try again when it finishes."}
        results = {}
        try:
            for entity_id in entities:
                item = {"status": "sending", "mode": mode, "temperature": temp if mode != "off" else None,
                        "timestamp": int(time.time()), "message": "Sending", "confirmed": False}
                results[entity_id] = item
                self._record_hvac_command(entity_id, item)
            for entity_id, item in results.items():
                try:
                    if mode == "off":
                        self.ha.call_service("climate/turn_off", {"entity_id": entity_id})
                    else:
                        if mode:
                            self.ha.call_service("climate/set_hvac_mode", {"entity_id": entity_id, "hvac_mode": mode})
                        if temp not in {None, ""}:
                            self.ha.call_service("climate/set_temperature", {"entity_id": entity_id, "temperature": temp})
                    calls.append(entity_id)
                    item.update(status="unconfirmed", message="Sent; could not confirm in Home Assistant")
                except Exception as exc:
                    item.update(status="failed", message=f"Command failed: {exc}")
                self._record_hvac_command(entity_id, item)
            # Read back HA state; infrared devices cannot confirm physical receipt.
            for attempt in range(3):
                pending = [entity for entity, item in results.items() if item["status"] == "unconfirmed"]
                if not pending:
                    break
                if attempt:
                    time.sleep(1)
                for entity_id in pending:
                    item = results[entity_id]
                    try:
                        observed = self.ha.get_state(entity_id) or {}
                        observed_mode = observed.get("state")
                        observed_temp = (observed.get("attributes") or {}).get("temperature")
                        item.update(observed_mode=observed_mode, observed_temperature=observed_temp)
                        matches = observed_mode not in {None, "unknown", "unavailable"}
                        matches = matches and (not mode or observed_mode == mode)
                        if mode != "off" and temp not in {None, ""}:
                            matches = matches and _values_match(observed_temp, temp, numeric=True)
                        if matches:
                            item.update(status="confirmed", confirmed=True, message="Confirmed by Home Assistant")
                    except Exception:
                        item["message"] = "Sent; Home Assistant state could not be read"
                    self._record_hvac_command(entity_id, item)
            confirmed = sum(item["confirmed"] for item in results.values())
            failed = sum(item["status"] == "failed" for item in results.values())
            with self._hvac_status_lock:
                self._hvac_history.extend({**deepcopy(item), "entity_id": entity} for entity, item in results.items())
                self._hvac_history = self._hvac_history[-30:]
            message = f"{confirmed} confirmed by Home Assistant; {len(results) - confirmed - failed} unconfirmed; {failed} failed."
            return {"ok": not failed, "confirmed": confirmed == len(results), "message": message,
                    "entities": calls, "commands": results}
        finally:
            self._hvac_lock.release()

    def _vacuum_action(self, payload):
        action = str(payload.get("action") or "").strip().lower()
        service = {
            "start": "vacuum/start",
            "pause": "vacuum/pause",
            "stop": "vacuum/stop",
            "dock": "vacuum/return_to_base",
        }.get(action)
        if not service:
            return {"ok": False, "message": "Unknown vacuum action."}
        entity_id = str(payload.get("entity_id") or VACUUM_ENTITY).strip()
        try:
            if action == "start":
                mode = self._set_saved_vacuum_mode(payload)
                self._apply_vacuum_cleaning_mode(entity_id, mode)
            self.ha.call_service(service, {"entity_id": entity_id})
        except Exception as exc:
            return {"ok": False, "message": f"Vacuum command failed: {exc}"}
        return {"ok": True, "message": f"Vacuum {action} command sent.", "entity_id": entity_id}

    def _vacuum_entity_action(self, payload):
        entity_id = str(payload.get("entity_id") or "").strip()
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if domain not in {"select", "number", "switch", "button", "fan", "vacuum"}:
            return {"ok": False, "message": "Unsupported vacuum control entity."}
        try:
            if domain == "select":
                option = str(payload.get("option") or "").strip()
                if not option:
                    return {"ok": False, "message": "Choose an option first."}
                if entity_id == "select.cinderella_dock_empty_mode":
                    return self._set_dock_empty_mode(option)
                LOGGER.info("Vacuum control: setting %s to %s", entity_id, option)
                self.ha.call_service("select/select_option", {"entity_id": entity_id, "option": option})
                return self._vacuum_confirmed_result(entity_id, option, label="state")
            elif domain == "number":
                value = float(payload.get("value"))
                LOGGER.info("Vacuum control: setting %s to %s", entity_id, value)
                self.ha.call_service("number/set_value", {"entity_id": entity_id, "value": value})
                return self._vacuum_confirmed_result(entity_id, value, label="state", numeric=True)
            elif domain == "switch":
                wants_on = _payload_bool(payload.get("state", True))
                service = "switch/turn_on" if wants_on else "switch/turn_off"
                LOGGER.info("Vacuum control: calling %s for %s", service, entity_id)
                self.ha.call_service(service, {"entity_id": entity_id})
                return self._vacuum_confirmed_result(entity_id, "on" if wants_on else "off", label="state")
            elif domain == "button":
                LOGGER.info("Vacuum control: pressing %s", entity_id)
                self.ha.call_service("button/press", {"entity_id": entity_id})
                return {"ok": True, "message": f"Pressed {entity_id}.", "entity_id": entity_id}
            elif domain == "fan":
                percentage = payload.get("percentage")
                preset = str(payload.get("preset_mode") or "").strip()
                if preset:
                    LOGGER.info("Vacuum control: setting %s preset to %s", entity_id, preset)
                    self.ha.call_service("fan/set_preset_mode", {"entity_id": entity_id, "preset_mode": preset})
                    return self._vacuum_confirmed_result(entity_id, preset, attribute="preset_mode", label="preset")
                elif percentage not in {None, ""}:
                    percentage_value = int(float(percentage))
                    LOGGER.info("Vacuum control: setting %s percentage to %s", entity_id, percentage_value)
                    self.ha.call_service("fan/set_percentage", {"entity_id": entity_id, "percentage": percentage_value})
                    return self._vacuum_confirmed_result(entity_id, percentage_value, attribute="percentage", label="percentage", numeric=True)
                else:
                    return {"ok": False, "message": "Choose a fan preset or percentage first."}
            elif domain == "vacuum":
                command = str(payload.get("command") or "").strip()
                if command:
                    if command not in {"app_start_collect_dust"}:
                        return {"ok": False, "message": "Unsupported vacuum command."}
                    LOGGER.info("Vacuum control: sending %s to %s", command, entity_id)
                    self.ha.call_service("vacuum/send_command", {"entity_id": entity_id, "command": command})
                    return {"ok": True, "message": f"Sent {command} to {entity_id}.", "entity_id": entity_id}
                speed = str(payload.get("fan_speed") or "").strip()
                if not speed:
                    return {"ok": False, "message": "Choose a suction speed first."}
                LOGGER.info("Vacuum control: setting %s suction speed to %s", entity_id, speed)
                self.ha.call_service("vacuum/set_fan_speed", {"entity_id": entity_id, "fan_speed": speed})
                return self._vacuum_confirmed_result(entity_id, speed, attribute="fan_speed", label="suction speed")
        except Exception as exc:
            return {"ok": False, "message": f"Vacuum control failed: {exc}"}
        return {"ok": True, "message": f"Sent command to {entity_id}.", "entity_id": entity_id}

    def _set_dock_empty_mode(self, option):
        option = str(option or "").strip().lower()
        if option == "unknown":
            return {"ok": False, "message": "Choose smart, light, balanced, or max for dock empty mode."}
        if option not in DOCK_EMPTY_MODE_VALUES:
            return {"ok": False, "message": f"Unsupported dock empty mode: {option}"}
        LOGGER.info("Vacuum control: setting dock empty mode to %s", option)
        self.ha.call_service(
            "vacuum/send_command",
            {
                "entity_id": VACUUM_ENTITY,
                "command": "set_dust_collection_mode",
                "params": {"mode": DOCK_EMPTY_MODE_VALUES[option]},
            },
        )
        return self._vacuum_confirmed_result("select.cinderella_dock_empty_mode", option, label="state")

    def _vacuum_confirmed_result(self, entity_id, expected, *, attribute=None, label="state", numeric=False):
        confirmed, actual = self._wait_for_entity_value(entity_id, expected, attribute=attribute, numeric=numeric)
        if confirmed:
            return {"ok": True, "message": f"Confirmed {entity_id} {label} is {expected}.", "entity_id": entity_id, "confirmed": True}
        actual_text = "unknown" if actual in {None, ""} else actual
        LOGGER.warning("Vacuum control did not confirm: %s expected %s=%s but HA reports %s", entity_id, label, expected, actual_text)
        return {
            "ok": False,
            "message": f"Command sent, but Home Assistant still reports {entity_id} {label} as {actual_text}.",
            "entity_id": entity_id,
            "confirmed": False,
            "reported": actual_text,
        }

    def _wait_for_entity_value(self, entity_id, expected, *, attribute=None, numeric=False):
        deadline = time.monotonic() + VACUUM_CONFIRM_TIMEOUT_SECONDS
        actual = None
        while True:
            state = self.ha.get_state(entity_id) or {}
            attrs = state.get("attributes") or {}
            actual = attrs.get(attribute) if attribute else state.get("state")
            if _values_match(actual, expected, numeric=numeric):
                return True, actual
            if time.monotonic() >= deadline:
                return False, actual
            time.sleep(VACUUM_CONFIRM_POLL_SECONDS)

    def _vacuum_room_clean(self, payload):
        entity_id = str(payload.get("entity_id") or VACUUM_ENTITY).strip()
        raw_segments = payload.get("segments") or ""
        segments = []
        items = raw_segments if isinstance(raw_segments, list) else re.split(r"[\s,;]+", str(raw_segments).strip())
        for item in items:
            if not item:
                continue
            try:
                segments.append(int(item))
            except ValueError:
                return {"ok": False, "message": f"Room ID must be a number: {item}"}
        if not segments:
            return {"ok": False, "message": "Enter one or more room IDs first."}
        repeat = _clamped_int(payload.get("repeat"), 1, 1, 3)
        mode = self._set_saved_vacuum_mode(payload)
        try:
            self._apply_vacuum_cleaning_mode(entity_id, mode)
            self.ha.call_service(
                "vacuum/send_command",
                {
                    "entity_id": entity_id,
                    "command": "app_segment_clean",
                    "params": [{"segments": segments, "repeat": repeat}],
                },
            )
        except Exception as exc:
            return {"ok": False, "message": f"Room clean failed: {exc}"}
        return {"ok": True, "message": f"Sent {_vacuum_mode_label(mode).lower()} room clean for {len(segments)} room(s), repeat {repeat}.", "segments": segments, "repeat": repeat, "mode": mode}

    def _set_saved_vacuum_mode(self, payload):
        mode = _normalize_vacuum_cleaning_mode(payload.get("vacuum_cleaning_mode") or payload.get("cleaning_mode") or self.control_state.state.get("settings", {}).get("vacuum_cleaning_mode"))
        self.control_state.state.setdefault("settings", {})["vacuum_cleaning_mode"] = mode
        self.control_state._save()
        return mode

    def _apply_vacuum_cleaning_mode(self, entity_id, mode):
        controls = self._vacuum_controls(entity_id)
        current_fan = ""
        selected = next((control for control in controls if control.get("entity_id") == entity_id), None)
        attrs = selected.get("attributes") if selected and isinstance(selected.get("attributes"), dict) else {}
        current_fan = str(attrs.get("fan_speed") or "")
        for service, payload in _vacuum_cleaning_mode_service_calls(entity_id, controls, mode, current_fan):
            LOGGER.info("Vacuum control: applying %s with %s before cleaning.", service, payload)
            self.ha.call_service(service, payload)

    def _vacuum_refresh_rooms(self, payload):
        entity_id = str(payload.get("entity_id") or VACUUM_ENTITY).strip()
        try:
            result = self.ha.call_service("roborock/get_maps", {"entity_id": entity_id}, return_response=True)
        except Exception as exc:
            return {"ok": False, "message": f"Room discovery failed: {exc}", "rooms": self.control_state.vacuum_rooms(entity_id)}
        rooms = _parse_roborock_rooms(result, entity_id)
        if not rooms:
            return {
                "ok": False,
                "message": "No rooms came back from Roborock maps.",
                "rooms": self.control_state.vacuum_rooms(entity_id),
            }
        saved = self.control_state.set_vacuum_rooms(entity_id, rooms)
        return {"ok": True, "message": f"Loaded {len(saved)} room choices.", "rooms": saved}

    def _vacuum_controls(self, selected_entity_id):
        try:
            states = self.ha.get_states()
        except Exception as exc:
            LOGGER.debug("Could not read vacuum controls: %s", exc)
            return []
        tokens = _vacuum_tokens(selected_entity_id)
        controls = []
        for entity in states if isinstance(states, list) else []:
            entity_id = str(entity.get("entity_id") or "")
            domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
            attrs = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
            text = " ".join(str(part).lower() for part in [entity_id, attrs.get("friendly_name"), attrs.get("manufacturer"), attrs.get("model")])
            if domain not in VACUUM_CONTROL_DOMAINS and entity_id != selected_entity_id:
                continue
            if entity_id != selected_entity_id and not any(token and token in text for token in tokens):
                continue
            controls.append({
                "entity_id": entity_id,
                "domain": domain,
                "state": str(entity.get("state") or "unknown"),
                "friendly_name": attrs.get("friendly_name", ""),
                "attributes": _vacuum_public_attributes(attrs),
            })
        return sorted(controls, key=lambda item: (item["domain"], item.get("friendly_name") or item["entity_id"]))


def _payload_bool(payload):
    if isinstance(payload, dict) and "state" in payload:
        value = payload.get("state")
    else:
        value = payload
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "enabled"}
    return bool(value)


def _clamped_int(value, fallback, minimum, maximum):
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        try:
            parsed = int(float(fallback))
        except (TypeError, ValueError):
            parsed = minimum
    return max(minimum, min(maximum, parsed))


def _csv_list(value):
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[\s,]+", str(value or ""))
    return [str(item).strip().lower() for item in items if str(item).strip()]


def _line_list(value):
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").splitlines()
    return [str(item).strip() for item in items if str(item).strip()]


def _normalize_cinderella_messages(value):
    source = value if isinstance(value, dict) else {}
    normalized = deepcopy(DEFAULT_CINDERELLA_MESSAGES)
    for bucket in CINDERELLA_MESSAGE_BUCKETS:
        lines = _line_list(source.get(bucket))
        if lines:
            normalized[bucket] = lines
    specific = source.get("specific_errors") if isinstance(source.get("specific_errors"), dict) else {}
    normalized["specific_errors"] = {
        str(name or "").strip().lower().replace(" ", "_"): _line_list(lines)
        for name, lines in specific.items()
        if str(name or "").strip() and _line_list(lines)
    }
    return normalized


def _cinderella_messages_from_payload(payload, current):
    if not any(key.startswith("cinderella_") for key in payload):
        return None
    messages = _normalize_cinderella_messages(current)
    for bucket in CINDERELLA_MESSAGE_BUCKETS:
        key = f"cinderella_{bucket}"
        if key in payload:
            lines = _line_list(payload.get(key))
            messages[bucket] = lines or list(DEFAULT_CINDERELLA_MESSAGES[bucket])
    if "cinderella_specific_errors_json" in payload:
        raw = str(payload.get("cinderella_specific_errors_json") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("Specific Error Messages JSON is invalid. Use an object mapping error names to lists of phrases.") from exc
            if not isinstance(parsed, dict) or any(not isinstance(lines, list) or any(not isinstance(line, str) for line in lines) for lines in parsed.values()):
                raise ValueError("Specific Error Messages JSON must map error names to lists of text phrases.")
            messages["specific_errors"] = _normalize_cinderella_messages({"specific_errors": parsed})["specific_errors"]
        else:
            messages["specific_errors"] = {}
    return messages


def _speaker_base_url(external_base_url):
    text = str(external_base_url or "").strip().rstrip("/")
    if text.startswith("http://100.") or text.startswith("https://100."):
        return "http://homeassistant.local:8099"
    return text


def _safe_filename(filename):
    filename = Path(str(filename or "").replace("\\", "/")).name.strip()
    if not filename:
        return ""
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_CHIME_SUFFIXES:
        return ""
    stem = Path(filename).stem.strip(" .") or "chime"
    stem = re.sub(r"[^A-Za-z0-9_ -]+", "_", stem)
    stem = re.sub(r"[. ]+", " ", stem).strip() or "chime"
    return f"{stem}{suffix}"


def _sync_media_chimes():
    if not _media_chimes_available():
        return
    MEDIA_CHIMES_DIR.mkdir(parents=True, exist_ok=True)
    CHIMES_DIR.mkdir(parents=True, exist_ok=True)
    _sync_chime_directory(MEDIA_CHIMES_DIR, CHIMES_DIR)
    _sync_chime_directory(CHIMES_DIR, MEDIA_CHIMES_DIR)


def _sync_chime_directory(source_dir, target_dir):
    for item in source_dir.iterdir():
        if not item.is_file() or item.suffix.lower() not in ALLOWED_CHIME_SUFFIXES:
            continue
        safe_name = _safe_filename(item.name)
        if not safe_name:
            continue
        target = target_dir / safe_name
        if item.resolve() == target.resolve():
            continue
        if not target.exists() or item.stat().st_mtime > target.stat().st_mtime or item.stat().st_size != target.stat().st_size:
            shutil.copy2(item, target)


def _available_chime_names():
    names = set()
    for directory in (CHIMES_DIR, MEDIA_CHIMES_DIR):
        try:
            for item in directory.iterdir():
                if item.is_file() and item.suffix.lower() in ALLOWED_CHIME_SUFFIXES:
                    safe_name = _safe_filename(item.name)
                    if safe_name:
                        names.add(safe_name)
        except OSError:
            continue
    return sorted(names)


def _media_chimes_available():
    try:
        return MEDIA_CHIMES_DIR.exists() or MEDIA_CHIMES_DIR.parent.exists()
    except OSError:
        return False


def _read_entity(ha, entity_id):
    try:
        payload = ha.get_state(entity_id) or {}
    except Exception as exc:
        return {"ok": False, "state": "missing", "message": str(exc), "attributes": {}}
    state = str(payload.get("state") or "unknown")
    attributes = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else {}
    return {
        "ok": state.lower() not in {"unknown", "unavailable", "missing"},
        "state": state,
        "friendly_name": attributes.get("friendly_name", ""),
        "attributes": _public_attributes(attributes),
        "last_changed": payload.get("last_changed", ""),
        "last_updated": payload.get("last_updated", ""),
    }


def _public_attributes(attributes):
    keys = [
        "current_temperature",
        "temperature",
        "target_temp_high",
        "target_temp_low",
        "hvac_modes",
        "fan_mode",
        "fan_modes",
        "swing_mode",
        "swing_modes",
        "battery_level",
        "status",
        "percentage",
        "preset_mode",
        "preset_modes",
    ]
    return {key: attributes.get(key) for key in keys if key in attributes}


def _vacuum_public_attributes(attributes):
    keys = [
        "friendly_name",
        "options",
        "min",
        "max",
        "step",
        "unit_of_measurement",
        "fan_speed",
        "fan_speed_list",
        "percentage",
        "preset_mode",
        "preset_modes",
        "battery_level",
        "status",
    ]
    return {key: attributes.get(key) for key in keys if key in attributes}


def _values_match(actual, expected, *, numeric=False):
    if numeric:
        try:
            return float(actual) == float(expected)
        except (TypeError, ValueError):
            return False
    return str(actual).strip().lower() == str(expected).strip().lower()


def _parse_roborock_rooms(data, entity_id):
    service_response = data.get("service_response") if isinstance(data, dict) else None
    if not isinstance(service_response, dict):
        return []
    vacuum_payload = service_response.get(entity_id) or next(iter(service_response.values()), {})
    maps = vacuum_payload.get("maps") if isinstance(vacuum_payload, dict) else []
    rooms = []
    for map_info in maps if isinstance(maps, list) else []:
        map_name = str(map_info.get("name") or "Current map")
        room_map = map_info.get("rooms") if isinstance(map_info.get("rooms"), dict) else {}
        for room_id, room_name in room_map.items():
            try:
                segment_id = int(room_id)
            except (TypeError, ValueError):
                continue
            name = str(room_name or f"Room {segment_id}")
            label = f"{name} ({segment_id})" if map_name == "Current map" else f"{name} on {map_name} ({segment_id})"
            rooms.append({"label": label, "name": name, "map": map_name, "segment": segment_id})
    return _sanitize_vacuum_rooms(rooms)


def _sanitize_vacuum_rooms(rooms):
    cleaned = []
    for room in rooms if isinstance(rooms, list) else []:
        if not isinstance(room, dict):
            continue
        try:
            segment = int(room.get("segment"))
        except (TypeError, ValueError):
            continue
        name = str(room.get("name") or f"Room {segment}")
        map_name = str(room.get("map") or "Current map")
        label = str(room.get("label") or (f"{name} ({segment})" if map_name == "Current map" else f"{name} on {map_name} ({segment})"))
        cleaned.append({"label": label, "name": name, "map": map_name, "segment": segment})
    return sorted(cleaned, key=lambda room: room["label"].lower())


def _normalize_vacuum_cleaning_mode(value):
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "vacuum_and_mop": "vacuum_mop",
        "mop_and_vacuum": "vacuum_mop",
        "vacuum_mop": "vacuum_mop",
        "vacuum": "vacuum_only",
        "vacuum_only": "vacuum_only",
        "mop": "mop_only",
        "mop_only": "mop_only",
    }
    return aliases.get(text, "vacuum_mop")


def _vacuum_mode_label(mode):
    return {
        "vacuum_mop": "Vacuum and mop",
        "vacuum_only": "Vacuum only",
        "mop_only": "Mop only",
    }.get(_normalize_vacuum_cleaning_mode(mode), "Vacuum and mop")


def _normalized_option(value):
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _select_first_option(options, preferred):
    normalized = {_normalized_option(option): str(option) for option in options}
    for item in preferred:
        if item in normalized:
            return normalized[item]
    return ""


def _select_mop_intensity_option(options, mode):
    options = [str(option) for option in options]
    if mode == "vacuum_only":
        return _select_first_option(options, ("off", "none", "close", "closed"))
    return _select_first_option(options, ("moderate", "medium", "standard", "normal", "low", "slight", "high", "extreme"))


def _select_mop_mode_option(options, mode):
    options = [str(option) for option in options]
    if mode == "vacuum_only":
        return ""
    if mode == "mop_only":
        return _select_first_option(options, ("standard", "smart_mode", "deep", "deep_plus", "fast", "custom"))
    return _select_first_option(options, ("standard", "smart_mode", "fast", "custom"))


def _select_fan_speed_for_mode(options, mode, current=""):
    options = [str(option) for option in options]
    if mode == "mop_only":
        return _select_first_option(options, ("off_raise_main_brush", "off", "quiet", "balanced"))
    current_text = _normalized_option(current)
    if current_text in {"off_raise_main_brush", "off"}:
        return _select_first_option(options, ("balanced", "standard", "turbo", "max", "quiet"))
    return str(current or "") if str(current or "") in options else ""


def _vacuum_cleaning_mode_service_calls(entity_id, controls, mode, fan_speed=""):
    mode = _normalize_vacuum_cleaning_mode(mode)
    calls = []
    for control in controls or []:
        control_id = control.get("entity_id", "")
        domain = control_id.split(".", 1)[0] if "." in control_id else ""
        attrs = control.get("attributes") if isinstance(control.get("attributes"), dict) else {}
        name_text = " ".join(str(part).lower() for part in [control_id, attrs.get("friendly_name"), control.get("friendly_name")])
        options = [str(option) for option in attrs.get("options", [])] if isinstance(attrs.get("options"), list) else []
        if domain == "select" and any(token in name_text for token in ("cleaning_mode", "clean mode", "water_box_mode", "water box")):
            option = _select_first_option(options, ("vacuum_mop", "vacuum_and_mop", "standard", "smart_mode") if mode == "vacuum_mop" else (mode,))
            if option:
                calls.append(("select/select_option", {"entity_id": control_id, "option": option}))
        elif domain == "select" and any(token in name_text for token in ("mop_intensity", "mop intensity", "water", "flow")):
            option = _select_mop_intensity_option(options, mode)
            if option and option != str(control.get("state", "")):
                calls.append(("select/select_option", {"entity_id": control_id, "option": option}))
        elif domain == "select" and any(token in name_text for token in ("mop_mode", "mop mode")):
            option = _select_mop_mode_option(options, mode)
            if option and option != str(control.get("state", "")):
                calls.append(("select/select_option", {"entity_id": control_id, "option": option}))
        elif domain == "number" and mode == "vacuum_only" and any(token in name_text for token in ("mop_intensity", "water", "flow")):
            try:
                value = float(attrs.get("min", 0))
            except (TypeError, ValueError):
                value = 0
            calls.append(("number/set_value", {"entity_id": control_id, "value": value}))
    selected_vacuum = next((control for control in controls or [] if control.get("entity_id") == entity_id), None)
    attrs = selected_vacuum.get("attributes") if selected_vacuum and isinstance(selected_vacuum.get("attributes"), dict) else {}
    fan_options = [str(item) for item in attrs.get("fan_speed_list", [])] if isinstance(attrs.get("fan_speed_list"), list) else []
    fan = _select_fan_speed_for_mode(fan_options, mode, fan_speed)
    if fan and fan != str(fan_speed):
        calls.append(("vacuum/set_fan_speed", {"entity_id": entity_id, "fan_speed": fan}))
    return calls


def _vacuum_tokens(entity_id):
    base = str(entity_id or "").split(".", 1)[-1].lower()
    parts = [item for item in re.split(r"[_\W]+", base) if item]
    tokens = {base, "roborock", "cinderella"}
    tokens.update(parts)
    tokens.discard("vacuum")
    return tokens
