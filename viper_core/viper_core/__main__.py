import logging
import signal
import shutil
import threading
import time

from .config import configure_logging, load_config
from .control import ControlApi, ControlState
from .events import EventProcessor
from .ha import HomeAssistantClient
from .health_server import HealthServer
from .live import doorbell_live_events, gemini_true_live_doorbell_events
from .status import build_status_snapshot
from .bridge_status import bridge_status
from . import __version__
from .doorbell_listener import DoorbellListener
from .setup import SetupService


LOGGER = logging.getLogger("viper_core")
STOP_EVENT = threading.Event()
REQUIRED_ENTITY_IDS = [
    "switch.refrigerator_cubed_ice",
    "binary_sensor.refrigerator_fridge_door",
    "binary_sensor.refrigerator_freezer_door",
    "input_boolean.keep_ice_maker_on",
    "input_boolean.ice_maker_auto_refill_running",
    "counter.ice_usage_counter",
    "climate.office_heat_pump_alexa",
    "climate.living_room_heat_pump_alexa",
    "climate.kitchen_heat_pump_alexa",
    "climate.jamie_s_room_heat_pump_alexa",
    "climate.master_bedroom_heat_pump_alexa",
]


def _query_value(query, key, default):
    values = query.get(key) if isinstance(query, dict) else None
    if isinstance(values, list) and values:
        return values[-1]
    return default


def main():
    config = load_config()
    configure_logging(config.log_level)
    client = HomeAssistantClient(config.ha_url, config.ha_token)
    control_state = ControlState()
    control_api = ControlApi(control_state, client)
    events = EventProcessor(config, client, control_state)
    listener = DoorbellListener(config, control_state, events.handle)
    setup = SetupService(control_state, client, config, events, listener)
    control_api.setup_service = setup
    listener.start()
    state = {
        "ok": False,
        "service": "Viper Core",
        "version": __version__,
        "config": config.public_dict(),
        "home_assistant": {"ok": False, "message": "No health check has run yet."},
        "dependencies": {"ok": False, "message": "No dependency check has run yet."},
        "devices": {"ok": False, "message": "No device status refresh has run yet.", "heat_pumps": [], "airflow": [], "vacuum": {}, "refrigerator": {}},
        "ha_status": {},
        "recent_events": events.recent_events,
    }

    def current_state():
        state["recent_events"] = list(events.recent_events)
        state["control"] = control_state.public_state()
        state["hvac_commands"] = control_api.hvac_commands()
        state["hvac_history"] = control_api.hvac_history()
        state["fridge_health"] = events.fridge_health
        state["doorbell_listener"] = dict(listener.status)
        state["setup"] = setup.snapshot()
        state["runtime"] = {"ffmpeg": shutil.which("ffmpeg") or ""}
        state["ha_status"] = build_status_snapshot(client, state, control_state)
        return dict(state)

    def live_handler(door, query):
        seconds = _query_value(query, "seconds", 30)
        chunk_seconds = _query_value(query, "chunk_seconds", 3)
        speak = str(_query_value(query, "speak", "false")).strip().lower() in {"1", "true", "yes", "on"}
        all_speakers = str(_query_value(query, "all_speakers", "false")).strip().lower() in {"1", "true", "yes", "on"}
        force_confirmation = str(_query_value(query, "force_confirmation", "false")).strip().lower() in {"1", "true", "yes", "on"}
        mode = str(_query_value(query, "mode", "rolling")).strip().lower()
        session_id = str(_query_value(query, "session_id", "") or "").strip()
        if mode == "gemini_true_live":
            return gemini_true_live_doorbell_events(config, client, control_state, events.handle, door, seconds, speak, all_speakers, force_confirmation, session_id=session_id)
        return doorbell_live_events(config, client, control_state, events.handle, door, seconds, chunk_seconds, speak)

    server = HealthServer("0.0.0.0", 8099, current_state, events.handle, control_api, live_handler)
    server_thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    server_thread.start()

    def stop(_signum=None, _frame=None):
        STOP_EVENT.set()
        listener.stop_event.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    LOGGER.info("Viper Core started. Health page is listening on port 8099.")
    try:
        while not STOP_EVENT.is_set():
            ha_status = client.api_status()
            required = []
            for entity in REQUIRED_ENTITY_IDS:
                feature = "hvac" if entity.startswith("climate.") else "fridge" if "door" in entity else "ice_maker"
                if control_state.feature_enabled(feature):
                    required.append(entity)
            settings = control_state.state.get("settings", {})
            for door in ("front", "back"):
                if door == "front" or settings.get("back_door_enabled"):
                    required.extend(settings[key] for key in (f"{door}_door_trigger", f"{door}_door_live_stream_switch") if settings.get(key))
            dependency_status = client.dependency_status(required) if ha_status.get("ok") else {
                "ok": False,
                "message": "Skipped because Home Assistant API is not ready.",
                "entities": {},
            }
            state["home_assistant"] = ha_status
            state["matterbridge"] = bridge_status(config) if control_state.feature_enabled("matterbridge") else {}
            if ha_status.get("ok") and control_state.feature_enabled("fridge"):
                events._fridge_stale_status()
            state["dependencies"] = dependency_status
            state["devices"] = control_api.device_status() if ha_status.get("ok") else {
                "ok": False,
                "message": "Skipped because Home Assistant API is not ready.",
                "heat_pumps": [],
                "airflow": [],
                "vacuum": {},
                "refrigerator": {},
            }
            state["ok"] = bool(ha_status.get("ok") and dependency_status.get("ok"))
            if ha_status.get("ok"):
                LOGGER.info("Home Assistant API reachable in %sms.", ha_status.get("latency_ms"))
                if not dependency_status.get("ok"):
                    LOGGER.warning("Viper Core dependency check failed: %s", dependency_status.get("message"))
            else:
                LOGGER.warning("Home Assistant API check failed: %s", ha_status.get("message"))
            STOP_EVENT.wait(config.health_check_seconds)
    finally:
        listener.stop_event.set()
        server.shutdown()
        LOGGER.info("Viper Core stopped.")


if __name__ == "__main__":
    main()
