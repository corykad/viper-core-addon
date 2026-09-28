# Viper Core For Home Assistant

Ring doorbell announcements and AI image descriptions on Home Assistant Green.
New installations start with an empty, doorbell-focused setup.

[Add this repository to Home Assistant](https://my.home-assistant.io/redirect/supervisor_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fcorykad%2Fviper-core-addon)

Then install **Viper Core**, start it, and open its Web UI.

Read the [setup guide](viper_core/DOCS.md) before your installation visit. It covers the built-in Ring integration, Google Cast/Sonos/Alexa, API keys, audible tests and backups. No MQTT broker is needed for a new Ring-only install.

This source distribution includes only the add-on and its documentation. It does not include saved household settings, credentials, recordings, generated audio, or the old household automation package. Optional device code remains for legacy compatibility, but those features are disabled on clean installations.

See [Viper Core details](viper_core/README.md) and the [container checks](https://github.com/corykad/viper-core-addon/actions).
