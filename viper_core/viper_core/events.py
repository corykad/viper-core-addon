import logging
import random
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import escape

from . import live
from . import tts, vision
from .features import feature_for_event


LOGGER = logging.getLogger(__name__)


class EventProcessor:
    def __init__(self, config, ha_client, control_state=None):
        self.config = config
        self.ha = ha_client
        self.control_state = control_state
        self.recent_events = []
        self.last_event_by_key = {}
        self._dedupe_lock = threading.Lock()
        self._fridge_repair_suppress_until = 0
        self._fridge_repair_times = []
        self._fridge_repair_lock = threading.Lock()
        self.fridge_health = {"stale": False, "message": "Not checked yet."}

    def handle(self, event_type, payload):
        event_type = _clean(event_type)
        payload = payload if isinstance(payload, dict) else {}
        feature = feature_for_event(event_type)
        if feature and self.control_state and not self.control_state.feature_enabled(feature):
            return self._record(event_type, {}, True, "Event ignored because this feature is disabled.")
        if event_type in {"doorbell", "doorbell_video", "doorbell_live_update", "fridge", "vacuum", "ice_maker", "hvac", "broadcast", "chime", "pushover_test", "voice_test"}:
            return getattr(self, f"_handle_{event_type}")(payload)
        return self._record(event_type, payload, False, f"Unknown Viper Core event type: {event_type}")

    def _handle_doorbell(self, payload):
        door = _door_key(payload.get("door") or payload.get("source") or "front")
        action = _clean(payload.get("action") or payload.get("event") or "pressed")
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        key = f"doorbell:{door}"
        is_test = _payload_bool(payload.get("test", False))
        settings = self.control_state.state.get("settings", {}) if self.control_state else {}
        if (not is_test and settings.get("doorbell_listener_enabled")
                and settings.get(f"{door}_door_video_source") == "ring_native"
                and payload.get("source") != "ha_listener"
                and payload.get("entity_id") != settings.get(f"{door}_door_trigger")):
            return self._record("doorbell", payload, True, "Ignored legacy Ring router event; native press listener is selected.", duplicate=True)
        if not is_test and self.control_state and not self.control_state.public_state().get("armed", True):
            return self._record("doorbell", payload, True, f"Ignored {_door_label(door).lower()} {action}; Viper is disarmed.")
        if not is_test and self._is_duplicate(key, seconds=getattr(effective_config, "doorbell_dedupe_seconds", 30)):
            return self._record("doorbell", payload, True, f"Ignored duplicate {_door_label(door)} event from {action}.", duplicate=True)
        mode = str(getattr(effective_config, "doorbell_video_mode", "fast") or "fast").lower()
        if mode == "live":
            seconds = getattr(effective_config, "doorbell_live_video_seconds", 30)
            session_id = live.create_live_session(door)
            thread = threading.Thread(
                target=self._background_true_live_doorbell,
                args=(door, seconds, False, session_id),
                daemon=True,
            )
            thread.start()
            message = f"{_door_label(door)} live doorbell stream is starting."
            live_url = _doorbell_live_url(effective_config, door, session_id)
        else:
            message = vision.describe_doorbell(effective_config, self.ha, door)
            live_url = _doorbell_live_url(effective_config, door)
        if not message:
            message = f"{_door_label(door)}bell {action.replace('_', ' ')}."
        else:
            message = _door_alert_message(door, message)
        self._notify(
            _doorbell_title(door),
            message,
            self.config.doorbell_speaker_service,
            "doorbell",
            "doorbell",
            {**payload, "door": door, "live_url": live_url},
            speak=mode != "live",
        )
        if mode != "live":
            self._maybe_start_video_followup(door, message, effective_config, payload)
        return self._record("doorbell", {**payload, "door": door}, True, message)

    def _background_true_live_doorbell(self, door, seconds, all_speakers=False, session_id=""):
        try:
            for _item in live.gemini_true_live_doorbell_events(self.config, self.ha, self.control_state, self.handle, door, seconds, True, all_speakers, session_id=session_id):
                pass
        except Exception:
            LOGGER.exception("Automatic true live doorbell narration failed.")

    def _handle_doorbell_video(self, payload):
        door = _door_key(payload.get("door") or payload.get("source") or "front")
        seconds = payload.get("seconds")
        mode = _clean(payload.get("mode") or "manual")
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        message = vision.describe_live_doorbell(effective_config, self.ha, door, seconds=seconds, mode=mode)
        if not message:
            message = f"{_door_label(door)} live video did not return a description."
            spoken = message
        else:
            spoken = f"{_door_label(door)} live video: {_door_specific_text(door, message)}"
        self._notify(f"{_doorbell_title(door)} Video", spoken, self.config.doorbell_speaker_service, "doorbell", "doorbell_video", {**payload, "door": door})
        return self._record("doorbell_video", {**payload, "door": door}, True, spoken)

    def _handle_doorbell_live_update(self, payload):
        door = _door_key(payload.get("door") or "front")
        message = _door_alert_message(door, payload.get("message") or "")
        if not message:
            return self._record("doorbell_live_update", {**payload, "door": door}, False, "Live doorbell update ignored: no message provided.")
        if not self.ha.available():
            return self._record("doorbell_live_update", {**payload, "door": door}, False, "Live doorbell update skipped: Home Assistant is not available.")
        if self.control_state and self.control_state.public_state().get("global_mute"):
            return self._record("doorbell_live_update", {**payload, "door": door}, True, "Live doorbell update skipped because global mute is on.")
        category = "all" if _payload_bool(payload.get("all_speakers", False)) else "doorbell"
        result = self._speak(_doorbell_title(door), message, category)
        if result and result.get("error"):
            return self._record("doorbell_live_update", {**payload, "door": door}, False, f"Live doorbell update speech failed: {result['error']}")
        return self._record("doorbell_live_update", {**payload, "door": door}, True, message)

    def _handle_fridge(self, payload):
        appliance = _clean(payload.get("appliance") or payload.get("source") or "fridge")
        state = _clean(payload.get("state") or payload.get("event") or "changed")
        stale = self._fridge_stale_status()
        if state == "stale_check":
            if stale.get("stale"):
                repair = self._maybe_repair_refrigerator_connection(stale)
                if repair.get("started"):
                    message = "Refrigerator connection check started SmartThings repair."
                else:
                    message = f"{stale.get('message')} Repair not started: {repair.get('reason')}."
            else:
                message = stale.get("message", "Refrigerator connection check passed.")
            return self._record("fridge", {**payload, "stale_status": stale}, True, message)
        if self._fridge_reload_suppression_active(payload):
            message = f"Ignored refrigerator {appliance.replace('_', ' ')} {state.replace('_', ' ')} during SmartThings repair."
            LOGGER.info(message)
            return self._record("fridge", {**payload, "stale_status": stale}, True, message, duplicate=True)
        message = f"The {appliance.replace('_', ' ')} is {state.replace('_', ' ')}."
        self._notify("Viper Refrigerator", message, self.config.fridge_speaker_service, "fridge", "fridge", payload)
        return self._record("fridge", {**payload, "stale_status": stale}, True, message)

    def _fridge_reload_suppression_active(self, payload):
        if time.time() >= self._fridge_repair_suppress_until:
            return False
        entity_id = str(payload.get("entity_id") or "")
        return "refrigerator_fridge_door" in entity_id or "refrigerator_freezer_door" in entity_id

    def _maybe_repair_refrigerator_connection(self, stale):
        if not stale.get("repair_eligible"):
            return {"started": False, "reason": "insufficient evidence of a connection failure"}
        if not getattr(self.config, "fridge_stale_auto_repair", False):
            return {"started": False, "reason": "disabled"}
        if not self.ha.available():
            return {"started": False, "reason": "ha_unavailable"}
        entry_id = str(getattr(self.config, "smartthings_config_entry_id", "") or "").strip()
        if not entry_id:
            return {"started": False, "reason": "missing_smartthings_entry"}
        with self._fridge_repair_lock:
            now = time.time()
            self._fridge_repair_times = [stamp for stamp in self._fridge_repair_times if now - stamp < 86400]
            if self._fridge_repair_times and now - self._fridge_repair_times[-1] < 900:
                return {"started": False, "reason": "15-minute repair cooldown"}
            if len(self._fridge_repair_times) >= 3:
                return {"started": False, "reason": "daily repair limit reached"}
            self._fridge_repair_times.append(now)
            self._fridge_repair_suppress_until = now + 90
        thread = threading.Thread(
            target=self._repair_refrigerator_connection,
            args=(entry_id,),
            daemon=True,
            name="viper-fridge-smartthings-repair",
        )
        thread.start()
        return {"started": True, "entry_id": entry_id, "stale": stale}

    def _repair_refrigerator_connection(self, entry_id):
        entities = [
            "binary_sensor.refrigerator_fridge_door",
            "binary_sensor.refrigerator_freezer_door",
            "switch.refrigerator_cubed_ice",
            "sensor.refrigerator_water_filter_usage",
            "sensor.refrigerator_fridge_temperature",
            "sensor.refrigerator_freezer_temperature",
        ]
        try:
            for entity_id in entities:
                self.ha.call_service("homeassistant/update_entity", {"entity_id": entity_id})
            self.ha.call_service("homeassistant/reload_config_entry", {"entry_id": entry_id})
            time.sleep(8)
            for entity_id in entities:
                self.ha.call_service("homeassistant/update_entity", {"entity_id": entity_id})
            LOGGER.info("Requested SmartThings refrigerator repair for config entry %s.", entry_id)
        except Exception as exc:
            LOGGER.warning("SmartThings refrigerator repair failed: %s", exc)

    def _maybe_start_video_followup(self, door, first_message, effective_config, payload):
        mode = str(getattr(effective_config, "doorbell_video_mode", "fast") or "fast").lower()
        if mode in {"fast", "manual", "live"}:
            return
        if mode == "smart" and not vision.description_needs_live_followup(first_message):
            self._record("doorbell_video", {**payload, "door": door, "mode": mode}, True, "Smart live video follow-up skipped; first RTSP pass was clear.")
            return
        thread = threading.Thread(
            target=self._background_doorbell_video,
            args=(door, mode, getattr(effective_config, "doorbell_live_video_seconds", 4)),
            daemon=True,
        )
        thread.start()

    def _background_doorbell_video(self, door, mode, seconds):
        try:
            self._handle_doorbell_video({"door": door, "seconds": seconds, "mode": mode, "source": f"automatic_{mode}"})
        except Exception:
            LOGGER.exception("Doorbell live video follow-up failed.")

    def _handle_vacuum(self, payload):
        raw_event = payload.get("event") or payload.get("state") or "status"
        event = _vacuum_event_key(raw_event)
        error = str(payload.get("error") or "").strip()
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        announce_events = {
            _vacuum_event_key(item)
            for item in (getattr(effective_config, "vacuum_announce_events", []) or [])
        }
        if announce_events and event not in announce_events and event != "error":
            return self._record("vacuum", payload, True, f"Logged Cinderella {event.replace('_', ' ')} without announcement.")
        dedupe_key = f"vacuum:{event}:{error.lower()}"
        quiet_seconds = int(getattr(effective_config, "vacuum_repeat_quiet_minutes", 20) or 20) * 60
        if self._is_duplicate(dedupe_key, seconds=quiet_seconds):
            return self._record("vacuum", payload, True, f"Ignored repeated Cinderella {event.replace('_', ' ')} update.", duplicate=True)
        source = _clean(payload.get("source") or "vacuum")
        message = _vacuum_message(event, error, source, getattr(effective_config, "cinderella_messages", None))
        if not isinstance(getattr(effective_config, "cinderella_messages", None), dict) and error and "{error}" not in message and error.lower() not in message.lower():
            message = f"{message} {error}"
        self._notify("Viper Vacuum", message, self.config.vacuum_speaker_service, "utilities", "vacuum", payload)
        return self._record("vacuum", payload, True, message)

    def _handle_ice_maker(self, payload):
        action = _clean(payload.get("action") or payload.get("event") or "status")
        message = f"Ice maker {action.replace('_', ' ')}."
        self._notify("Viper Ice Maker", message, "", "utilities", "ice_maker", payload)
        return self._record("ice_maker", payload, True, message)

    def _handle_hvac(self, payload):
        unit = str(payload.get("unit") or payload.get("entity_id") or "Heat pump").strip()
        state = str(payload.get("state") or payload.get("action") or "status changed").strip()
        message = f"{unit}: {state}."
        self._notify("Viper Heat Pump", message, "", "utilities", "hvac", payload)
        return self._record("hvac", payload, True, message)

    def _handle_broadcast(self, payload):
        message = str(payload.get("message") or payload.get("broadcast_text") or "").strip()
        if not message:
            return self._record("broadcast", payload, False, "Broadcast ignored: no message provided.")
        channel = _clean(payload.get("channel") or "utilities")
        category = "fridge" if channel.startswith(("fridge", "freezer")) else "utilities"
        title = "Viper Broadcast"
        if payload.get("push"):
            title = "Viper Broadcast Push"
        self._notify(title, message, "", category, "broadcast", payload)
        return self._record("broadcast", payload, True, message)

    def _handle_chime(self, payload):
        filename = str(payload.get("filename") or "").strip()
        category = _clean(payload.get("category") or "utilities")
        if not filename:
            return self._record("chime", payload, False, "Chime test ignored: no file selected.")
        if self.control_state and filename not in self.control_state.available_chimes():
            return self._record("chime", payload, False, f"Chime {filename} is not uploaded.")
        if not self._play_chime(filename, category):
            return self._record("chime", payload, False, f"Chime {filename} could not be played by any enabled speaker.")
        return self._record("chime", payload, True, f"Tested chime {filename}.")

    def _handle_pushover_test(self, payload):
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        title = str(payload.get("title") or "Viper Core Test").strip()
        message = str(payload.get("message") or "Viper Core Pushover is working.").strip()
        if not getattr(effective_config, "pushover_user_key", "") or not getattr(effective_config, "pushover_api_token", ""):
            return self._record("pushover_test", payload, False, "Pushover test failed: Pushover keys are not configured.")
        try:
            _send_pushover(effective_config.pushover_api_token, effective_config.pushover_user_key, title, message)
        except Exception as exc:
            return self._record("pushover_test", payload, False, f"Pushover test failed: {exc}")
        return self._record("pushover_test", payload, True, "Pushover test sent.")

    def _handle_voice_test(self, payload):
        message = str(payload.get("message") or "Viper Core voice test is working.").strip()
        if not message:
            return self._record("voice_test", payload, False, "Voice test ignored: no message provided.")
        if not self.ha.available():
            return self._record("voice_test", payload, False, "Voice test failed: Home Assistant is not available.")
        if self.control_state and self.control_state.public_state().get("global_mute"):
            return self._record("voice_test", payload, True, "Voice test skipped because global mute is on.")
        result = self._speak("Viper Voice Test", message, "utilities", include_alexa=False)
        if result.get("error"):
            return self._record("voice_test", payload, False, f"Voice test failed: {result['error']}")
        return self._record("voice_test", payload, True, message)

    def _notify(self, title, message, speaker_service, category, event_type="", payload=None, speak=True):
        event_payload = payload if isinstance(payload, dict) else {}
        if not self.ha.available():
            LOGGER.warning("Skipping HA notification because HA is not configured: %s", message)
            return
        if self.control_state and self.control_state.public_state().get("global_mute"):
            LOGGER.info("Global mute is on. Logged without audio: %s", message)
            return
        service = speaker_service or self.config.notification_service
        service_payload = _service_payload(service, title, message, self.config.speaker_targets)
        try:
            self.ha.call_service(service, service_payload)
        except Exception as exc:
            LOGGER.warning("Primary notification service failed: %s", exc)
            try:
                self.ha.create_notification(title, message)
            except Exception as fallback_exc:
                LOGGER.warning("Fallback persistent notification failed: %s", fallback_exc)
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        push_url = str(event_payload.get("live_url") or "").strip()
        push_url_title = "Open Live Doorbell" if push_url else ""
        if self.config.pushover_service and _push_allowed(event_type):
            try:
                payload = {"title": title, "message": message}
                if push_url:
                    payload.update({"url": push_url, "url_title": push_url_title})
                self.ha.call_service(self.config.pushover_service, payload)
                LOGGER.info("Pushover service sent for %s.", event_type or category)
            except Exception as exc:
                LOGGER.warning("Pushover service failed: %s", exc)
        if _push_allowed(event_type) and getattr(effective_config, "pushover_user_key", "") and getattr(effective_config, "pushover_api_token", ""):
            try:
                _send_pushover(effective_config.pushover_api_token, effective_config.pushover_user_key, title, message, url=push_url, url_title=push_url_title)
                LOGGER.info("Direct Pushover sent for %s.", event_type or category)
            except Exception as exc:
                LOGGER.warning("Direct Pushover failed: %s", exc)
        chime_played = self._play_event_chime(event_type, event_payload, category)
        if event_type == "fridge" and chime_played:
            LOGGER.info("Skipping refrigerator speech because chime handled %s.", message)
            return
        if speak:
            self._speak(title, message, category)

    def _play_event_chime(self, event_type, payload, category):
        if not self.control_state:
            return False
        chime = self.control_state.chime_for_event(event_type, payload)
        if not chime:
            LOGGER.info("No chime assigned for %s event payload %s.", event_type, payload)
            return False
        return self._play_chime(chime, category)

    def _play_chime(self, chime, category):
        route_targets = self._route_targets(category)
        url = self._chime_url(chime)
        media_source = _chime_media_source(chime)
        if route_targets["sonos"] and not url:
            LOGGER.warning("Cannot play chime %s on direct Sonos because speaker_base_url or external_base_url is not configured.", chime)
        sent = False
        for target in route_targets["sonos"] if url else []:
            try:
                _play_sonos_url(target, url)
                sent = True
            except Exception as exc:
                LOGGER.warning("Direct Sonos chime failed: %s", exc)
        for target in route_targets["ha"]:
            ha_media = url or media_source
            try:
                self.ha.call_service(
                    "media_player/play_media",
                    {
                        "entity_id": target,
                        "media_content_id": ha_media,
                        "media_content_type": _chime_media_type(chime),
                    },
                )
                sent = True
            except Exception as exc:
                LOGGER.warning("HA chime playback failed: %s", exc)
                if url and ha_media != media_source:
                    try:
                        self.ha.call_service(
                            "media_player/play_media",
                            {
                                "entity_id": target,
                                "media_content_id": media_source,
                                "media_content_type": _chime_media_type(chime),
                            },
                        )
                        LOGGER.info("Played chime %s on %s using media source fallback.", chime, target)
                        sent = True
                    except Exception as fallback_exc:
                        LOGGER.warning("HA chime media source fallback failed: %s", fallback_exc)
        if sent:
            LOGGER.info(
                "Sent chime %s to %s Home Assistant speaker(s) and %s Sonos speaker(s).",
                chime,
                len(route_targets["ha"]),
                len(route_targets["sonos"]),
            )
        else:
            LOGGER.warning("Chime %s was assigned but no compatible speakers were enabled for %s.", chime, category)
        return sent

    def _chime_url(self, chime):
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        base = _speaker_media_base_url(effective_config)
        if not base:
            return ""
        return f"{base.rstrip('/')}/chimes/{urllib.parse.quote(str(chime), safe='')}"

    def _speak(self, title, message, category, include_alexa=True):
        if not self.ha.available():
            return
        route_targets = self._route_targets(category)
        tts_targets = route_targets["ha"]
        sonos_targets = route_targets["sonos"]
        alexa_targets = route_targets["alexa"]
        LOGGER.info(
            "Speaking %s through %s Home Assistant, %s Sonos, and %s Alexa target(s).",
            category,
            len(tts_targets),
            len(sonos_targets),
            len(alexa_targets),
        )
        ai_tts = self._speak_with_ai_tts(message, route_targets)
        ai_tts_handled = ai_tts.get("handled", False)
        ai_tts_sent = ai_tts.get("sent", False)
        sent = ai_tts_sent
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        if (self.config.tts_service or getattr(effective_config, "tts_entity", "")) and tts_targets and not ai_tts_handled:
            try:
                tts_entity = getattr(effective_config, "tts_entity", "")
                self.ha.call_service(
                    "tts/speak" if tts_entity else self.config.tts_service,
                    {"entity_id": tts_entity, "media_player_entity_id": tts_targets, "message": message} if tts_entity else {"entity_id": tts_targets, "message": message},
                )
                sent = True
            except Exception as exc:
                LOGGER.warning("TTS service failed: %s", exc)
        if sonos_targets and not ai_tts_handled:
            try:
                payload = self.ha.tts_get_url(message)
                media_url = payload.get("url") if isinstance(payload, dict) else ""
                if not media_url:
                    raise RuntimeError("Home Assistant did not return a TTS media URL.")
                for target in sonos_targets:
                    _play_sonos_url(target, media_url)
                    sent = True
            except Exception as exc:
                LOGGER.warning("Direct Sonos playback failed: %s", exc)
        if include_alexa and self.config.alexa_notify_service and alexa_targets:
            try:
                self.ha.call_service(
                    self.config.alexa_notify_service,
                    {
                        "message": message,
                        "title": title,
                        "target": alexa_targets,
                        "data": {"type": "announce"},
                    },
                )
                sent = True
            except Exception as exc:
                LOGGER.warning("Alexa announce failed: %s", exc)
        return {**ai_tts, "sent": sent, "error": "" if sent else ai_tts.get("error") or "No speaker accepted the announcement. Check speaker selection and speech configuration."}

    def _speak_with_ai_tts(self, message, route_targets):
        effective_config = self.control_state.effective_config(self.config) if self.control_state else self.config
        engine = str(getattr(effective_config, "tts_engine", "") or "").lower()
        if engine not in {"gemini", "openai"}:
            return {"handled": False, "sent": False, "error": ""}
        settings = getattr(effective_config, "__dict__", {})
        try:
            tts.cleanup_old_tts()
            if engine == "openai":
                audio = tts.generate_openai_tts(message, settings)
            else:
                audio = tts.generate_gemini_tts(message, settings)
        except Exception as exc:
            LOGGER.warning("%s TTS failed; not falling back to Home Assistant TTS because AI TTS is selected: %s", engine.title(), exc)
            return {"handled": True, "sent": False, "error": f"{engine.title()} TTS failed: {exc}"}
        filename = audio.get("filename", "")
        LOGGER.info("Generated %s TTS file %s.", engine.title(), filename or "(unknown)")
        sent = False
        url = tts.media_url(filename, _speaker_media_base_url(effective_config))
        for target in route_targets["ha"]:
            try:
                self.ha.call_service(
                    "media_player/play_media",
                    {
                        "entity_id": target,
                        "media_content_id": url or audio["media_source"],
                        "media_content_type": audio["mime_type"],
                    },
                )
                sent = True
                LOGGER.info("Sent %s TTS file %s to Home Assistant speaker %s.", engine.title(), filename or "(unknown)", target)
            except Exception as exc:
                LOGGER.warning("%s TTS HA playback failed for %s: %s", engine.title(), target, exc)
        for target in route_targets["sonos"] if url else []:
            try:
                _play_sonos_url(target, url)
                sent = True
                LOGGER.info("Sent %s TTS file %s to direct Sonos %s.", engine.title(), filename or "(unknown)", target)
            except Exception as exc:
                LOGGER.warning("%s TTS direct Sonos playback failed for %s: %s", engine.title(), target, exc)
        if sent:
            tts.delete_tts_later(filename)
        return {"handled": True, "sent": sent, "error": "" if sent else f"{engine.title()} TTS generated audio, but no enabled compatible speakers could play it."}

    def _route_targets(self, category):
        if self.control_state:
            return self.control_state.speaker_targets(category)
        return {
            "ha": list(self.config.tts_targets or []),
            "sonos": list(self.config.direct_sonos_targets or []),
            "alexa": list(self.config.alexa_targets or []),
        }

    def _record(self, event_type, payload, ok, message, duplicate=False):
        item = {
            "ok": bool(ok),
            "event_type": event_type,
            "message": message,
            "payload": payload,
            "duplicate": bool(duplicate),
            "timestamp": int(time.time()),
        }
        self.recent_events.append(item)
        self.recent_events = self.recent_events[-50:]
        LOGGER.info("%s", message)
        return item

    def _is_duplicate(self, key, seconds):
        now = time.monotonic()
        with self._dedupe_lock:
            previous = self.last_event_by_key.get(key)
            if previous is not None and now - previous < seconds:
                return True
            self.last_event_by_key[key] = now
            return False

    def _fridge_stale_status(self):
        self.fridge_health = self._read_fridge_health()
        return self.fridge_health

    def _read_fridge_health(self):
        if not self.ha.available():
            return {"stale": True, "repair_eligible": False, "message": "Home Assistant is not available."}
        threshold = 45
        if self.control_state:
            threshold = int(getattr(self.control_state.effective_config(self.config), "fridge_stale_minutes", 45) or 45)
        doors = ("binary_sensor.refrigerator_fridge_door", "binary_sensor.refrigerator_freezer_door")
        support = ("sensor.refrigerator_fridge_temperature", "sensor.refrigerator_freezer_temperature", "switch.refrigerator_cubed_ice")
        unavailable = []
        updates = []
        errors = []
        for entity_id in doors + support:
            try:
                state = self.ha.get_state(entity_id) or {}
            except Exception:
                errors.append(entity_id)
                continue
            if str(state.get("state") or "unknown").lower() in {"unknown", "unavailable"}:
                unavailable.append(entity_id)
            updated = _parse_ha_time(state.get("last_reported") or state.get("last_updated"))
            if updated:
                updates.append(updated)
        age = max(0, int((datetime.now(timezone.utc) - max(updates)).total_seconds() / 60)) if updates else None
        repair_eligible = any(entity in unavailable for entity in doors) and any(entity in unavailable for entity in support)
        if unavailable:
            message = "Refrigerator entities unavailable: " + ", ".join(unavailable) + "."
        elif errors:
            message = "Some refrigerator entities could not be checked; no automatic repair."
        elif age is not None and age <= threshold:
            message = "Refrigerator entities are available and reporting updates."
        else:
            message = "Refrigerator entities are available. No recent updates; an unchanged door alone does not indicate a connection failure."
        return {"stale": bool(unavailable or errors), "repair_eligible": repair_eligible and not errors,
                "age_minutes": age, "threshold_minutes": threshold, "message": message,
                "unavailable": unavailable, "checked_at": int(time.time())}


