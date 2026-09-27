"""Runs in the built container without accounts or network access."""
import importlib
import json
import sys
import subprocess
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "viper_core"))
from viper_core.control import ControlState
from viper_core.web_ui import render_page


class CleanImageTests(unittest.TestCase):
    @unittest.skipUnless(Path("/app/viper_core").is_dir(), "Container startup check")
    def test_clean_app_starts_and_serves_setup(self):
        process = subprocess.Popen([sys.executable, "-m", "viper_core"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail("App exited during startup")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8099/", timeout=1) as response:
                        html = response.read().decode()
                    self.assertIn("Doorbell Setup", html)
                    self.assertNotIn('href="?page=heat-pumps"', html)
                    return
                except OSError:
                    time.sleep(0.1)
            self.fail("App did not serve the setup page")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_all_runtime_modules_import(self):
        for name in ("__main__", "doorbell_listener", "setup", "vision", "live", "tts"):
            importlib.import_module("viper_core." + name)

    def test_fresh_state_has_no_household_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ControlState(Path(directory) / "control_state.json")
            self.assertEqual(state.state["speakers"], {})
            self.assertFalse(state.state["setup_complete"])
            self.assertEqual([key for key, value in state.state["features"].items() if value], ["doorbell"])
            for key in ("openai_api_key", "gemini_api_key", "front_door_stream_url", "back_door_stream_url", "front_door_trigger"):
                self.assertFalse(state.state["settings"][key])
            self.assertNotIn("192.168.", json.dumps(state.public_state()))
            self.assertIn("Doorbell Setup", render_page({"control": state.public_state()}))
            self.assertTrue(state._save())
            self.assertEqual(ControlState(state.path).state["speakers"], {})


if __name__ == "__main__":
    unittest.main()
