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

Add Home Assistant's built-in [Ring integration](https://www.home-assistant.io/integrations/ring/) in Settings > Devices & services. Use the owner's Ring credentials and complete verification inside Home Assistant, not in Viper or a chat. Confirm that each doorbell has a `camera.*_live_view` and `event.*_ding` entity.

For a new Ring-only installation, do not install Ring-MQTT or Mosquitto. Viper opens live video through Home Assistant and listens for its native ding event. Choose the press event, not a motion event. Ring remains cloud-dependent.

Existing RTSP installations can keep their streams and switches. RTSP is still available for optional Smart, Detailed, true Live and manual video workflows; the native camera currently supports Fast descriptions.

No Viper-specific YAML automation package is required.

## 3. Install Viper Core

Add `https://github.com/corykad/viper-core-addon` in the Home Assistant app store's Repositories menu, install Viper Core, enable Start on boot and Watchdog, and start it. Open Web UI. The first page is Setup.

For a local install before repository publication, place the clean package's `viper_core` directory under Home Assistant's `/addons` directory, refresh the app store, and install Viper Core under Local apps. Do not copy the whole Windows project or the old `viper_core_package.yaml`.

## 4. Complete Setup

1. Refresh Device List.
2. Select the Main Doorbell `event.*_ding` Press Event and `camera.*_live_view` Live Camera. Leave the Video Source on Built-in Ring. Enable the second doorbell only when needed.
3. Enable Listen for doorbell presses automatically.
4. Select the Doorbell Speaker and its connection type. For Sonos or Google Cast, select the Home Assistant Speech Provider.
5. Select the image-description provider and enter that provider's API key.
6. Save Doorbell Setup.
7. Run Test Announcement Speaker and listen for it.
8. Run Test Main Camera Description. It must return an actual description. Repeat the camera test for a second doorbell if enabled.
9. Press each physical doorbell. Hear its announcement and refresh the device list to confirm the event time. Leave at least 30 seconds between repeated presses of the same bell to allow the default duplicate filter to expire.
10. Confirm that you heard the tests, then choose Finish Setup and Open Dashboard.

An accepted speaker request does not prove it was audible. The final confirmation is deliberately based on what you heard. Camera tests can incur API charges.

The initial speaker choice can be changed later. Add more announcement destinations on Speakers. Voice controls the speech engine; Doorbells controls image models and prompts. Native Ring supports Fast descriptions; optional long-form live narration still requires an RTSP source. Start with Fast until the physical doorbell test passes.

## 5. Handoff And Backup

- Let the owner navigate Setup, Dashboard, Doorbells and Speakers with their screen reader. Check labels, focus, results and volume together.
- Restart Viper Core, then test another real press to confirm saved settings and automatic listening return.
- Test with the Windows PC off.
- Create a Home Assistant backup and configure the owner's preferred backup storage. Keep backup recovery details with the owner.

Ring, the AI provider and some speech providers need internet access. Keep the normal Ring chime/notifications available if one of those services is unavailable.

## If A Test Fails

- **No events:** check that automatic listening is enabled, Viper is armed, the listener says Connected, and the selected entity changes when the button is pressed. Motion is not a button press.
- **No image description:** check the built-in Ring live view in Home Assistant, the selected camera, the AI API key and the configured model's availability to that account. A live session can occasionally start slowly; try the camera test again.
- **Speaker request accepted but silent:** check speaker volume, Home Assistant media reachability and the chosen speech provider. Test in Home Assistant too.
- **Alexa fails:** check Alexa Media Player authentication and its announce action. This is independent of the heat pump Matterbridge integration.
- **Duplicate announcements:** choose one native ding event per bell. Viper ignores its old Ring-MQTT router events after native listening is selected; check for any other household automations that also speak.

The web panel uses Home Assistant ingress by default; no host port is published. Optional direct media/phone links require separate URL and network configuration. Do not expose port 8099 publicly: it is an unauthenticated local control endpoint.

## Release Validation

The development checks cover clean and saved-state behavior, event subscription, setup validation, keyboard operation and automated accessibility rules. The repository's container workflow builds for ARM64 (Green) and AMD64. Check its latest result before installation. Final Ring authentication, camera access, speaker audibility and screen-reader usability must be tested on the new household's equipment.