def _service_payload(service, title, message, speaker_targets):
    normalized = str(service or "").replace("/", ".").lower()
    if normalized == "persistent_notification.create":
        return {"title": title, "message": message}
    if normalized.startswith("notify."):
        return {"title": title, "message": message}
    if normalized in {"tts.speak", "tts.cloud_say", "tts.google_translate_say"}:
        payload = {"message": message}
        if speaker_targets:
            payload["entity_id"] = speaker_targets
        return payload
    if normalized.startswith("media_player."):
        return {"entity_id": speaker_targets, "media_content_id": message, "media_content_type": "music"}
    return {"title": title, "message": message}


def _chime_media_type(filename):
    suffix = str(filename or "").rsplit(".", 1)[-1].lower()
    return {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "m4a": "audio/mp4",
    }.get(suffix, "audio/mpeg")


def _chime_media_source(filename):
    return f"media-source://media_source/local/viper_core_chimes/{urllib.parse.quote(str(filename or ''), safe='')}"


def _play_sonos_url(
    host,
    media_url,
    tolerate_set_timeout=False,
    set_timeout=8,
    radio_title="",
    tolerate_play_timeout=False,
    play_timeout=8,
):
    host = str(host or "").strip()
    media_url = str(media_url or "").strip()
    if not host or not media_url:
        return
    endpoint = f"http://{host}:1400/MediaRenderer/AVTransport/Control"
    current_uri = _sonos_radio_uri(media_url) if radio_title else media_url
    escaped_url = escape(current_uri, quote=True)
    metadata = _sonos_radio_metadata(media_url, radio_title) if radio_title else ""
    try:
        _sonos_soap(
            endpoint,
            "SetAVTransportURI",
            (
                '<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                "<InstanceID>0</InstanceID>"
                f"<CurrentURI>{escaped_url}</CurrentURI>"
                f"<CurrentURIMetaData>{metadata}</CurrentURIMetaData>"
                "</u:SetAVTransportURI>"
            ),
            timeout=set_timeout,
        )
    except Exception:
        if not tolerate_set_timeout:
            raise
        LOGGER.warning("Sonos %s live relay SetURI timed out after %ss for %s; sending Play anyway.", host, set_timeout, media_url)
    try:
        _sonos_soap(
            endpoint,
            "Play",
            (
                '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                "<InstanceID>0</InstanceID><Speed>1</Speed>"
                "</u:Play>"
            ),
            timeout=play_timeout,
        )
    except Exception:
        if not tolerate_play_timeout:
            raise
        LOGGER.warning("Sonos %s live relay Play timed out after %ss for %s; checking transport state separately.", host, play_timeout, media_url)


