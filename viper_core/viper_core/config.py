import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path


LOGGER = logging.getLogger(__name__)
OPTIONS_PATH = Path("/data/options.json")
DEFAULT_EXTERNAL_BASE_URL = ""
DEFAULT_OPENAI_VISION_MODEL = "gpt-6-astra"
LEGACY_OPENAI_VISION_MODELS = {
    "gpt-5.5",
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
}


def normalize_openai_vision_model(model):
    model = str(model or "").strip()
    if not model or model in LEGACY_OPENAI_VISION_MODELS:
        return DEFAULT_OPENAI_VISION_MODEL
    return model


@dataclass(frozen=True)
class CoreConfig:
    log_level: str = "info"
    health_check_seconds: int = 60
    enabled_features: dict = field(default_factory=dict)
    notification_service: str = "persistent_notification.create"
    tts_service: str = "tts.google_say"
    tts_targets: list = field(default_factory=list)
    direct_sonos_targets: list = field(default_factory=list)
    alexa_notify_service: str = "notify.alexa_media"
    alexa_targets: list = field(default_factory=list)
    doorbell_speaker_service: str = ""
    fridge_speaker_service: str = ""
    vacuum_speaker_service: str = ""
    pushover_service: str = ""
    pushover_user_key: str = ""
    pushover_api_token: str = ""
    speaker_targets: list = field(default_factory=list)
    external_base_url: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""
    gemini_vision_model: str = "gemini-3.5-flash"
    openai_vision_model: str = DEFAULT_OPENAI_VISION_MODEL
    smartthings_config_entry_id: str = ""
    fridge_stale_auto_repair: bool = False
    fallback_ha_url: str = ""
    fallback_ha_token: str = ""
    supervisor_token: str = ""
    matterbridge_addon_slug: str = "246dd49f_matterbridge"

    @property
    def ha_url(self):
        if self.supervisor_token:
            return "http://supervisor/core/api"
        return self.fallback_ha_url.rstrip("/")

    @property
    def ha_token(self):
        return self.supervisor_token or self.fallback_ha_token

    def public_dict(self):
        return {
            "log_level": self.log_level,
            "health_check_seconds": self.health_check_seconds,
            "enabled_features": self.enabled_features,
            "notification_service": self.notification_service,
            "tts_service": self.tts_service,
            "tts_target_count": len(self.tts_targets),
            "direct_sonos_target_count": len(self.direct_sonos_targets),
            "alexa_notify_service": self.alexa_notify_service,
            "alexa_target_count": len(self.alexa_targets),
            "doorbell_speaker_service_configured": bool(self.doorbell_speaker_service),
            "fridge_speaker_service_configured": bool(self.fridge_speaker_service),
            "vacuum_speaker_service_configured": bool(self.vacuum_speaker_service),
            "pushover_service_configured": bool(self.pushover_service),
            "pushover_direct_configured": bool(self.pushover_user_key and self.pushover_api_token),
            "speaker_target_count": len(self.speaker_targets),
            "external_base_url_configured": bool(self.external_base_url),
            "gemini_vision_configured": bool(self.gemini_api_key),
            "openai_configured": bool(self.openai_api_key),
            "gemini_vision_model": self.gemini_vision_model,
            "openai_vision_model": self.openai_vision_model,
            "smartthings_config_entry_id_configured": bool(self.smartthings_config_entry_id),
            "fridge_stale_auto_repair": self.fridge_stale_auto_repair,
            "ha_mode": "supervisor" if self.supervisor_token else "fallback",
            "ha_url_configured": bool(self.ha_url),
            "ha_token_configured": bool(self.ha_token),
        }


def load_config(options_path=OPTIONS_PATH):
    data = {}
    if options_path.exists():
        try:
            data = json.loads(options_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not read add-on options: %s", exc)

    return CoreConfig(
        log_level=str(data.get("log_level") or "info").lower(),
        health_check_seconds=max(15, int(data.get("health_check_seconds") or 60)),
        enabled_features=dict(data.get("enabled_features") or {}),
        notification_service=str(data.get("notification_service") or "persistent_notification.create").strip(),
        tts_service=str(data.get("tts_service") or "tts.google_say").strip(),
        tts_targets=[str(item).strip() for item in (data.get("tts_targets") or []) if str(item).strip()],
        direct_sonos_targets=[
            str(item).strip()
            for item in data.get("direct_sonos_targets", [])
            if str(item).strip()
        ],
        alexa_notify_service=str(data.get("alexa_notify_service") or "notify.alexa_media").strip(),
        alexa_targets=[str(item).strip() for item in (data.get("alexa_targets") or []) if str(item).strip()],
        doorbell_speaker_service=str(data.get("doorbell_speaker_service") or "").strip(),
        fridge_speaker_service=str(data.get("fridge_speaker_service") or "").strip(),
        vacuum_speaker_service=str(data.get("vacuum_speaker_service") or "").strip(),
        pushover_service=str(data.get("pushover_service") or "").strip(),
        pushover_user_key=str(data.get("pushover_user_key") or os.environ.get("PUSHOVER_USER") or "").strip(),
        pushover_api_token=str(data.get("pushover_api_token") or os.environ.get("PUSHOVER_TOKEN") or "").strip(),
        speaker_targets=[str(item).strip() for item in (data.get("speaker_targets") or []) if str(item).strip()],
        external_base_url=str(
            data.get("external_base_url")
            or os.environ.get("VIPER_EXTERNAL_BASE_URL")
            or DEFAULT_EXTERNAL_BASE_URL
        ).rstrip("/"),
        gemini_api_key=str(data.get("gemini_api_key") or os.environ.get("GEMINI_KEY") or ""),
        openai_api_key=str(data.get("openai_api_key") or os.environ.get("OPENAI_API_KEY") or ""),
        gemini_vision_model=str(data.get("gemini_vision_model") or "gemini-3.5-flash").strip(),
        openai_vision_model=normalize_openai_vision_model(data.get("openai_vision_model")),
        smartthings_config_entry_id=str(data.get("smartthings_config_entry_id") or "").strip(),
        fridge_stale_auto_repair=bool(data.get("fridge_stale_auto_repair", False)),
        fallback_ha_url=str(data.get("fallback_ha_url") or "").rstrip("/"),
        fallback_ha_token=str(data.get("fallback_ha_token") or ""),
        supervisor_token=os.environ.get("SUPERVISOR_TOKEN", ""),
        matterbridge_addon_slug=str(data.get("matterbridge_addon_slug") or "246dd49f_matterbridge"),
    )


def configure_logging(level_name):
    levels = {
        "trace": logging.DEBUG,
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    logging.basicConfig(
        level=levels.get(str(level_name).lower(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
