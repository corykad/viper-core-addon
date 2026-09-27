import os
import platform
import shutil
import time
from pathlib import Path


def build_status_snapshot(ha, state, control_state):
    control = control_state.public_state() if control_state else {}
    return {
        "system": _system_status(),
        "home_assistant": _home_assistant_status(ha, state),
        "storage": _storage_status(),
        "speakers": _speaker_status(ha, control.get("speakers") or {}),
        "entities": _entity_status(state),
        "recent_issues": _recent_issues(state.get("recent_events") or []),
    }


def _system_status():
    uptime_seconds = _read_uptime_seconds()
    return {
        "hostname": platform.node() or "unknown",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uptime_seconds": uptime_seconds,
        "uptime": _format_duration(uptime_seconds),
        "load_average": _load_average(),
        "cpu": _cpu_status(),
        "memory": _memory_status(),
        "process": _process_status(),
    }


def _home_assistant_status(ha, state):
    status = dict(state.get("home_assistant") or {})
    config = {}
    if ha and ha.available():
        try:
            config = ha.get_config() or {}
        except Exception as exc:
            config = {"error": str(exc)}
    return {
        "api": status,
        "config": {
            "version": config.get("version", ""),
            "location_name": config.get("location_name", ""),
            "time_zone": config.get("time_zone", ""),
            "unit_system": (config.get("unit_system") or {}).get("temperature", ""),
            "error": config.get("error", ""),
        },
    }


def _storage_status():
    paths = [
        ("Add-on Data", "/data"),
        ("Media", "/media"),
        ("Root", "/"),
    ]
    rows = []
    seen = set()
    for label, path in paths:
        if not Path(path).exists():
            continue
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            usage = shutil.disk_usage(path)
        except OSError as exc:
            rows.append({"label": label, "path": path, "ok": False, "message": str(exc)})
            continue
        used_percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0
        rows.append({
            "label": label,
            "path": path,
            "ok": True,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "used_percent": used_percent,
        })
    return rows


def _speaker_status(ha, speakers):
    rows = []
    for name, speaker in sorted((speakers or {}).items()):
        item = {
            "name": name,
            "enabled": bool(speaker.get("enabled", True)),
            "type": str(speaker.get("type") or "ha"),
            "id": speaker.get("id", ""),
            "routes": _speaker_routes(speaker),
            "state": "not checked",
            "ok": False,
            "message": "",
        }
        if not item["enabled"]:
            item["state"] = "disabled"
            item["message"] = "Disabled in Viper."
            rows.append(item)
            continue
        if item["type"] == "sonos":
            item.update(_sonos_status(item["id"]))
            rows.append(item)
            continue
        if not ha or not ha.available():
            item["message"] = "Home Assistant API is unavailable."
            rows.append(item)
            continue
        try:
            state = ha.get_state(item["id"]) or {}
            attrs = state.get("attributes") or {}
            item.update({
                "state": state.get("state") or "unknown",
                "ok": str(state.get("state") or "").lower() not in {"unknown", "unavailable"},
                "volume": attrs.get("volume_level"),
                "muted": attrs.get("is_volume_muted"),
                "friendly_name": attrs.get("friendly_name", ""),
            })
        except Exception as exc:
            item["message"] = str(exc)
        rows.append(item)
    return rows


def _entity_status(state):
    dependencies = state.get("dependencies") or {}
    devices = state.get("devices") or {}
    return {
        "dependencies_ok": bool(dependencies.get("ok")),
        "dependencies_message": dependencies.get("message", ""),
        "device_ok": bool(devices.get("ok")),
        "device_message": devices.get("message", ""),
        "dependency_entities": dependencies.get("entities") or {},
    }


def _recent_issues(events):
    rows = []
    for event in reversed(list(events or [])):
        message = str(event.get("message") or "")
        if event.get("ok") and "failed" not in message.lower() and "needs attention" not in message.lower():
            continue
        rows.append(event)
        if len(rows) >= 10:
            break
    return rows


def _memory_status():
    values = {}
    for line in _read_lines("/proc/meminfo"):
        parts = line.split()
        if len(parts) >= 2:
            try:
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024
            except ValueError:
                pass
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = max(0, total - available) if total else 0
    return {
        "total": total,
        "available": available,
        "used": used,
        "used_percent": round((used / total) * 100, 1) if total else 0,
    }


def _cpu_status():
    model = ""
    mhz_values = []
    processors = 0
    for line in _read_lines("/proc/cpuinfo"):
        if line.startswith("processor"):
            processors += 1
        if line.startswith("model name") and not model:
            model = line.split(":", 1)[-1].strip()
        if line.startswith("cpu MHz"):
            try:
                mhz_values.append(float(line.split(":", 1)[-1].strip()))
            except ValueError:
                pass
    return {
        "model": model or platform.processor() or "unknown",
        "cores": processors or (os.cpu_count() or 0),
        "speed_mhz": round(max(mhz_values), 1) if mhz_values else 0,
    }


def _process_status():
    try:
        rss_pages = int(Path("/proc/self/statm").read_text(encoding="utf-8").split()[1])
        rss = rss_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        rss = 0
    return {"pid": os.getpid(), "memory": rss}


def _load_average():
    try:
        one, five, fifteen = os.getloadavg()
        return {"one": round(one, 2), "five": round(five, 2), "fifteen": round(fifteen, 2)}
    except OSError:
        return {"one": 0, "five": 0, "fifteen": 0}


def _read_uptime_seconds():
    try:
        return int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
    except (OSError, IndexError, ValueError):
        return 0


def _read_lines(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _format_duration(seconds):
    seconds = max(0, int(seconds or 0))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _format_bytes(value):
    value = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "0 B"


def _speaker_routes(speaker):
    routes = []
    for key, label in (("doorbell", "Doorbell"), ("fridge", "Fridge"), ("utilities", "Utilities")):
        if speaker.get(key, True):
            routes.append(label)
    return ", ".join(routes) if routes else "No routes"


def _sonos_status(host):
    import urllib.request

    host = str(host or "").strip()
    if not host:
        return {"state": "missing host", "ok": False, "message": "No Sonos host configured."}
    request = urllib.request.Request(f"http://{host}:1400/xml/device_description.xml")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            response.read(256)
        return {"state": "reachable", "ok": True, "latency_ms": int((time.monotonic() - started) * 1000)}
    except Exception as exc:
        return {"state": "unreachable", "ok": False, "message": str(exc)}