def _sonos_radio_metadata(media_url, title):
    escaped_url = escape(str(media_url or ""), quote=True)
    escaped_title = escape(str(title or "Viper Live Audio"), quote=True)
    didl = (
        '<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
        'xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/" '
        'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">'
        '<item id="R:0/0/0" parentID="R:0/0" restricted="true">'
        f"<dc:title>{escaped_title}</dc:title>"
        "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
        f'<res protocolInfo="http-get:*:audio/mpeg:*">{escaped_url}</res>'
        "</item>"
        "</DIDL-Lite>"
    )
    return escape(didl, quote=True)


def _sonos_radio_uri(media_url):
    text = str(media_url or "").strip()
    if text.startswith("x-rincon-mp3radio://"):
        return text
    return f"x-rincon-mp3radio://{text}"


def _sonos_soap(endpoint, action, body, timeout=8):
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body}</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=envelope,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def _send_pushover(token, user, title, message, url="", url_title=""):
    data = {
        "token": str(token or ""),
        "user": str(user or ""),
        "title": str(title or "Viper Core"),
        "message": str(message or ""),
    }
    if url:
        data["url"] = str(url)
        data["url_title"] = str(url_title or "Open Live Doorbell")
    payload = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def _clean(value):
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _payload_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "enabled"}
    return bool(value)


