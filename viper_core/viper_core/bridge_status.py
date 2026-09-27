"""Read-only Matterbridge diagnostics from Supervisor logs."""

import json
import re
import time
import urllib.request


def parse_bridge_logs(logs):
    result = {"connection": "unknown", "last_command": "", "connection_evidence": ""}
    for raw in logs.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw)
        lower = line.lower()
        connection = None
        if "[matterbridge hass plugin]" in lower:
            if "disconnected from home assistant" in lower:
                connection = "disconnected"
            elif "connected to home assistant" in lower:
                connection = "connected"
        elif "[homeassistant]" in lower:
            if "restart the plugin to reconnect" in lower:
                connection = "disconnected"
            elif "reconnecting" in lower:
                connection = "reconnecting"
        if connection:
            result["connection"] = connection
            # Keep only the timestamp and classification, never URLs or credentials.
            stamp = re.search(r"\[(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]", line)
            result["connection_evidence"] = (stamp.group(1) + " - " if stamp else "") + connection
        if "subscribed attribute thermostat:" in lower and "changed from" in lower:
            match = re.search(r"\[([^\]]+)\] Subscribed attribute Thermostat:(\w+).*?changed from (\S+) to (\S+)", line)
            if match:
                result["last_command"] = f"{match[1]}: {match[2]} {match[3]} to {match[4]} (delivery unconfirmed)"
    return result


def bridge_status(config):
    checked_at = int(time.time())
    result = {"connection": "unknown", "checked_at": checked_at,
              "last_successful_command": None,
              "command_note": "Matterbridge does not expose a verified command-success history. Received commands are not proof of delivery."}
    token = getattr(config, "supervisor_token", "")
    if not token:
        return {**result, "message": "Supervisor access is needed to check Matterbridge."}
    slug = getattr(config, "matterbridge_addon_slug", "246dd49f_matterbridge")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug):
        return {**result, "message": "Invalid Matterbridge add-on identifier."}
    def read(path):
        request = urllib.request.Request(f"http://supervisor/addons/{slug}/{path}",
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read().decode("utf-8", errors="replace")
    try:
        info = json.loads(read("info"))
        data = info.get("data") or {}
        if data.get("state") != "started":
            return {**result, "connection": "disconnected", "message": "Matterbridge add-on is not running."}
        evidence = parse_bridge_logs(read("logs?lines=2000"))
        return {**result, **evidence, "message": "Latest connection evidence from Matterbridge logs; not an end-to-end Alexa test."}
    except Exception:
        return {**result, "message": "Matterbridge status could not be checked. Check Supervisor access and the add-on identifier."}
