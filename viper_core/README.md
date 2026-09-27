# Viper Core

Viper Core is the Home Assistant add-on version of Viper's always-on brain.

Viper Core runs Ring doorbell announcements and AI image descriptions on Home Assistant, including Home Assistant Green. A Windows PC is not needed for daily use.

New installations start with one doorbell slot, no selected speakers or credentials, and the heat pump, refrigerator, vacuum and Matterbridge features disabled. Setup also supports a second doorbell. Existing saved installations keep their settings.

## What This Add-on Does Now

- Starts automatically with Home Assistant.
- Uses the Home Assistant Supervisor token when available.
- Falls back to a configured Home Assistant URL and token when needed.
- Exposes a health page on the add-on ingress panel.
- Logs Home Assistant API connectivity every health check interval.
- Keeps secrets out of the health page.
- Receives Home Assistant events at `/event/doorbell`, `/event/fridge`, `/event/vacuum`, `/event/ice_maker`, and `/event/hvac`.
- Creates Home Assistant notifications or calls configured notification/speaker services.

## Doorbell Setup

The Setup page discovers Home Assistant entities so you can select the doorbell press event, speaker and speech provider. Add the live RTSP stream supplied by Ring-MQTT and your image-description API key. Save, test the speaker and camera, then press the real doorbell and confirm that you heard it.

Viper subscribes to Home Assistant events directly. A separate Viper automation package is not needed for a new installation. Sonos and Google Cast speakers use Home Assistant's media player integration. Alexa uses the separately installed Alexa Media Player integration.

## Existing Installations

Saved devices, routes and settings are retained. The new automatic listener stays off until explicitly enabled, so existing YAML doorbell automations keep working. Before switching to the listener, disable the matching Viper doorbell forwarding automations to avoid duplicate announcements.

The clean setup currently configures doorbells and speakers. Other device controls remain available to legacy installations; general setup for those devices is not part of the new-household wizard.

## Local Development

Run the Python module directly:

```powershell
python -m py_compile ha_addons\viper_core\viper_core\__main__.py ha_addons\viper_core\viper_core\config.py ha_addons\viper_core\viper_core\ha.py ha_addons\viper_core\viper_core\health_server.py
```

## Installing On Home Assistant Green

Add `https://github.com/corykad/viper-core-addon` in the Home Assistant app store's Repositories menu, install Viper Core, enable Start on boot and Watchdog, and start it. Choose Open Web UI.

Follow [the Green setup guide](DOCS.md). Use your own Ring account, speakers and AI API key. Do not restore someone else's Home Assistant backup or copy their Viper settings.

## Accessibility

The web interface uses labeled native controls, keyboard navigation, a skip link, and announced command results. Automated WCAG 2.2 AA checks and keyboard tests are part of development. These checks do not replace testing with the user's screen reader and actual speakers.