def _title(value):
    return str(value or "").replace("_", " ").title()


def _door_key(value):
    text = _clean(value)
    return "back" if text.startswith("back") else "front"


def _door_label(door):
    return "Back door" if _door_key(door) == "back" else "Front door"


def _doorbell_title(door):
    return "Viper Back Doorbell" if _door_key(door) == "back" else "Viper Front Doorbell"


def _doorbell_live_url(config, door, session_id=""):
    base = str(getattr(config, "external_base_url", "") or "").strip().rstrip("/")
    if not base:
        return ""
    if session_id:
        return f"{base}/doorbell/session/{urllib.parse.quote(str(session_id), safe='')}"
    query = urllib.parse.urlencode(
        {
            "page": "doorbells",
            "live": _door_key(door),
            "mode": "gemini_true_live",
            "speak": "browser",
        }
    )
    return f"{base}/?{query}"


def _speaker_media_base_url(config):
    base = str(getattr(config, "speaker_base_url", "") or "").strip().rstrip("/")
    if base:
        return base
    base = str(getattr(config, "external_base_url", "") or "").strip().rstrip("/")
    if base.startswith("http://100.") or base.startswith("https://100."):
        return "http://homeassistant.local:8099"
    return base


def _door_specific_text(door, message):
    text = str(message or "").strip()
    if _door_key(door) == "back":
        return text.replace("Front door", "Back door").replace("front door", "back door")
    return text.replace("Back door", "Front door").replace("back door", "front door")


