# Set Up Viper Core On A New Green

## Before Visiting

- Have the Green, its power supply, and an Ethernet cable available.
- Have the owner's Ring account and access to its two-factor authentication.
- Make sure each doorbell works in the Ring app.
- Choose a speaker. Sonos or Google Cast can use Home Assistant's built-in integrations; an existing Alexa speaker needs the custom Alexa Media Player integration for Viper announcements.
- Have an OpenAI or Gemini API key with API billing available. A chat subscription is separate from API access. Viper sends camera images to the chosen provider.

## 1. Prepare Home Assistant

Connect the Green to the router and power, complete Home Assistant onboarding, and install available updates. Use the owner's account and network details.

Add the speaker in Settings > Devices & services. Follow the relevant guide:

- [Sonos](https://www.home-assistant.io/integrations/sonos/)
- [Google Cast](https://www.home-assistant.io/integrations/cast/)
- [Alexa Media Player](https://github.com/alandtse/alexa_media_player/wiki)

For Sonos or Google Cast, configure a Home Assistant text-to-speech integration that exposes a `tts.*` entity, such as [Google Translate TTS](https://www.home-assistant.io/integrations/google_translate/). Confirm the speaker can play a test from Home Assistant. Speakers must be able to reach Home Assistant's media URL; guest Wi-Fi isolation can prevent this.

Alexa is a separate route and does not need the Home Assistant speech-provider selection. Its integration must provide the `notify.alexa_media` announce action.

## 2. Connect Ring

Follow the [Ring-MQTT app installation guide](https://github.com/tsightler/ring-mqtt/wiki/Installation-(Home-Assistant-Addon)) to configure an MQTT broker, Home Assistant MQTT discovery, Ring sign-in and video streaming. Use the owner's Ring credentials and complete its authentication flow.

From the Ring device's discovered entities, identify its **ding/press** binary sensor and its live stream switch. Obtain the RTSP URL using the Ring-MQTT documentation for your installation. Keep any credentials embedded in that URL private.

Viper can alternatively listen to a press `event.*` entity from the official [Ring integration](https://www.home-assistant.io/integrations/ring/). This does not remove Viper's requirement for a working RTSP stream for image descriptions. Choose one press entity for each physical doorbell, not a motion entity.

No Viper-specific YAML automation package is required.

## 3. Install Viper Core

Add `https://github.com/corykad/viper-core-addon` in the Home Assistant app store's Repositories menu, install Viper Core, enable Start on boot and Watchdog, and start it. Open Web UI. The first page is Setup.

For a local install before repository publication, place the clean package's `viper_core` directory under Home Assistant's `/addons` directory, refresh the app store, and install Viper Core under Local apps. Do not copy the whole Windows project or the old `viper_core_package.yaml`.

## 4. Complete Setup

1. Refresh Device List.
2. Select Main Doorbell Press Event and enter its RTSP URL. Select the stream switch if Ring-MQTT requires it. Enable the second doorbell only when needed.
3. Enable Listen for doorbell presses automatically.
4. Select the Doorbell Speaker and its connection type. For Sonos or Google Cast, select the Home Assistant Speech Provider.
5. Select the image-description provider and enter that provider's API key.
6. Save Doorbell Setup.
7. Run Test Announcement Speaker and listen for it.
8. Run Test Main Camera Description. It must return an actual description. Repeat the camera test for a second doorbell if enabled.
9. Press each physical doorbell. Hear its announcement and refresh the device list to confirm the event time. Leave at least 30 seconds between repeated presses of the same bell to allow the default duplicate filter to expire.
10. Confirm that you heard the tests, then choose Finish Setup and Open Dashboard.

An accepted speaker request does not prove it was audible. The final confirmation is deliberately based on what you heard. Camera tests can incur API charges.

The initial speaker choice can be changed later. Add more announcement destinations on Speakers. Voice controls the speech engine; Doorbells controls image models, prompts and optional live narration. Start with still-image descriptions until the basic path has passed the physical doorbell test.

## 5. Handoff And Backup

- Let the owner navigate Setup, Dashboard, Doorbells and Speakers with their screen reader. Check labels, focus, results and volume together.
- Restart Viper Core, then test another real press to confirm saved settings and automatic listening return.
- Test with the Windows PC off.
- Create a Home Assistant backup and configure the owner's preferred backup storage. Keep backup recovery details with the owner.

Ring, the AI provider and some speech providers need internet access. Keep the normal Ring chime/notifications available if one of those services is unavailable.

## If A Test Fails

- **No events:** check that automatic listening is enabled, Viper is armed, the listener says Connected, and the selected entity changes when the button is pressed. Motion is not a button press.
- **No image description:** check the stream in Ring-MQTT, its stream switch, the API key and the configured model's availability to that account.
- **Speaker request accepted but silent:** check speaker volume, Home Assistant media reachability and the chosen speech provider. Test in Home Assistant too.
- **Alexa fails:** check Alexa Media Player authentication and its announce action. This is independent of the heat pump Matterbridge integration.
- **Duplicate announcements:** remove any old Viper forwarding automation when using the built-in listener. Keep only one selected press entity per bell.

The web panel uses Home Assistant ingress by default; no host port is published. Optional direct media/phone links require separate URL and network configuration. Do not expose port 8099 publicly: it is an unauthenticated local control endpoint.

## Release Validation

The development checks cover clean and saved-state behavior, event subscription, setup validation, keyboard operation and automated accessibility rules. The repository's container workflow builds for ARM64 (Green) and AMD64. Check its latest result before installation. Final Ring authentication, camera access, speaker audibility and screen-reader usability must be tested on the new household's equipment.
