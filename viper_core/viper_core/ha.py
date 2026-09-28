import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit


class HomeAssistantClient:
    def __init__(self, base_url, token, timeout=8):
        self.base_url = str(base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout

    def available(self):
        return bool(self.base_url and self.token)

    def websocket_url(self):
        parts = urlsplit(self.base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Home Assistant URL must use HTTP or HTTPS.")
        path = parts.path.rstrip("/")
        if parts.hostname == "supervisor" and path == "/core/api":
            path = "/core"
        return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                           path + "/websocket", "", ""))

    def api_status(self):
        started = time.monotonic()
        if not self.available():
            return {
                "ok": False,
                "reason": "missing_config",
                "message": "Home Assistant URL or token is not configured.",
                "latency_ms": 0,
            }

        request = urllib.request.Request(
            f"{self.base_url}/",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body) if body else {}
            message = payload.get("message") if isinstance(payload, dict) else ""
            return {
                "ok": True,
                "reason": "ok",
                "message": message or "Home Assistant API is reachable.",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except json.JSONDecodeError:
            return {
                "ok": False,
                "reason": "invalid_response",
                "message": "Home Assistant API returned invalid JSON.",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except urllib.error.HTTPError as exc:
            return {
                "ok": False,
                "reason": f"http_{exc.code}",
                "message": f"Home Assistant API returned HTTP {exc.code}.",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {
                "ok": False,
                "reason": "connection_failed",
                "message": str(exc),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }

    def get_state(self, entity_id):
        return self._request("GET", f"/states/{entity_id}")

    def get_states(self):
        return self._request("GET", "/states")

    def get_config(self):
        return self._request("GET", "/config")

    def get_binary(self, path):
        if not self.available():
            raise RuntimeError("Home Assistant URL or token is not configured.")
        url = f"{self.base_url}{'/' + str(path).lstrip('/')}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")

    def dependency_status(self, entity_ids):
        results = {}
        ok = True
        for entity_id in entity_ids or []:
            entity_id = str(entity_id or "").strip()
            if not entity_id:
                continue
            try:
                state = self.get_state(entity_id)
                current = str((state or {}).get("state") or "unknown").lower()
                healthy = bool(state) and current != "unavailable" and (entity_id.startswith("event.") or current != "unknown")
                results[entity_id] = {
                    "ok": healthy,
                    "state": current,
                    "friendly_name": ((state or {}).get("attributes") or {}).get("friendly_name", ""),
                }
                ok = ok and healthy
            except Exception as exc:
                results[entity_id] = {"ok": False, "state": "missing", "message": str(exc)}
                ok = False
        return {
            "ok": ok,
            "entities": results,
            "message": "Required Home Assistant entities are ready." if ok else "One or more required Home Assistant entities need attention.",
        }

    def call_service(self, domain_service, data=None, return_response=False):
        domain_service = str(domain_service or "").strip().replace(".", "/")
        suffix = "?return_response" if return_response else ""
        return self._request("POST", f"/services/{domain_service}{suffix}", data or {})

    def create_notification(self, title, message):
        return self.call_service(
            "persistent_notification/create",
            {"title": str(title or "Viper Core"), "message": str(message or "")},
        )

    def tts_get_url(self, message, platform="google_translate", language="en"):
        payload = {
            "message": str(message or ""),
            "platform": str(platform or "google_translate"),
            "language": str(language or "en"),
        }
        return self._request("POST", "/tts_get_url", payload)

    def _request(self, method, path, data=None):
        if not self.available():
            raise RuntimeError("Home Assistant URL or token is not configured.")
        url = f"{self.base_url}{'/' + str(path).lstrip('/')}"
        body = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=str(method or "GET").upper())
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw else None