def _door_alert_message(door, message):
    text = _door_specific_text(door, message)
    label = _door_label(door)
    if text.lower().startswith(label.lower()):
        return text
    return f"{label}: {text}"


def _push_allowed(event_type):
    return _clean(event_type) in {"doorbell", "doorbell_video"}


def _parse_ha_time(value):
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _vacuum_message(event, error="", source="vacuum", messages=None):
    labels = {
        "departure": "Cinderella started cleaning.",
        "washing": "Cinderella is washing the mop.",
        "emptying": "Cinderella is emptying the bin.",
        "returning": "Cinderella is heading back to the dock.",
        "victory": "Cinderella is back at the dock.",
        "paused": "Cinderella is paused.",
        "drying": "Cinderella is drying the mop.",
        "status_update": "Cinderella status changed.",
        "error": "Cinderella needs attention.",
    }
    if isinstance(messages, dict):
        bucket = event
        if event == "error":
            error_key = _clean(error)
            specific = messages.get("specific_errors") if isinstance(messages.get("specific_errors"), dict) else {}
            specific_keys = [error_key]
            if source == "dock" and error_key:
                specific_keys.insert(0, f"dock_{error_key}")
            for key in specific_keys:
                choices = [str(item).strip() for item in specific.get(key, []) if str(item).strip()]
                if choices:
                    return random.choice(choices).replace("{error}", error)
            bucket = "dock_error_templates" if source == "dock" else "vacuum_error_templates"
        choices = [str(item).strip() for item in messages.get(bucket, []) if str(item).strip()]
        if choices:
            return random.choice(choices).replace("{error}", error)
    return labels.get(event, f"Cinderella {event.replace('_', ' ')}.")


def _vacuum_event_key(value):
    text = _clean(value)
    aliases = {
        "starting": "departure",
        "cleaning": "departure",
        "mopping": "departure",
        "spot_cleaning": "departure",
        "zoned_cleaning": "departure",
        "zone_cleaning": "departure",
        "segment_cleaning": "departure",
        "mapping": "departure",
        "patrol": "departure",
        "robot_status_mopping": "departure",
        "clean_mop_cleaning": "departure",
        "clean_mop_mopping": "departure",
        "segment_mopping": "departure",
        "segment_clean_mop_cleaning": "departure",
        "segment_clean_mop_mopping": "departure",
        "zoned_mopping": "departure",
        "zoned_clean_mop_cleaning": "departure",
        "zoned_clean_mop_mopping": "departure",
        "washing_mop": "washing",
        "washing_the_mop": "washing",
        "washing_the_mop_2": "washing",
        "back_to_dock_washing_duster": "washing",
        "emptying": "emptying",
        "emptying_bin": "emptying",
        "emptying_dustbin": "emptying",
        "emptying_the_bin": "emptying",
        "returning": "returning",
        "returning_home": "returning",
        "docking": "returning",
        "going_to_target": "returning",
        "going_to_wash_the_mop": "returning",
        "attaching_the_mop": "returning",
        "detaching_the_mop": "returning",
        "air_drying_stopping": "returning",
        "charging": "victory",
        "docked": "victory",
        "charging_complete": "victory",
        "idle": "victory",
        "paused": "paused",
        "remote_control_active": "paused",
        "manual_mode": "paused",
        "in_call": "paused",
        "locked": "paused",
        "drying_the_mop": "drying",
        "mop_drying": "drying",
        "charger_disconnected": "status_update",
        "status": "status_update",
        "charging_problem": "status_update",
        "shutting_down": "status_update",
        "updating": "status_update",
        "device_offline": "status_update",
        "egg_attack": "status_update",
        "unknown": "status_update",
        "unavailable": "status_update",
    }
    return aliases.get(text, text or "status_update")
