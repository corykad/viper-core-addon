import json
from datetime import datetime, timezone
from html import escape
from urllib.parse import quote


CHIME_EVENTS = [
    ("front_doorbell", "Front Doorbell"),
    ("back_doorbell", "Back Doorbell"),
    ("fridge_open", "Fridge Open"),
    ("fridge_closed", "Fridge Closed"),
    ("freezer_open", "Freezer Open"),
    ("freezer_closed", "Freezer Closed"),
]

PAGES = [
    ("setup", "Setup"),
    ("dashboard", "Dashboard"),
    ("ha-status", "HA Status"),
    ("doorbells", "Doorbells"),
    ("speakers", "Speakers"),
    ("chimes", "Chimes"),
    ("refrigerator", "Refrigerator"),
    ("heat-pumps", "Heat Pumps"),
    ("vacuum", "Vacuum"),
    ("vacuum-controls", "Vacuum Controls"),
    ("vacuum-messages", "Vacuum Messages"),
    ("voice", "Voice"),
    ("settings", "Settings"),
    ("diagnostics", "Diagnostics"),
]


def render_page(state, page="dashboard"):
    control = state.get("control") or {}
    speakers = control.get("speakers") or {}
    chimes = control.get("chimes") or {}
    settings = control.get("settings") or {}
    devices = state.get("devices") or {}
    available_chimes = chimes.get("available") or []
    selected_chimes = chimes.get("events") or {}
    recent_events = state.get("recent_events") or []
    health = "Healthy" if state.get("ok") else "Needs attention"
    page = _normalize_page(page)
    features = control.get("features") or {name: True for name in ("doorbell", "hvac", "vacuum", "fridge", "ice_maker", "matterbridge")}
    feature_pages = {"heat-pumps": "hvac", "refrigerator": "fridge", "vacuum": "vacuum", "vacuum-controls": "vacuum", "vacuum-messages": "vacuum", "doorbells": "doorbell"}
    pages = [(key, label) for key, label in PAGES if key not in feature_pages or features.get(feature_pages[key])]
    if page not in dict(pages) or (page == "dashboard" and control.get("setup_complete") is False):
        page = "setup"
    sections = {
        "setup": [_section("Doorbell Setup", [_setup_form(state)])],
        "dashboard": [
            _section("System", [
                f"<p><strong>Status:</strong> {_e(health)}</p>",
                f"<p><strong>Home Assistant:</strong> {_e((state.get('home_assistant') or {}).get('message', 'unknown'))}</p>",
                f"<p><strong>Dependencies:</strong> {_e((state.get('dependencies') or {}).get('message', 'unknown'))}</p>",
                '<div class="actions">'
                + _post_button("ui/control/armed", "state", "true", "Arm Viper")
                + _post_button("ui/control/armed", "state", "false", "Disarm Viper")
                + _post_button("ui/control/global_mute", "state", "true", "Mute All")
                + _post_button("ui/control/global_mute", "state", "false", "Unmute All")
                + "</div>",
            ]),
            _section("At A Glance", [_dashboard_summary(state)]),
            _section("Alexa / Matterbridge", [_bridge_status(state.get("matterbridge") or {})]),
            _section("Recent Events", [_recent_events(recent_events)]),
        ],
        "doorbells": [
            _section("Doorbell Status", [_doorbell_summary(settings)]),
            _section("Doorbell AI", [_doorbell_ai_form(settings)]),
            _section("Doorbell Tests", [_doorbell_test_buttons(settings), _live_doorbell_panel(settings)]),
        ],
        "live-doorbell": [
            _section("Live Console", [_live_doorbell_panel(settings)]),
        ],
        "ha-status": [
            _section("Home Assistant Status", [_ha_status_page(state)]),
            _section("Speaker Health", [_speaker_health_table((state.get("ha_status") or {}).get("speakers") or [])]),
            _section("Entity Health", [_entity_health_table((state.get("ha_status") or {}).get("entities") or {})]),
            _section("Recent Issues", [_recent_issues_table((state.get("ha_status") or {}).get("recent_issues") or [])]),
        ],
        "speakers": [
            _section("Speakers", [_speaker_summary(speakers), _speaker_table(speakers), _add_speaker_form()]),
        ],
        "chimes": [
            _section("Chime Assignments", [_chime_assignments(selected_chimes, available_chimes)]),
            _section("Manage Chime Files", [_manage_chime_files(available_chimes)]),
        ],
        "refrigerator": [
            _section("Connection Health", [f"<p>{_e((state.get('fridge_health') or {}).get('message', 'Not checked yet.'))}</p>"]),
            _section("Refrigerator Status", [_refrigerator_status(devices.get("refrigerator") or {})]),
            _section("Refrigerator Tests", [_fridge_test_buttons()]),
        ],
        "heat-pumps": [
            _section("Heat Pumps", [_heat_pump_status(devices.get("heat_pumps") or [], state.get("hvac_commands")), _airflow_status(devices.get("airflow") or []), _hvac_form()]),
        ],
        "vacuum": [
            _section("Vacuum", [_vacuum_status(devices.get("vacuum") or {}, devices.get("vacuum_status") or {}), _vacuum_form()]),
        ],
        "vacuum-controls": [
            _section("Advanced Vacuum Controls", [
                _vacuum_status(devices.get("vacuum") or {}, devices.get("vacuum_status") or {}),
                '<p id="vacuum_control_status" class="muted" role="status"></p>',
                _vacuum_fan_speed_form(devices.get("vacuum") or {}, devices.get("vacuum_controls") or []),
                _vacuum_dock_empty_mode_form(devices.get("vacuum_controls") or []),
                _vacuum_dock_actions_form(),
                _vacuum_room_clean_form(settings, control),
                _vacuum_control_table(devices.get("vacuum_controls") or []),
            ]),
        ],
        "vacuum-messages": [
            _section("Robot Message Studio", [_vacuum_message_studio(settings)]),
        ],
        "voice": [
            _section("Voice Behavior", [_voice_form(settings)]),
            _section("Voice Test", [_voice_test_form()]),
        ],
        "settings": [
            _section("Settings", [_settings_form(settings)]),
            _section("Broadcast", [_broadcast_form()]),
        ],
        "diagnostics": [
            _section("Alexa / Matterbridge", [_bridge_status(state.get("matterbridge") or {}, detailed=True)]),
            _section("Heat Pump Commands This Session", [_heat_pump_status(devices.get("heat_pumps") or [], state.get("hvac_commands"))]),
            _section("Recent Web Commands", [_hvac_history(state.get("hvac_history") or [])]),
            _section("Diagnostics", [_diagnostics(state)]),
            _section("Tests", [_utility_test_buttons()]),
        ],
    }
    if not features.get("matterbridge"):
        sections["dashboard"] = [section for section in sections["dashboard"] if "<h2>Alexa / Matterbridge</h2>" not in section]
        sections["diagnostics"] = [section for section in sections["diagnostics"] if "<h2>Alexa / Matterbridge</h2>" not in section]
    if not features.get("hvac"):
        sections["diagnostics"] = [_section("Diagnostics", [_diagnostics(state)]), _section("Doorbell Listener", [f"<p>{_e((state.get('doorbell_listener') or {}).get('message', 'Not connected yet.'))}</p>"])]
    if control.get("installation_profile") == "doorbells":
        sections["speakers"] = [_section("Speakers", [_speaker_summary(speakers, doorbells_only=True), _speaker_table(speakers, routes=("doorbell",)), _add_speaker_form()])]
        sections["settings"] = [_section("Connection Settings", [_connection_settings(settings)])]
        sections["chimes"] = [_section("Doorbell Chimes", [_chime_assignments(selected_chimes, available_chimes, doorbells_only=True)]), _section("Manage Chime Files", [_manage_chime_files(available_chimes)])]
    title = dict(PAGES).get(page, "Dashboard")
    return _page_shell("".join(sections.get(page) or sections["dashboard"]), page, title, pages)


def render_all_page_for_legacy_tests(state):
    control = state.get("control") or {}
    speakers = control.get("speakers") or {}
    chimes = control.get("chimes") or {}
    settings = control.get("settings") or {}
    devices = state.get("devices") or {}
    available_chimes = chimes.get("available") or []
    selected_chimes = chimes.get("events") or {}
    recent_events = state.get("recent_events") or []
    health = "Healthy" if state.get("ok") else "Needs attention"
    rows = [_section("System", [
        f"<p><strong>Status:</strong> {_e(health)}</p>",
        f"<p><strong>Home Assistant:</strong> {_e((state.get('home_assistant') or {}).get('message', 'unknown'))}</p>",
        f"<p><strong>Dependencies:</strong> {_e((state.get('dependencies') or {}).get('message', 'unknown'))}</p>",
        '<div class="actions">'
        + _post_button("ui/control/armed", "state", "true", "Arm Viper")
        + _post_button("ui/control/armed", "state", "false", "Disarm Viper")
        + _post_button("ui/control/global_mute", "state", "true", "Mute All")
        + _post_button("ui/control/global_mute", "state", "false", "Unmute All")
        + "</div>",
    ])]
    rows.append(_section("Home Assistant Status", [_ha_status_page(state)]))
    rows.append(_section("Speakers", [
        _speaker_table(speakers),
        _add_speaker_form(),
    ]))
    rows.append(_section("Chimes", [
        _chime_assignments(selected_chimes, available_chimes),
        _manage_chime_files(available_chimes),
    ]))
    rows.append(_section("Settings", [_settings_form(settings)]))
    rows.append(_section("Doorbell AI", [_doorbell_ai_form(settings), _doorbell_test_buttons()]))
    rows.append(_section("Heat Pumps", [_heat_pump_status(devices.get("heat_pumps") or []), _airflow_status(devices.get("airflow") or []), _hvac_form()]))
    rows.append(_section("Vacuum", [_vacuum_status(devices.get("vacuum") or {}, devices.get("vacuum_status") or {}), _vacuum_form()]))
    rows.append(_section("Advanced Vacuum Controls", ['<p id="vacuum_control_status" class="muted" role="status"></p>', _vacuum_fan_speed_form(devices.get("vacuum") or {}, devices.get("vacuum_controls") or []), _vacuum_dock_empty_mode_form(devices.get("vacuum_controls") or []), _vacuum_dock_actions_form(), _vacuum_room_clean_form(settings, control), _vacuum_control_table(devices.get("vacuum_controls") or [])]))
    rows.append(_section("Robot Message Studio", [_vacuum_message_studio(settings)]))
    rows.append(_section("Voice Behavior", [_voice_form(settings), _voice_test_form()]))
    rows.append(_section("Refrigerator", [_refrigerator_status(devices.get("refrigerator") or {})]))
    rows.append(_section("Diagnostics", [_diagnostics(state)]))
    rows.append(_section("Tests", [
        "<h3>Doorbells</h3>",
        _doorbell_test_buttons(),
        "<h3>Refrigerator</h3>",
        _fridge_test_buttons(),
        "<h3>Utilities</h3>",
        _utility_test_buttons(),
        _broadcast_form(),
    ]))
    rows.append(_section("Recent Events", [_recent_events(recent_events)]))
    return _page_shell("\n".join(rows), "dashboard", "All Controls")


def _page_shell(body, current_page, title, pages=None):
    body = body.replace("<table>", '<div class="table-scroll" role="region" aria-label="Scrollable data table" tabindex="0"><table>')
    body = body.replace("</table>", "</table></div>").replace("<th>", '<th scope="col">')
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_e(title)} - Viper Core</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #f6f7f8; color: #171717; }}
    header {{ background: #102033; color: white; padding: 18px 22px; }}
    nav {{ background: #ffffff; border-bottom: 1px solid #d8dde3; padding: 10px 18px; }}
    nav a {{ display: inline-block; margin: 4px 8px 4px 0; padding: 8px 10px; border: 1px solid #cbd3dc; border-radius: 4px; color: #102033; text-decoration: none; }}
    nav a[aria-current="page"] {{ background: #102033; color: white; border-color: #102033; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 18px; }}
    section {{ background: white; border: 1px solid #d8dde3; border-radius: 6px; margin: 0 0 16px; padding: 16px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #e5e8ec; padding: 8px; text-align: left; vertical-align: top; }}
    label {{ display: block; font-weight: 650; margin: 10px 0 4px; }}
    input, select, textarea {{ box-sizing: border-box; width: 100%; max-width: 560px; padding: 8px; }}
    button {{ margin: 4px 6px 4px 0; padding: 8px 12px; font-weight: 650; }}
    .actions form {{ display: inline; }}
    .compact form {{ display: inline; }}
    .summary {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .summary p {{ border: 1px solid #e5e8ec; border-radius: 6px; margin: 0; padding: 10px; }}
    .status-line {{ margin: 10px 0; font-weight: 650; }}
    .room-grid {{ display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); max-width: 760px; }}
    .room-option {{ border: 1px solid #d8dde3; border-radius: 6px; padding: 10px; margin: 0; font-weight: 650; }}
    .room-option input {{ width: auto; margin-right: 8px; }}
    .live-log {{ max-width: 760px; min-height: 180px; border: 1px solid #d8dde3; border-radius: 6px; padding: 10px; background: #fbfcfd; }}
    .live-log p {{ margin: 0 0 8px; }}
    .live-console {{ max-width: 760px; border: 1px solid #d8dde3; border-radius: 6px; padding: 12px; margin: 12px 0; background: #fbfcfd; }}
    .live-console p {{ margin: 0 0 8px; }}
    .inline-check {{ display: block; font-weight: 650; margin: 8px 0; }}
    .inline-check input {{ width: auto; margin-right: 8px; }}
    .muted {{ color: #5b6470; }}
    * {{ letter-spacing: 0; }}
    :focus-visible {{ outline: 3px solid #005fcc; outline-offset: 3px; }}
    .skip-link {{ position: absolute; left: 12px; top: -100px; padding: 12px; background: white; color: #102033; z-index: 10; }}
    .skip-link:focus {{ top: 12px; }}
    .table-scroll {{ max-width: 100%; overflow-x: auto; }}
    .table-scroll table {{ min-width: 640px; }}
    th, td {{ min-width: 5rem; overflow-wrap: normal; }}
    th, td, p, li, a {{ overflow-wrap: anywhere; }}
    input, select, textarea, button {{ font: inherit; border: 1px solid #697586; border-radius: 4px; min-height: 44px; }}
    button {{ background: #f0f2f4; color: #171717; max-width: 100%; white-space: normal; overflow-wrap: anywhere; }}
    input[type="checkbox"], input[type="radio"] {{ width: 24px; min-height: 24px; vertical-align: middle; }}
    [aria-disabled="true"] {{ opacity: .7; cursor: wait; }}
    summary {{ min-height: 44px; padding: 8px 0; box-sizing: border-box; cursor: pointer; }}
    .summary, .room-grid {{ grid-template-columns: repeat(auto-fit, minmax(min(210px, 100%), 1fr)); }}
    main, section, form {{ min-width: 0; }}
    section {{ border: 0; border-radius: 0; border-bottom: 1px solid #d8dde3; }}
    .status-line:empty {{ margin: 0; }}
    @media (max-width: 480px) {{ main {{ padding: 10px; }} section {{ padding: 10px 0; }} }}
    @media (forced-colors: active) {{ :focus-visible {{ outline-color: Highlight; }} }}
  </style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to main content</a>
<header><h1>Viper Core</h1><p>Home Assistant-hosted controls for Viper.</p></header>
{_nav(current_page, pages)}
<main id="main-content" tabindex="-1"><h2>{_e(title)}</h2><p id="async_status" class="status-line" role="status" aria-live="polite" aria-atomic="true"></p>{body}</main>
<script>
function viperBasePath() {{
  const marker = '/api/hassio_ingress/';
  const path = window.location.pathname || '/';
  const index = path.indexOf(marker);
  if (index >= 0) {{
    const rest = path.slice(index + marker.length);
    const token = rest.split('/')[0] || '';
    return path.slice(0, index) + marker + token + '/';
  }}
  return '/';
}}
function viperUrl(path) {{
  let target = String(path || '');
  try {{
    const parsed = new URL(target, window.location.href);
    target = parsed.pathname + parsed.search;
  }} catch (_error) {{
  }}
  const uiIndex = target.indexOf('/ui/');
  if (uiIndex >= 0) {{
    target = target.slice(uiIndex + 1);
  }}
  if (target.startsWith('/')) {{
    target = target.slice(1);
  }}
  return viperBasePath() + target;
}}
function updateVoiceEngineOptions() {{
  const engine = document.querySelector('[name="tts_engine"]');
  if (!engine) {{
    return;
  }}
  for (const section of document.querySelectorAll('[data-tts-engine]')) {{
    section.hidden = section.getAttribute('data-tts-engine') !== engine.value;
  }}
}}
function updateDoorbellPromptOptions() {{
  const style = document.querySelector('[name="ai_style_default"]');
  const custom = document.querySelector('[data-ai-custom-default]');
  if (!style || !custom) {{
    return;
  }}
  custom.hidden = style.value !== 'custom';
}}
function updateDoorbellModeOptions() {{
  const mode = document.querySelector('[name="doorbell_video_mode"]')?.value || 'fast';
  const provider = document.querySelector('[name="ai_provider"]')?.value || 'openai';
  const liveMode = mode === 'live';
  const smartMode = mode === 'smart';
  for (const section of document.querySelectorAll('[data-doorbell-when]')) {{
    const rule = section.getAttribute('data-doorbell-when') || '';
    section.hidden = !rule.split(/\\s+/).includes(mode);
  }}
  for (const section of document.querySelectorAll('[data-provider-when]')) {{
    const rule = section.getAttribute('data-provider-when') || '';
    section.hidden = liveMode || !rule.split(/\\s+/).includes(provider);
  }}
  for (const section of document.querySelectorAll('[data-hide-when-live="true"]')) {{
    section.hidden = liveMode;
  }}
  for (const section of document.querySelectorAll('[data-show-when-live="true"]')) {{
    section.hidden = !liveMode;
  }}
  for (const section of document.querySelectorAll('[data-show-when-smart="true"]')) {{
    section.hidden = !smartMode;
  }}
}}
function updateLiveTestOptions() {{
  const mode = document.getElementById('live_mode')?.value || 'gemini_true_live';
  for (const section of document.querySelectorAll('[data-live-when]')) {{
    const rule = section.getAttribute('data-live-when') || '';
    section.hidden = !rule.split(/\\s+/).includes(mode);
  }}
}}
async function postChimeTest(payload, status, button) {{
  if (button?.getAttribute('aria-disabled') === 'true') return;
  if (status) {{
    status.textContent = 'Running test...';
  }}
  if (button) {{
    button.setAttribute('aria-disabled', 'true');
  }}
  try {{
    const response = await fetch(viperUrl('ui/chimes/test'), {{
      method: 'POST',
      body: payload.toString(),
      headers: {{
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Viper-Async': 'true'
      }}
    }});
    const result = await response.json();
    if (status) {{
      status.textContent = result.message || (result.ok ? 'Test sent.' : 'Test failed.');
    }}
  }} catch (error) {{
    if (status) {{
      status.textContent = 'Test failed: ' + error;
    }}
  }} finally {{
    if (button) {{
      button.removeAttribute('aria-disabled');
    }}
  }}
}}
let viperLiveSource = null;
let viperAudioContext = null;
let viperAudioCursor = 0;
let viperLiveSessionId = '';
let viperLiveDoor = 'front';
let viperLivePollTimer = null;
function viperSpeakBrowser(message) {{
  if (!message || !('speechSynthesis' in window)) {{
    return;
  }}
  const utterance = new SpeechSynthesisUtterance(message);
  utterance.rate = 1.25;
  window.speechSynthesis.speak(utterance);
}}
function playLivePcm(base64Audio, sampleRate) {{
  if (!base64Audio) {{
    return;
  }}
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {{
    return;
  }}
  if (!viperAudioContext) {{
    viperAudioContext = new AudioContextClass();
  }}
  const binary = atob(base64Audio);
  const samples = new Int16Array(binary.length / 2);
  for (let i = 0; i < samples.length; i++) {{
    const low = binary.charCodeAt(i * 2);
    const high = binary.charCodeAt(i * 2 + 1);
    let value = (high << 8) | low;
    if (value >= 0x8000) {{
      value -= 0x10000;
    }}
    samples[i] = value;
  }}
  const rate = Number(sampleRate || 24000);
  const buffer = viperAudioContext.createBuffer(1, samples.length, rate);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) {{
    channel[i] = samples[i] / 32768;
  }}
  const source = viperAudioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(viperAudioContext.destination);
  const startAt = Math.max(viperAudioContext.currentTime, viperAudioCursor);
  source.start(startAt);
  viperAudioCursor = startAt + buffer.duration;
}}
function appendLiveLog(message) {{
  const log = document.getElementById('live_doorbell_log');
  if (!log) {{
    return;
  }}
  const row = document.createElement('p');
  row.textContent = message;
  log.prepend(row);
}}
function setText(id, value) {{
  const node = document.getElementById(id);
  if (node) {{
    node.textContent = value || '';
  }}
}}
function setLink(id, url, label) {{
  const node = document.getElementById(id);
  if (!node) {{
    return;
  }}
  if (url) {{
    node.hidden = false;
    node.href = url;
    node.textContent = label || url;
  }} else {{
    node.hidden = true;
    node.removeAttribute('href');
    node.textContent = '';
  }}
}}
async function refreshLiveSession() {{
  if (!viperLiveSessionId) {{
    return;
  }}
  try {{
    const response = await fetch(viperUrl('api/live/session/' + encodeURIComponent(viperLiveSessionId)), {{cache: 'no-store'}});
    const payload = await response.json();
    if (!payload.ok || !payload.session) {{
      return;
    }}
    const session = payload.session;
    setText('live_session_id', session.id || viperLiveSessionId);
    setText('live_session_state', session.status || '');
    setText('live_frames_sent', String(session.video_frames_sent || 0));
    setText('live_frames_discarded', String(session.video_frames_discarded || 0));
    setText('live_visual_prompts', String(session.visual_prompts_sent || 0));
    setText('live_raw_words', session.raw_transcript || 'No AI words yet.');
    setText('live_final_words', session.message || 'No final transcript yet.');
    setText('live_scene_change', session.scene_change || 'unknown');
    setText('live_scene_summary', session.scene_summary || 'No scene memory yet.');
    setText('live_scene_confidence', session.scene_confidence || 'unknown');
    setText('live_risky_claim', session.scene_risky_claim || 'none');
    setText('live_verification_status', session.verification_status || 'not needed');
    setText('live_verification_summary', session.verification_summary || '');
    setText('live_last_command', session.last_command || '');
    setText('live_speaker_state', session.last_speaker_result || '');
    setLink('live_exact_frames_link', session.debug_contact_sheet_url || '', 'Open exact frames Gemini saw');
    setLink('live_replay_link', session.audio_url || '', 'Open saved narration audio');
  }} catch (_error) {{
  }}
}}
function startLivePolling() {{
  if (viperLivePollTimer) {{
    window.clearInterval(viperLivePollTimer);
  }}
  refreshLiveSession();
  viperLivePollTimer = window.setInterval(refreshLiveSession, 1000);
}}
function stopLivePollingSoon() {{
  window.setTimeout(function () {{
    refreshLiveSession();
    if (viperLivePollTimer) {{
      window.clearInterval(viperLivePollTimer);
      viperLivePollTimer = null;
    }}
  }}, 5000);
}}
function repeatLiveTranscript() {{
  const text = document.getElementById('live_final_words')?.textContent || document.getElementById('live_raw_words')?.textContent || '';
  if (text && text !== 'No final transcript yet.' && text !== 'No AI words yet.') {{
    viperSpeakBrowser(text);
  }}
}}
async function describeLiveNow() {{
  if (!viperLiveSessionId) {{
    startLiveDoorbell(viperLiveDoor || 'front');
    return;
  }}
  const status = document.getElementById('live_doorbell_status');
  try {{
    const response = await fetch(viperUrl('api/live/session/' + encodeURIComponent(viperLiveSessionId) + '/describe'), {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: '{{}}'
    }});
    const payload = await response.json();
    if (status) {{
      status.textContent = payload.message || 'Describe-now request sent.';
    }}
    appendLiveLog(payload.message || 'Describe-now request sent.');
    if (!payload.ok) {{
      startLiveDoorbell(viperLiveDoor || 'front');
    }} else {{
      refreshLiveSession();
    }}
  }} catch (_error) {{
    startLiveDoorbell(viperLiveDoor || 'front');
  }}
}}
function stopLiveDoorbell() {{
  if (viperLiveSource) {{
    viperLiveSource.close();
    viperLiveSource = null;
  }}
  viperAudioCursor = 0;
  const status = document.getElementById('live_doorbell_status');
  if (status) {{
    status.textContent = 'Live description stopped.';
  }}
}}
async function stopLiveDoorbellBackend() {{
  if (viperLiveSessionId) {{
    try {{
      await fetch(viperUrl('api/live/session/' + encodeURIComponent(viperLiveSessionId) + '/stop'), {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: '{{}}'
      }});
    }} catch (_error) {{
    }}
  }}
  stopLiveDoorbell();
  stopLivePollingSoon();
}}
function startLiveDoorbell(door) {{
  stopLiveDoorbell();
  viperLiveDoor = door || 'front';
  if ('speechSynthesis' in window) {{
    window.speechSynthesis.cancel();
  }}
  const status = document.getElementById('live_doorbell_status');
  const browserSpeech = document.getElementById('live_browser_speech')?.checked;
  const speakerSpeech = document.getElementById('live_speaker_speech')?.checked;
  const allSpeakers = document.getElementById('live_all_speakers')?.checked;
  const forceConfirmation = document.getElementById('live_force_confirmation')?.checked;
  const liveMode = document.getElementById('live_mode')?.value || 'rolling';
  const seconds = document.getElementById('live_total_seconds')?.value || '30';
  const chunkSeconds = document.getElementById('live_chunk_seconds')?.value || '3';
  const pageParams = new URLSearchParams(window.location.search || '');
  const sessionId = pageParams.get('session_id') || '';
  const query = new URLSearchParams({{seconds: seconds, chunk_seconds: chunkSeconds, speak: speakerSpeech ? 'true' : 'false', all_speakers: allSpeakers ? 'true' : 'false', mode: liveMode, force_confirmation: forceConfirmation ? 'true' : 'false'}});
  if (sessionId) {{
    query.set('session_id', sessionId);
  }}
  const url = viperUrl('api/live/doorbell/' + door + '?' + query.toString());
  if (status) {{
    status.textContent = 'Starting live description for ' + door + ' door...';
  }}
  appendLiveLog('Starting live description for ' + door + ' door.');
  viperLiveSource = new EventSource(url);
  for (const type of ['session', 'status', 'update', 'transcript', 'raw_transcript', 'final_transcript', 'scene', 'audio', 'speaker_audio', 'warning', 'error', 'done']) {{
    viperLiveSource.addEventListener(type, function (event) {{
      let data = {{}};
      try {{
        data = JSON.parse(event.data || '{{}}');
      }} catch (_error) {{
        data = {{message: event.data || ''}};
      }}
      const message = data.message || '';
      if (type === 'session' && data.session_id) {{
        viperLiveSessionId = data.session_id;
        setText('live_session_id', viperLiveSessionId);
        startLivePolling();
      }}
      if (status) {{
        status.textContent = message || type;
      }}
      if (message) {{
        if (type === 'raw_transcript') {{
          appendLiveLog('AI sees: ' + message);
        }} else if (type === 'final_transcript') {{
          appendLiveLog('Final AI transcript: ' + message);
        }} else if (type === 'scene') {{
          appendLiveLog('Scene ' + (data.change || 'unknown') + ': ' + message);
        }} else {{
          appendLiveLog(message);
        }}
      }}
      if (type === 'audio' && browserSpeech) {{
        playLivePcm(data.audio || '', data.sample_rate || 24000);
      }}
      if ((type === 'update' || type === 'final_transcript') && browserSpeech) {{
        viperSpeakBrowser(message);
      }}
      if (type === 'done' || type === 'error') {{
        stopLiveDoorbell();
        stopLivePollingSoon();
      }}
    }});
  }}
  viperLiveSource.onerror = function () {{
    if (status) {{
      status.textContent = 'Live description connection closed.';
    }}
    stopLiveDoorbell();
    stopLivePollingSoon();
  }};
}}
document.addEventListener('submit', async function (event) {{
  const form = event.target;
  const submitter = event.submitter;
  const isAsync = form.enctype !== 'multipart/form-data';
  if (!isAsync) {{
    return;
  }}
  event.preventDefault();
  if (form.getAttribute('aria-busy') === 'true') return;
  form.setAttribute('aria-busy', 'true');
  const statusId = (submitter && submitter.getAttribute('data-status-target')) || form.getAttribute('data-status-target') || 'async_status';
  const status = document.getElementById(statusId);
  const button = submitter || form.querySelector('button');
  if (status) {{
    status.textContent = 'Working...';
  }}
  if (button) {{
    button.setAttribute('aria-disabled', 'true');
  }}
  const hvacUnit = form.querySelector('#hvac_entity');
  if (hvacUnit) {{
    for (const cell of document.querySelectorAll('[data-hvac-result]')) {{
      if (hvacUnit.value === 'all' || cell.getAttribute('data-hvac-result') === hvacUnit.value) cell.textContent = 'Sending';
    }}
  }}
  try {{
    const payload = new URLSearchParams();
    for (const element of form.elements) {{
      if (!element.name || element.disabled) {{
        continue;
      }}
      const type = (element.type || '').toLowerCase();
      if ((type === 'checkbox' || type === 'radio') && !element.checked) {{
        continue;
      }}
      payload.append(element.name, element.value || '');
    }}
    const action = (submitter && submitter.getAttribute('formaction')) || form.getAttribute('action') || form.action;
    if (String(action || '').includes('ui/voice/test')) {{
      const selectedEngine = document.querySelector('[name="tts_engine"]')?.value || 'home_assistant';
      const voiceNames = selectedEngine === 'gemini'
        ? ['tts_engine', 'gemini_tts_model', 'gemini_tts_voice', 'gemini_tts_speed', 'gemini_tts_style', 'gemini_tts_min_interval_seconds']
        : selectedEngine === 'openai'
          ? ['tts_engine', 'openai_tts_model', 'openai_tts_voice', 'openai_tts_speed', 'openai_tts_instructions']
          : ['tts_engine'];
      for (const name of voiceNames) {{
        const element = document.querySelector(`[name="${{name}}"]`);
        if (element && !payload.has(name)) {{
          payload.append(name, element.value || '');
        }}
      }}
      const keepWarm = document.querySelector('[name="gemini_tts_keep_warm"]');
      if (selectedEngine === 'gemini' && keepWarm && keepWarm.checked && !payload.has('gemini_tts_keep_warm')) {{
        payload.append('gemini_tts_keep_warm', keepWarm.value || 'true');
      }}
    }}
    const response = await fetch(viperUrl(action), {{
      method: (submitter && submitter.formMethod) || form.method || 'POST',
      body: payload.toString(),
      headers: {{
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Viper-Async': 'true'
      }}
    }});
    const result = await response.json();
    if (status) {{
      status.textContent = result.message || (response.ok && result.ok !== false ? 'Done.' : 'Command failed.');
    }}
    if (result.field) {{
      const field = form.elements.namedItem(result.field);
      if (field) {{ field.setAttribute('aria-invalid', 'true'); field.setAttribute('aria-describedby', statusId); }}
    }}
    if (result.commands) updateHvacResults(result.commands);
    if (result.ok !== false && String(action || '').includes('ui/setup/finish')) {{
      window.location.assign(viperBasePath() + '?page=dashboard');
      return;
    }}
    if (result.ok !== false && result.state) {{
      await refreshControlSection(form);
      const updatedStatus = document.getElementById(statusId);
      if (updatedStatus) updatedStatus.textContent = result.message || 'Saved.';
    }}
  }} catch (error) {{
    if (status) {{
      status.textContent = 'Command could not be confirmed. Check device status before retrying. ' + error;
    }}
  }} finally {{
    if (button) {{
      button.removeAttribute('aria-disabled');
    }}
    form.removeAttribute('aria-busy');
  }}
}});
async function refreshControlSection(form) {{
  const section = form.closest('section');
  if (!section) return;
  const sections = Array.from(document.querySelectorAll('main > section'));
  const index = sections.indexOf(section);
  const active = document.activeElement;
  const restore = section.contains(active);
  const activeId = active?.id;
  const activeText = active?.textContent;
  const formIndex = Array.from(section.querySelectorAll('form')).indexOf(form);
  const response = await fetch(window.location.href);
  if (!response.ok) return;
  const documentCopy = new DOMParser().parseFromString(await response.text(), 'text/html');
  const replacement = documentCopy.querySelectorAll('main > section')[index];
  if (!replacement) return;
  section.replaceWith(replacement);
  updateVoiceEngineOptions(); updateDoorbellModeOptions(); updateDoorbellPromptOptions();
  if (restore) {{
    const nextForm = replacement.querySelectorAll('form')[formIndex];
    const next = (activeId && document.getElementById(activeId)) ||
      Array.from(nextForm?.querySelectorAll('button') || []).find(item => item.textContent === activeText);
    if (next) next.focus();
    else {{ const status = document.getElementById('async_status'); status.tabIndex = -1; status.focus(); }}
  }}
}}
function updateHvacResults(commands) {{
  for (const cell of document.querySelectorAll('[data-hvac-result]')) {{
    const item = commands[cell.getAttribute('data-hvac-result')];
    if (!item) continue;
    const requested = [item.mode, item.temperature == null ? '' : item.temperature].filter(String).join(' ');
    cell.textContent = (item.message || item.status) + (requested ? '; requested ' + requested : '') +
      (item.observed_mode ? '; HA reports ' + item.observed_mode + ' ' + (item.observed_temperature ?? '') : '') +
      (item.timestamp ? '; ' + new Date(item.timestamp * 1000).toLocaleString() : '');
  }}
}}
document.addEventListener('change', function (event) {{
  if (event.target?.getAttribute('aria-invalid') === 'true') event.target.removeAttribute('aria-invalid');
  if (event.target && event.target.matches('[name="tts_engine"]')) {{
    updateVoiceEngineOptions();
  }}
  if (event.target && event.target.matches('[name="ai_style_default"]')) {{
    updateDoorbellPromptOptions();
  }}
  if (event.target && event.target.matches('[name="doorbell_video_mode"], [name="ai_provider"]')) {{
    updateDoorbellModeOptions();
  }}
  if (event.target && event.target.matches('#live_mode')) {{
    updateLiveTestOptions();
  }}
}});
document.addEventListener('DOMContentLoaded', function () {{
  updateVoiceEngineOptions();
  updateDoorbellPromptOptions();
  updateDoorbellModeOptions();
  updateLiveTestOptions();
  const params = new URLSearchParams(window.location.search || '');
  const liveDoor = params.get('live');
  if (liveDoor === 'front' || liveDoor === 'back') {{
    const liveMode = document.getElementById('live_mode');
    if (liveMode && params.get('mode')) {{
      liveMode.value = params.get('mode');
    }}
    const browserSpeech = document.getElementById('live_browser_speech');
    if (browserSpeech) {{
      browserSpeech.checked = params.get('speak') !== 'none';
    }}
    window.setTimeout(function () {{
      startLiveDoorbell(liveDoor);
    }}, 750);
  }}
}});
document.addEventListener('click', async function (event) {{
  const liveStart = event.target.closest('button[data-live-door]');
  if (liveStart) {{
    event.preventDefault();
    startLiveDoorbell(liveStart.getAttribute('data-live-door') || 'front');
    return;
  }}
  const liveStop = event.target.closest('button[data-live-stop="true"]');
  if (liveStop) {{
    event.preventDefault();
    stopLiveDoorbellBackend();
    return;
  }}
  const liveRepeat = event.target.closest('button[data-live-repeat="true"]');
  if (liveRepeat) {{
    event.preventDefault();
    repeatLiveTranscript();
    return;
  }}
  const liveDescribeNow = event.target.closest('button[data-live-describe-now="true"]');
  if (liveDescribeNow) {{
    event.preventDefault();
    describeLiveNow();
    return;
  }}
  const button = event.target.closest('button[data-test-selected-chime="true"]');
  if (!button) {{
    return;
  }}
  event.preventDefault();
  const form = button.closest('form');
  const status = document.getElementById(button.getAttribute('data-status-target') || 'chime_assignment_status');
  if (!form) {{
    if (status) {{
      status.textContent = 'Test failed: chime form not found.';
    }}
    return;
  }}
  const filename = form.querySelector('select[name="filename"]');
  const eventName = form.querySelector('input[name="event"]');
  const payload = new URLSearchParams();
  payload.append('filename', filename ? filename.value : '');
  payload.append('event', eventName ? eventName.value : '');
  await postChimeTest(payload, status, button);
}});
document.addEventListener('click', async function (event) {{
  const button = event.target.closest('button[data-test-file-chime="true"]');
  if (!button) {{
    return;
  }}
  event.preventDefault();
  const status = document.getElementById(button.getAttribute('data-status-target') || 'chime_test_status');
  const payload = new URLSearchParams();
  payload.append('filename', button.getAttribute('data-filename') || '');
  payload.append('event', button.getAttribute('data-event') || '');
  await postChimeTest(payload, status, button);
}});
</script>
</body>
</html>"""


def _nav(current_page, pages=None):
    links = []
    for page, label in (PAGES if pages is None else pages):
        current = ' aria-current="page"' if page == current_page else ""
        links.append(f'<a href="?page={_e(page)}"{current}>{_e(label)}</a>')
    return f"<nav aria-label=\"Viper Core pages\">{''.join(links)}</nav>"


def _setup_form(state):
    control = state.get("control") or {}
    settings = control.get("settings") or {}
    setup = state.get("setup") or {}
    entities = setup.get("entities") or []
    def select(name, label, domains, value=""):
        options = ['<option value="">Not selected</option>']
        for entity in entities:
            if entity.get("id", "").startswith(tuple(domains)):
                selected = " selected" if entity["id"] == value else ""
                options.append(f'<option value="{_e(entity["id"])}"{selected}>{_e(entity["name"])} ({_e(entity["id"])})</option>')
        return f'<label for="setup_{_e(name)}">{_e(label)}</label><select id="setup_{_e(name)}" name="{_e(name)}">{"".join(options)}</select>'
    speaker = (control.get("speakers") or {}).get("doorbell speaker") or {}
    rows = ["<p>Doorbell-only installation. Heat pumps, refrigerator, vacuum and Matterbridge are not required.</p>",
            '<p>Connect Ring and your speakers in Home Assistant first. For live image descriptions, configure a Ring-MQTT RTSP stream. Keep Ring account sign-in inside Ring or Ring-MQTT.</p>',
            '<p><a href="https://github.com/tsightler/ring-mqtt/wiki">Ring-MQTT setup guide</a></p>',
            _post_button("ui/setup/refresh", "", "", "Refresh Device List"),
            f'<p>{_e(setup.get("discovery_error", ""))}</p>',
            '<form action="/ui/setup/save" method="post"><h3>1. Doorbells</h3>']
    for door, label in (("front", "Main Doorbell"), ("back", "Second Doorbell")):
        if door == "back":
            checked = " checked" if settings.get("back_door_enabled") else ""
            rows.append(f'<label><input type="checkbox" name="back_door_enabled" value="true"{checked}> Enable second doorbell</label>')
        rows.append(select(f"{door}_door_trigger", f"{label} Press Event", ("event.", "binary_sensor."), settings.get(f"{door}_door_trigger")))
        rows.append(f'<label for="setup_{door}_stream">{label} RTSP URL</label><input id="setup_{door}_stream" name="{door}_door_stream_url" type="password" autocomplete="off" aria-describedby="stream_help">')
        rows.append(select(f"{door}_door_live_stream_switch", f"{label} Stream Switch (Optional)", ("switch.",), settings.get(f"{door}_door_live_stream_switch")))
    rows.append('<p id="stream_help">Leave a saved RTSP URL blank to keep it. Choose a press event, not a motion event.</p>')
    checked = " checked" if settings.get("doorbell_listener_enabled") else ""
    rows.append(f'<label><input type="checkbox" name="doorbell_listener_enabled" value="true"{checked}> Listen for doorbell presses automatically</label>')
    rows.append('<h3>2. Announcement Speaker</h3>')
    rows.append(select("speaker_entity", "Doorbell Speaker", ("media_player.",), speaker.get("id")))
    options = ''.join(f'<option value="{value}"{" selected" if speaker.get("type", "ha") == value else ""}>{label}</option>' for value, label in (("ha", "Home Assistant speaker (Sonos / Google Cast)"), ("alexa", "Alexa Media Player")))
    rows.append(f'<label for="setup_speaker_type">Speaker Connection</label><select id="setup_speaker_type" name="speaker_type">{options}</select>')
    rows.append('<p>Alexa requires the separate Alexa Media Player integration and its announce service. For Sonos or Google Cast, select a Home Assistant speech provider below.</p>')
    rows.append(select("tts_entity", "Home Assistant Speech Provider", ("tts.",), settings.get("tts_entity")))
    rows.append('<h3>3. Image Descriptions</h3>')
    options = ''.join(f'<option value="{value}"{" selected" if settings.get("ai_provider", "openai") == value else ""}>{label}</option>' for value, label in (("openai", "OpenAI"), ("gemini", "Gemini")))
    rows.append(f'<label for="setup_ai_provider">AI Provider</label><select id="setup_ai_provider" name="ai_provider">{options}</select>')
    for provider in ("openai", "gemini"):
        rows.append(f'<label for="setup_{provider}_key">{provider.title()} API Key</label><input id="setup_{provider}_key" name="{provider}_api_key" type="password" autocomplete="off"><p>{"Saved" if settings.get(provider + "_configured") else "Not configured"}. Leave blank to keep a saved key.</p>')
    rows.append('<p>Camera tests send images to the selected AI provider and may incur API charges. Voice and model choices are available on the Voice and Doorbells pages.</p><button type="submit">Save Doorbell Setup</button></form>')
    rows.append('<h3>4. Test And Finish</h3>')
    if not control.get("armed", True):
        rows.append(_post_button("ui/control/armed", "state", "true", "Arm Viper"))
    rows.append('<ul>' + ''.join(f'<li>{_e(message)}</li>' for message in setup.get("missing", [])) + '</ul>')
    for key, label, action in (("speaker", "Test Announcement Speaker", "speaker-test"), ("front", "Test Main Camera Description", "front-test"), ("back", "Test Second Camera Description", "back-test")):
        if key == "back" and not settings.get("back_door_enabled"):
            continue
        rows.append(_post_button(f"ui/setup/{action}", "", "", label))
        check = (setup.get("checks") or {}).get(key) or {}
        rows.append(f'<p>{_e(check.get("message") or "Not tested yet.")}</p>')
    listener = setup.get("listener") or {}
    rows.append(f'<p>Automatic alerts: {_e(listener.get("connection", "disabled"))}. Last real press: {_e(_utc_time(listener.get("last_event_at")))}.</p>')
    rows.append('<p>Press the actual doorbell, listen for its announcement, then refresh the device list to check the event time.</p><form action="/ui/setup/finish" method="post"><label><input type="checkbox" name="heard" value="true" required> I heard the speaker test and the real doorbell announcement</label><button type="submit">Finish Setup</button></form><p><a href="?page=dashboard">Open Dashboard</a></p>')
    return ''.join(rows)


def _connection_settings(settings):
    return f'''<form action="/ui/settings" method="post">
<label for="external_base_url">External URL For Phone Links (Optional)</label><input id="external_base_url" name="external_base_url" value="{_e(settings.get('external_base_url', ''))}">
<label for="speaker_base_url">Speaker Media URL (Optional)</label><input id="speaker_base_url" name="speaker_base_url" value="{_e(settings.get('speaker_base_url', ''))}">
<label for="doorbell_dedupe_seconds">Doorbell Duplicate Window, Seconds</label><input id="doorbell_dedupe_seconds" name="doorbell_dedupe_seconds" type="number" min="5" max="180" value="{_e(settings.get('doorbell_dedupe_seconds', 30))}">
<button type="submit">Save Settings</button></form>'''


def _section(title, parts):
    return f"<section><h2>{_e(title)}</h2>{''.join(parts)}</section>"


def _normalize_page(page):
    page = str(page or "dashboard").strip().lower()
    if page == "live-doorbell":
        return "doorbells"
    known = {item[0] for item in PAGES}
    return page if page in known else "dashboard"


def _dashboard_summary(state):
    control = state.get("control") or {}
    devices = state.get("devices") or {}
    settings = control.get("settings") or {}
    speakers = control.get("speakers") or {}
    enabled_speakers = sum(1 for speaker in speakers.values() if speaker.get("enabled", True))
    if control.get("installation_profile") == "doorbells":
        listener = state.get("doorbell_listener") or {}
        return (f"<p>Speakers: {_e(enabled_speakers)} enabled.</p><p>Automatic alerts: {_e(listener.get('connection', 'disabled'))}.</p>"
                f"<p>Last doorbell event: {_e(_utc_time(listener.get('last_event_at')))}.</p>"
                f"<p>Viper: {'armed' if control.get('armed', True) else 'disarmed'}; {'muted' if control.get('global_mute') else 'audio on'}.</p>")
    heat_pumps = devices.get("heat_pumps") or []
    heat_pump_issues = sum(1 for unit in heat_pumps if not unit.get("ok"))
    refrigerator = devices.get("refrigerator") or {}
    fridge_issues = sum(1 for item in refrigerator.values() if item and not item.get("ok"))
    return (
        '<div class="summary">'
        f"<p><strong>Viper:</strong> {'armed' if control.get('armed', True) else 'disarmed'}; {'muted' if control.get('global_mute') else 'audio on'}.</p>"
        f"<p><strong>Speakers:</strong> {_e(enabled_speakers)} enabled.</p>"
        f"<p><strong>Doorbells:</strong> {_e(settings.get('doorbell_video_mode', 'fast'))} video follow-up; Gemini {'ready' if settings.get('gemini_configured') else 'not configured'}.</p>"
        f"<p><strong>Heat pumps:</strong> {_e(len(heat_pumps))} units; {_e(heat_pump_issues)} need attention.</p>"
        f"<p><strong>Refrigerator:</strong> {_e(fridge_issues)} items need attention.</p>"
        f"<p><strong>Vacuum:</strong> {_e((devices.get('vacuum') or {}).get('state', 'unknown'))}.</p>"
        "</div>"
    )


def _ha_status_page(state):
    snapshot = state.get("ha_status") or {}
    system = snapshot.get("system") or {}
    ha_status = (snapshot.get("home_assistant") or {}).get("api") or state.get("home_assistant") or {}
    ha_config = (snapshot.get("home_assistant") or {}).get("config") or {}
    memory = system.get("memory") or {}
    cpu = system.get("cpu") or {}
    load = system.get("load_average") or {}
    process = system.get("process") or {}
    storage = snapshot.get("storage") or []
    rows = [
        '<div class="summary">',
        f"<p><strong>Viper Core:</strong> {_e(state.get('version', 'unknown'))}; uptime {_e(system.get('uptime', 'unknown'))}.</p>",
        f"<p><strong>Home Assistant API:</strong> {_e(ha_status.get('message', 'unknown'))}; {_e(ha_status.get('latency_ms', 0))} ms.</p>",
        f"<p><strong>Home Assistant:</strong> {_e(ha_config.get('version') or 'unknown')} in {_e(ha_config.get('time_zone') or 'unknown')}.</p>",
        f"<p><strong>CPU:</strong> {_e(cpu.get('cores', 0))} cores; {_e(cpu.get('speed_mhz') or 'unknown')} MHz; load {_e(load.get('one', 0))}, {_e(load.get('five', 0))}, {_e(load.get('fifteen', 0))}.</p>",
        f"<p><strong>Memory:</strong> {_e(_format_bytes(memory.get('used')))} used of {_e(_format_bytes(memory.get('total')))} ({_e(memory.get('used_percent', 0))}%).</p>",
        f"<p><strong>Viper Process:</strong> PID {_e(process.get('pid', 'unknown'))}; {_e(_format_bytes(process.get('memory')))} memory.</p>",
        "</div>",
    ]
    if cpu.get("model"):
        rows.append(f"<p><strong>Processor:</strong> {_e(cpu.get('model'))}</p>")
    if ha_config.get("error"):
        rows.append(f"<p><strong>HA config read:</strong> {_e(ha_config.get('error'))}</p>")
    rows.append(_storage_table(storage))
    return "".join(rows)


def _storage_table(storage):
    if not storage:
        return "<p>Storage status has not refreshed yet.</p>"
    rows = ["<table><thead><tr><th>Area</th><th>Used</th><th>Free</th><th>Total</th><th>Path</th></tr></thead><tbody>"]
    for item in storage:
        if not item.get("ok"):
            rows.append(f"<tr><td>{_e(item.get('label'))}</td><td colspan=\"4\">{_e(item.get('message', 'not available'))}</td></tr>")
            continue
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('label'))}</td>"
            f"<td>{_e(_format_bytes(item.get('used')))} ({_e(item.get('used_percent', 0))}%)</td>"
            f"<td>{_e(_format_bytes(item.get('free')))}</td>"
            f"<td>{_e(_format_bytes(item.get('total')))}</td>"
            f"<td>{_e(item.get('path'))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _speaker_health_table(speakers):
    if not speakers:
        return "<p>Speaker health has not refreshed yet.</p>"
    rows = ["<table><thead><tr><th>Speaker</th><th>Status</th><th>Type</th><th>Routes</th><th>Details</th></tr></thead><tbody>"]
    for item in speakers:
        status = "OK" if item.get("ok") else "Needs attention"
        if not item.get("enabled"):
            status = "Disabled"
        details = item.get("message") or item.get("friendly_name") or item.get("id") or ""
        if item.get("volume") is not None:
            details = f"{details}; volume {item.get('volume')}"
        if item.get("muted") is not None:
            details = f"{details}; muted {item.get('muted')}"
        if item.get("latency_ms") is not None:
            details = f"{details}; {item.get('latency_ms')} ms"
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('name'))}</td>"
            f"<td>{_e(status)}: {_e(item.get('state', 'unknown'))}</td>"
            f"<td>{_e(item.get('type'))}</td>"
            f"<td>{_e(item.get('routes'))}</td>"
            f"<td>{_e(details.strip('; '))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _entity_health_table(status):
    entities = status.get("dependency_entities") or {}
    rows = [
        '<div class="summary">',
        f"<p><strong>Dependencies:</strong> {_e(status.get('dependencies_message', 'unknown'))}</p>",
        f"<p><strong>Device refresh:</strong> {_e(status.get('device_message', 'unknown'))}</p>",
        "</div>",
    ]
    if not entities:
        rows.append("<p>No dependency entities have been checked yet.</p>")
        return "".join(rows)
    rows.append("<table><thead><tr><th>Entity</th><th>Status</th><th>Friendly Name</th></tr></thead><tbody>")
    for entity_id, item in sorted(entities.items()):
        rows.append(
            "<tr>"
            f"<td>{_e(entity_id)}</td>"
            f"<td>{_e('OK' if item.get('ok') else 'Needs attention')}: {_e(item.get('state', 'unknown'))}</td>"
            f"<td>{_e(item.get('friendly_name') or item.get('message') or '')}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _recent_issues_table(events):
    if not events:
        return "<p>No recent Viper issues recorded.</p>"
    rows = ["<table><thead><tr><th>Event</th><th>Message</th><th>Payload</th></tr></thead><tbody>"]
    for item in events:
        rows.append(
            "<tr>"
            f"<td>{_e(item.get('event_type', 'event'))}</td>"
            f"<td>{_e(item.get('message', ''))}</td>"
            f"<td>{_e(item.get('payload', ''))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _doorbell_summary(settings):
    mode = settings.get("doorbell_video_mode", "fast")
    front_stream = "configured" if settings.get("front_door_stream_url") else "not configured"
    back_stream = "configured" if settings.get("back_door_stream_url") else "not configured"
    return (
        '<div class="summary">'
        f"<p><strong>Doorbell Mode:</strong> {_e(mode)}</p>"
        f"<p><strong>RTSP Streams:</strong> front {_e(front_stream)}, back {_e(back_stream)}.</p>"
        "</div>"
    )


def _speaker_summary(speakers, doorbells_only=False):
    enabled = sum(1 for speaker in speakers.values() if speaker.get("enabled", True))
    doorbell = sum(1 for speaker in speakers.values() if speaker.get("enabled", True) and speaker.get("doorbell", True))
    fridge = sum(1 for speaker in speakers.values() if speaker.get("enabled", True) and speaker.get("fridge", True))
    utilities = sum(1 for speaker in speakers.values() if speaker.get("enabled", True) and speaker.get("utilities", True))
    if doorbells_only:
        return f'<p>Enabled: {_e(enabled)} speakers. Doorbell route: {_e(doorbell)} speakers.</p>'
    return (
        '<div class="summary">'
        f"<p><strong>Enabled:</strong> {_e(enabled)} speakers.</p>"
        f"<p><strong>Doorbell Route:</strong> {_e(doorbell)} speakers.</p>"
        f"<p><strong>Refrigerator Route:</strong> {_e(fridge)} speakers.</p>"
        f"<p><strong>Utility Route:</strong> {_e(utilities)} speakers.</p>"
        "</div>"
    )


def _speaker_table(speakers, routes=("doorbell", "fridge", "utilities")):
    route_names = routes
    rows = [
        "<table><thead><tr><th>Speaker</th><th>Target</th><th>Routes</th><th>Actions</th></tr></thead><tbody>"
    ]
    for name, speaker in sorted(speakers.items()):
        enabled = bool(speaker.get("enabled", True))
        routes = []
        for route in route_names:
            route_enabled = bool(speaker.get(route, True))
            routes.append(
                _post_button(
                    f"ui/speakers/{quote(name)}/route",
                    "route_state",
                    f"{route}:{str(not route_enabled).lower()}",
                    f"Turn {route.title()} {'Off' if route_enabled else 'On'} for {_display_name(name)}",
                )
            )
        actions = (
            _post_button(f"ui/speakers/{quote(name)}/enabled", "state", str(not enabled).lower(), f"{'Disable' if enabled else 'Enable'} {_display_name(name)}")
            + _post_button(f"ui/speakers/{quote(name)}/delete", "", "", f"Delete {_display_name(name)}")
        )
        rows.append(
            "<tr>"
            f"<td>{_e(_display_name(name))}<br><span class='muted'>{'enabled' if enabled else 'disabled'}</span></td>"
            f"<td>{_e(speaker.get('type', ''))}: {_e(speaker.get('id', ''))}</td>"
            f"<td class='compact'>{''.join(routes)}</td>"
            f"<td class='compact'>{actions}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _add_speaker_form():
    return """<form action="/ui/speakers" method="post">
<h3>Add Or Update Speaker</h3>
<label for="speaker_name">Name</label><input id="speaker_name" name="name">
<label for="speaker_id">Entity ID Or IP Address</label><input id="speaker_id" name="id">
<label for="speaker_type">Type</label><select id="speaker_type" name="type"><option value="ha">Home Assistant Speaker</option><option value="alexa">Alexa Media Player</option><option value="sonos">Direct Sonos IP</option></select>
<button type="submit">Save Speaker</button>
</form>"""


def _doorbell_ai_form(settings):
    mode = settings.get("doorbell_video_mode", "fast")
    provider = settings.get("ai_provider", "openai")
    provider_options = "".join(
        f'<option value="{_e(value)}"{ " selected" if value == provider else ""}>{_e(label)}</option>'
        for value, label in [("gemini", "Gemini"), ("openai", "OpenAI")]
    )
    mode_options = []
    for value, label in [
        ("fast", "Still Image"),
        ("live", "Live Narration"),
        ("smart", "Smart Still, Then Live If Unclear"),
    ]:
        selected = " selected" if value == mode else ""
        mode_options.append(f'<option value="{_e(value)}"{selected}>{_e(label)}</option>')
    openai_status = "configured" if settings.get("openai_configured") else "not configured"
    gemini_status = "configured" if settings.get("gemini_configured") else "not configured"
    prompt_editor = _default_ai_prompt_editor(settings)
    return f"""<form action="/ui/settings" method="post">
<label for="doorbell_video_mode">Normal Doorbell Mode</label>
<select id="doorbell_video_mode" name="doorbell_video_mode">{''.join(mode_options)}</select>
<div data-show-when-live="true">
<p><strong>Live Narration:</strong> Uses Gemini Live for both video understanding and live speech.</p>
<p><strong>Gemini key:</strong> {_e(gemini_status)}</p>
</div>
<div data-hide-when-live="true">
<label for="ai_provider">Still Image AI System</label>
<select id="ai_provider" name="ai_provider">{provider_options}</select>
{prompt_editor}
<div data-provider-when="gemini">
<p><strong>Gemini key:</strong> {_e(gemini_status)}</p>
</div>
<div data-provider-when="openai">
<p><strong>OpenAI key:</strong> {_e(openai_status)}</p>
</div>
</div>
<div data-doorbell-when="live smart">
<label for="doorbell_live_video_seconds">Live Narration Length, Seconds</label>
<input id="doorbell_live_video_seconds" name="doorbell_live_video_seconds" type="number" min="25" max="120" value="{_e(settings.get('doorbell_live_video_seconds', 30))}">
</div>
<div data-show-when-live="true">
<label for="live_speaker_chunk_seconds">Speaker Update Timing, Seconds</label>
<input id="live_speaker_chunk_seconds" name="live_speaker_chunk_seconds" type="number" min="1" max="5" value="{_e(settings.get('live_speaker_chunk_seconds', 2))}">
</div>
<div data-show-when-smart="true">
<label for="doorbell_live_video_frames">Smart Follow-Up Frames To Analyze</label>
<input id="doorbell_live_video_frames" name="doorbell_live_video_frames" type="number" min="2" max="6" value="{_e(settings.get('doorbell_live_video_frames', 4))}">
</div>
<details>
<summary>Advanced AI Settings</summary>
<div data-hide-when-live="true">
<label for="gemini_vision_model">Gemini Vision Model</label>
<input id="gemini_vision_model" name="gemini_vision_model" value="{_e(settings.get('gemini_vision_model', 'gemini-3.5-flash'))}">
<div data-provider-when="openai">
<label for="openai_api_key">New OpenAI API Key</label>
<input id="openai_api_key" name="openai_api_key" type="password" autocomplete="off">
<label><input name="clear_openai_api_key" type="checkbox" value="true"> Clear saved OpenAI key</label>
<label for="openai_vision_model">OpenAI Vision Model</label>
<input id="openai_vision_model" name="openai_vision_model" value="{_e(settings.get('openai_vision_model', 'gpt-6-astra'))}">
</div>
</div>
<div data-show-when-live="true">
<label for="gemini_vision_model_live">Gemini Live Model</label>
<input id="gemini_vision_model_live" name="gemini_live_model" value="{_e(settings.get('gemini_live_model', 'gemini-3.1-flash-live-preview'))}">
</div>
</details>
<button type="submit">Save Doorbell AI Settings</button>
</form>"""


def _default_ai_prompt_editor(settings):
    styles = settings.get("ai_description_styles") or {}
    custom = settings.get("ai_custom_descriptions") or {}
    selected = styles.get("front_photo") or "balanced"
    if len({styles.get(job) for job in ("front_photo", "back_photo", "manual_video", "smart_video", "detailed_video")}) > 1:
        selected = "balanced"
    custom_value = next((str(custom.get(job) or "").strip() for job in ("front_photo", "back_photo", "manual_video", "smart_video", "detailed_video") if str(custom.get(job) or "").strip()), "")
    options = []
    for value, text in _ai_style_options():
        selected_attr = " selected" if value == selected else ""
        options.append(f'<option value="{_e(value)}"{selected_attr}>{_e(text)}</option>')
    custom_hidden = "" if selected == "custom" else " hidden"
    return (
        '<h3>Prompt Editor</h3>'
        '<label for="ai_style_default">Prompt Style</label>'
        f'<select id="ai_style_default" name="ai_style_default">{"".join(options)}</select>'
        f'<div data-ai-custom-default{custom_hidden}>'
        '<label for="ai_custom_default">Custom Prompt</label>'
        f'<textarea id="ai_custom_default" name="ai_custom_default" rows="5">{_e(custom_value)}</textarea>'
        '</div>'
    )


def _ai_prompt_job(settings, job, label):
    styles = settings.get("ai_description_styles") or {}
    custom = settings.get("ai_custom_descriptions") or {}
    selected = styles.get(job) or {
        "front_photo": "balanced",
        "back_photo": "balanced",
        "manual_video": "detailed_blind",
        "smart_video": "fast_security",
        "detailed_video": "detailed_blind",
    }.get(job, "balanced")
    options = []
    for value, text in _ai_style_options():
        selected_attr = " selected" if value == selected else ""
        options.append(f'<option value="{_e(value)}"{selected_attr}>{_e(text)}</option>')
    custom_value = custom.get(job, "")
    return (
        f'<h3>{_e(label)}</h3>'
        f'<label for="ai_style_{_e(job)}">{_e(label)} Style</label>'
        f'<select id="ai_style_{_e(job)}" name="ai_style_{_e(job)}">{"".join(options)}</select>'
        f'<label for="ai_custom_{_e(job)}">{_e(label)} Custom Instructions</label>'
        f'<textarea id="ai_custom_{_e(job)}" name="ai_custom_{_e(job)}" rows="4">{_e(custom_value)}</textarea>'
    )


def _ai_style_options():
    return [
        ("balanced", "Balanced"),
        ("fast_security", "Fast security summary"),
        ("people_movement", "People and movement"),
        ("packages_deliveries", "Packages and deliveries"),
        ("detailed_blind", "Detailed for blind user"),
        ("custom", "Custom"),
    ]


def _doorbell_test_buttons(settings=None):
    back_button = _post_button("ui/test/doorbell/back", "", "", "Test Back Normal Flow") if (settings or {}).get("back_door_enabled", True) else ""
    return (
        '<div class="actions">'
        + _post_button("ui/test/doorbell/front", "", "", "Test Front Normal Flow")
        + back_button
        + "</div>"
    )


def _live_doorbell_panel(settings):
    back_button = '<button type="button" data-live-door="back">Test Back Live</button>' if settings.get("back_door_enabled", True) else ""
    return f"""<div>
<h3>Manual Live Test</h3>
<p id="live_doorbell_status" class="status-line" role="status" aria-live="polite">Ready.</p>
<p class="muted">Manual tests always ask the AI to report the current scene. Normal doorbell events stay quieter and speak only useful changes.</p>
<label for="live_mode">Manual Live Test Type</label>
<select id="live_mode">
<option value="gemini_true_live">Gemini True Live Audio</option>
<option value="rolling">Rolling AI Updates</option>
</select>
<label for="live_total_seconds">Manual Live Test Length, Seconds</label>
<input id="live_total_seconds" type="number" min="25" max="120" value="{_e(settings.get('doorbell_live_video_seconds', 30))}">
<div data-live-when="rolling">
<label for="live_chunk_seconds">Update Every, Seconds</label>
<input id="live_chunk_seconds" type="number" min="2" max="8" value="{_e(settings.get('live_doorbell_chunk_seconds', 3))}">
</div>
<label class="inline-check" for="live_force_confirmation"><input id="live_force_confirmation" type="checkbox" checked> Describe the current scene immediately during tests</label>
<label class="inline-check" for="live_browser_speech"><input id="live_browser_speech" type="checkbox" checked> Speak updates in this browser</label>
<label class="inline-check" for="live_speaker_speech"><input id="live_speaker_speech" type="checkbox"> Also speak updates on speakers</label>
<label class="inline-check" for="live_all_speakers"><input id="live_all_speakers" type="checkbox"> Use every enabled speaker instead of only doorbell speakers</label>
<div class="actions">
<button type="button" data-live-door="front">Test Front Live</button>
{back_button}
<button type="button" data-live-repeat="true">Repeat Last AI Transcript</button>
<button type="button" data-live-describe-now="true">Describe Now</button>
<button type="button" data-live-stop="true">Stop Live Narration</button>
</div>
<div class="live-console" aria-label="Live session details">
<p><strong>Session:</strong> <span id="live_session_id">Not started.</span></p>
<p><strong>State:</strong> <span id="live_session_state">Idle.</span></p>
<p><strong>Frames sent:</strong> <span id="live_frames_sent">0</span></p>
<p><strong>Frames discarded:</strong> <span id="live_frames_discarded">0</span></p>
<p><strong>Visual checks:</strong> <span id="live_visual_prompts">0</span></p>
<p><strong>AI words:</strong> <span id="live_raw_words">No AI words yet.</span></p>
<p><strong>Final transcript:</strong> <span id="live_final_words">No final transcript yet.</span></p>
<p><strong>Scene change:</strong> <span id="live_scene_change">unknown</span></p>
<p><strong>Scene memory:</strong> <span id="live_scene_summary">No scene memory yet.</span></p>
<p><strong>Confidence:</strong> <span id="live_scene_confidence">unknown</span></p>
<p><strong>Risky claim:</strong> <span id="live_risky_claim">none</span></p>
<p><strong>Verification:</strong> <span id="live_verification_status">not needed</span></p>
<p><strong>Verification summary:</strong> <span id="live_verification_summary"></span></p>
<p><strong>Last command:</strong> <span id="live_last_command"></span></p>
<p><strong>Speaker status:</strong> <span id="live_speaker_state"></span></p>
<p><a id="live_exact_frames_link" hidden>Open exact frames Gemini saw</a></p>
<p><a id="live_replay_link" hidden>Open saved narration audio</a></p>
</div>
<div id="live_doorbell_log" class="live-log" role="log" aria-live="polite" aria-label="Live doorbell description updates"></div>
</div>"""


def _fridge_test_buttons():
    return (
        '<div class="actions">'
        + _post_button("ui/test/fridge/fridge", "", "", "Test Fridge Open")
        + _post_button("ui/test/fridge/fridge_closed", "", "", "Test Fridge Closed")
        + _post_button("ui/test/fridge/freezer", "", "", "Test Freezer Open")
        + _post_button("ui/test/fridge/freezer_closed", "", "", "Test Freezer Closed")
        + _post_button("ui/control/ice_maker", "state", "true", "Turn Ice Maker On")
        + _post_button("ui/control/ice_maker", "state", "false", "Turn Ice Maker Off")
        + "</div>"
    )


def _utility_test_buttons():
    return (
        '<div class="actions">'
        + _post_button("ui/test/pushover", "", "", "Send Pushover Test")
        + "</div>"
    )


def _chime_upload_form():
    return """<form action="/ui/chimes/upload" method="post" enctype="multipart/form-data">
<label for="chime_file">Upload Chime File</label>
<input id="chime_file" name="file" type="file" accept=".mp3,.wav,.ogg,.m4a">
<button type="submit">Upload Chime</button>
</form>
<form action="/ui/chimes/upload-folder" method="post" enctype="multipart/form-data">
<label for="chime_folder">Upload Chime Folder</label>
<input id="chime_folder" name="files" type="file" accept=".mp3,.wav,.ogg,.m4a" multiple webkitdirectory directory>
<button type="submit">Upload Folder</button>
</form>"""


def _chime_assignments(selected, available, doorbells_only=False):
    options = ['<option value="">No chime</option>'] + [
        f'<option value="{_e(item)}">{{selected}}</option>'.replace("{selected}", _e(item)) for item in available
    ]
    forms = ['<p id="chime_assignment_status" class="status-line" role="status" aria-live="polite"></p>']
    for event, label in CHIME_EVENTS:
        if doorbells_only and not event.endswith("doorbell"):
            continue
        current = selected.get(event, "")
        event_options = []
        for item in ["", *available]:
            selected_attr = " selected" if item == current else ""
            text = "No chime" if not item else item
            event_options.append(f'<option value="{_e(item)}"{selected_attr}>{_e(text)}</option>')
        forms.append(
            f'<form action="/ui/chimes/assign" method="post" data-async="true"><input type="hidden" name="event" value="{_e(event)}">'
            f'<label for="chime_{_e(event)}">{_e(label)}</label><select id="chime_{_e(event)}" name="filename">{"".join(event_options)}</select>'
            '<div class="actions">'
            '<button type="submit">Save Chime</button>'
            '<button type="button" data-test-selected-chime="true" data-status-target="chime_assignment_status">Test Selected Chime</button>'
            '</div></form>'
        )
    return "".join(forms)


def _manage_chime_files(files):
    return (
        "<details><summary>Manage Uploaded Chime Files</summary>"
        + _chime_upload_form()
        + _chime_files(files)
        + "</details>"
    )


def _chime_files(files):
    if not files:
        return "<p>No uploaded chimes yet.</p>"
    rows = ['<p id="chime_test_status" class="status-line" role="status" aria-live="polite"></p><ul>']
    for filename in files:
        rows.append(
            f"<li>{_e(filename)} "
            + _post_button("ui/chimes/delete", "filename", filename, "Delete")
            + f'<button type="button" data-test-file-chime="true" data-status-target="chime_test_status" data-filename="{_e(filename)}">Test</button>'
            + "</li>"
        )
    rows.append("</ul>")
    return "".join(rows)


def _broadcast_form():
    return """<form action="/ui/broadcast" method="post">
<label for="broadcast_message">Broadcast Message</label>
<textarea id="broadcast_message" name="message" rows="3"></textarea>
<button type="submit">Speak Broadcast</button>
</form>"""


def _hvac_form():
    units = [
        ("all", "All Heat Pumps"),
        ("climate.office_heat_pump_alexa", "Office"),
        ("climate.living_room_heat_pump_alexa", "Living Room"),
        ("climate.kitchen_heat_pump_alexa", "Kitchen"),
        ("climate.jamie_s_room_heat_pump_alexa", "Jamie's Room"),
        ("climate.master_bedroom_heat_pump_alexa", "Master Bedroom"),
    ]
    unit_options = "".join(f'<option value="{_e(entity)}">{_e(label)}</option>' for entity, label in units)
    return f"""<p>Confirmation checks Home Assistant's reported state. Infrared delivery to the physical unit cannot be verified.</p>
<p id="hvac_status" class="status-line" role="status" aria-live="polite" aria-atomic="true"></p>
<form action="/ui/hvac" method="post" data-status-target="hvac_status">
<label for="hvac_entity">Unit</label><select id="hvac_entity" name="entity_id">{unit_options}</select>
<label for="hvac_mode">Mode</label><select id="hvac_mode" name="mode"><option value="cool">Cool</option><option value="heat">Heat</option><option value="off">Off</option><option value="">Temperature Only</option></select>
<label for="hvac_temperature">Target Temperature</label><input id="hvac_temperature" name="temperature" type="number" min="50" max="90" step="1" value="70">
<button type="submit">Send Heat Pump Command</button>
</form>"""


def _heat_pump_status(units, commands=None):
    if not units:
        return "<p>Heat pump status has not refreshed yet.</p>"
    rows = ["<table><thead><tr><th>Unit</th><th>Status</th><th>Target</th><th>Fan</th><th>Swing</th><th>Last Web Command</th></tr></thead><tbody>"]
    for unit in units:
        attrs = unit.get("attributes") or {}
        state = unit.get("state") or "unknown"
        target = attrs.get("temperature") or attrs.get("target_temp_high") or ""
        fan = attrs.get("fan_mode") or ""
        swing = attrs.get("swing_mode") or ""
        status = state if unit.get("ok") else f"{state} - needs attention"
        command = (commands or {}).get(unit.get("entity_id"), {})
        command_text = command.get("message", "No command this session")
        if command:
            requested = " ".join(str(value) for value in (command.get("mode"), command.get("temperature")) if value not in (None, ""))
            command_text += f"; requested {requested}; {_utc_time(command.get('timestamp'))}"
            if command.get("observed_mode"):
                command_text += f"; HA reports {command['observed_mode']} {command.get('observed_temperature', '')}"
        rows.append(
            "<tr>"
            f"<td>{_e(unit.get('name') or unit.get('entity_id'))}</td>"
            f"<td>{_e(status)}</td>"
            f"<td>{_e(target)}</td>"
            f"<td>{_e(fan)}</td>"
            f"<td>{_e(swing)}</td>"
            f'<td data-hvac-result="{_e(unit.get("entity_id"))}">{_e(command_text)}</td>'
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _utc_time(value):
    if not value:
        return "Not checked yet"
    return datetime.fromtimestamp(float(value), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _hvac_history(history):
    if not history:
        return "<p>No web heat-pump commands this session.</p>"
    rows = ["<table><thead><tr><th>Time</th><th>Unit</th><th>Requested</th><th>Result</th></tr></thead><tbody>"]
    for item in reversed(history):
        request = " ".join(str(value) for value in (item.get("mode"), item.get("temperature")) if value not in (None, ""))
        rows.append(f"<tr><td>{_e(_utc_time(item.get('timestamp')))}</td><td>{_e(item.get('entity_id'))}</td><td>{_e(request)}</td><td>{_e(item.get('message'))}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _bridge_status(status, detailed=False):
    connection = str(status.get("connection") or "unknown").capitalize()
    body = f"<p><strong>HA connection:</strong> {_e(connection)} (log evidence)</p>"
    body += f"<p>{_e(status.get('message') or 'Not checked yet.')}</p>"
    body += f"<p><strong>Checked:</strong> {_e(_utc_time(status.get('checked_at')))}</p>"
    if detailed:
        body += f"<p><strong>Last connection evidence:</strong> {_e(status.get('connection_evidence') or 'Not available')}</p>"
        body += f"<p><strong>Last received thermostat command:</strong> {_e(status.get('last_command') or 'Not present in current logs')}</p>"
        body += "<p><strong>Last successful Alexa command:</strong> Not available. Matterbridge does not expose verified delivery history.</p>"
    return body


def _airflow_status(units):
    if not units:
        return "<p>Airflow status has not refreshed yet.</p>"
    rows = ["<h3>Airflow</h3><table><thead><tr><th>Control</th><th>Status</th><th>Percent</th><th>Preset</th></tr></thead><tbody>"]
    for unit in units:
        attrs = unit.get("attributes") or {}
        status = unit.get("state") or "unknown"
        if not unit.get("ok"):
            status = f"{status} - needs attention"
        rows.append(
            "<tr>"
            f"<td>{_e(unit.get('name') or unit.get('entity_id'))}</td>"
            f"<td>{_e(status)}</td>"
            f"<td>{_e(attrs.get('percentage', ''))}</td>"
            f"<td>{_e(attrs.get('preset_mode', ''))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _vacuum_form():
    return """<div class="actions">
<form action="/ui/vacuum" method="post"><input type="hidden" name="action" value="start"><button type="submit">Start Vacuum</button></form>
<form action="/ui/vacuum" method="post"><input type="hidden" name="action" value="pause"><button type="submit">Pause Vacuum</button></form>
<form action="/ui/vacuum" method="post"><input type="hidden" name="action" value="dock"><button type="submit">Send Vacuum Home</button></form>
<form action="/ui/vacuum" method="post"><input type="hidden" name="action" value="stop"><button type="submit">Stop Vacuum</button></form>
</div>"""


def _vacuum_fan_speed_form(vacuum, controls=None):
    controls = controls or []
    control_vacuum = next((item for item in controls if item.get("domain") == "vacuum" and (item.get("attributes") or {}).get("fan_speed_list")), None)
    if control_vacuum:
        vacuum = control_vacuum
    attrs = vacuum.get("attributes") or {}
    entity_id = vacuum.get("entity_id") or "vacuum.cinderella"
    speeds = attrs.get("fan_speed_list") if isinstance(attrs.get("fan_speed_list"), list) else []
    if not speeds:
        return "<p>No vacuum suction speed list is currently exposed by Home Assistant.</p>"
    options = []
    current = str(attrs.get("fan_speed") or "")
    for speed in speeds:
        selected = " selected" if str(speed) == current else ""
        options.append(f'<option value="{_e(speed)}"{selected}>{_e(speed)}</option>')
    return f"""<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status">
<h3>Suction Speed</h3>
<input type="hidden" name="entity_id" value="{_e(entity_id)}">
<label for="vacuum_fan_speed">Suction Speed</label>
<select id="vacuum_fan_speed" name="fan_speed">{''.join(options)}</select>
<button type="submit" data-async="true" data-status-target="vacuum_control_status">Set Suction Speed</button>
</form>"""


def _vacuum_dock_actions_form():
    return """<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status">
<h3>Dock Actions</h3>
<input type="hidden" name="entity_id" value="vacuum.cinderella">
<input type="hidden" name="command" value="app_start_collect_dust">
<button type="submit" data-async="true" data-status-target="vacuum_control_status">Empty Dock Now</button>
</form>"""


def _vacuum_dock_empty_mode_form(controls):
    control = next((item for item in controls if item.get("entity_id") == "select.cinderella_dock_empty_mode"), None)
    if not control:
        return "<p>Dock empty mode is not currently exposed by Home Assistant.</p>"
    attrs = control.get("attributes") or {}
    options = attrs.get("options") if isinstance(attrs.get("options"), list) else []
    if not options:
        return "<p>Dock empty mode is not currently reporting options.</p>"
    current = str(control.get("state") or "")
    option_tags = "".join(f'<option value="{_e(option)}"{ " selected" if str(option) == current else ""}>{_e(option)}</option>' for option in options)
    return f"""<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status">
<h3>Dock Empty Mode</h3>
<input type="hidden" name="entity_id" value="select.cinderella_dock_empty_mode">
<label for="vacuum_dock_empty_mode">Dock Empty Mode</label>
<select id="vacuum_dock_empty_mode" name="option">{option_tags}</select>
<button type="submit" data-async="true" data-status-target="vacuum_control_status">Set Dock Empty Mode</button>
</form>"""


def _vacuum_room_clean_form(settings, control):
    rooms = ((control.get("vacuum_rooms") or {}).get("vacuum.cinderella") or [])
    current_mode = str(settings.get("vacuum_cleaning_mode") or "vacuum_mop")
    mode_options = "".join(
        f'<option value="{value}"{ " selected" if value == current_mode else ""}>{label}</option>'
        for value, label in (
            ("vacuum_mop", "Vacuum and Mop"),
            ("vacuum_only", "Vacuum Only"),
            ("mop_only", "Mop Only"),
        )
    )
    if rooms:
        room_choices = "".join(
            f'<label class="room-option" for="vacuum_room_{_e(room.get("segment"))}">'
            f'<input id="vacuum_room_{_e(room.get("segment"))}" type="checkbox" name="segments" value="{_e(room.get("segment"))}"> '
            f'{_e(room.get("name") or room.get("label") or "Room")}<span class="muted">, room ID {_e(room.get("segment"))}</span></label>'
            for room in rooms
        )
        room_body = f"""<fieldset>
<legend>Choose Rooms To Clean</legend>
<div class="room-grid">{room_choices}</div>
</fieldset>"""
    else:
        room_body = "<p>No room choices are saved yet. Press Refresh Room Choices first.</p>"
    return f"""<form action="/ui/vacuum/rooms" method="post">
<h3>Room Cleaning</h3>
<input type="hidden" name="entity_id" value="vacuum.cinderella">
<button type="submit" class="secondary">Refresh Room Choices</button>
</form>
<form action="/ui/vacuum/room_clean" method="post" data-async="true" data-status-target="vacuum_control_status">
<h3>Room Cleaning</h3>
<input type="hidden" name="entity_id" value="vacuum.cinderella">
<label for="vacuum_cleaning_mode">Cleaning Job Mode</label>
<select id="vacuum_cleaning_mode" name="vacuum_cleaning_mode">{mode_options}</select>
{room_body}
<label for="vacuum_room_repeat">Repeat Count</label>
<input id="vacuum_room_repeat" name="repeat" type="number" min="1" max="3" value="{_e(settings.get('vacuum_room_repeat_count', 1))}">
<button type="submit" data-async="true" data-status-target="vacuum_control_status">Clean Selected Rooms</button>
</form>"""


def _vacuum_control_table(controls):
    if not controls:
        return "<p>No extra Roborock controls were found yet. Refresh the page after Home Assistant finishes loading the Roborock integration.</p>"
    rows = ["<h3>Roborock Entities</h3><table><thead><tr><th>Control</th><th>State</th><th>Action</th></tr></thead><tbody>"]
    for control in controls:
        entity_id = control.get("entity_id", "")
        if entity_id == "select.cinderella_dock_empty_mode":
            continue
        domain = control.get("domain", "")
        attrs = control.get("attributes") or {}
        label = control.get("friendly_name") or entity_id
        action = ""
        if domain == "select":
            options = attrs.get("options") if isinstance(attrs.get("options"), list) else []
            option_tags = "".join(f'<option value="{_e(option)}"{ " selected" if str(option) == str(control.get("state")) else ""}>{_e(option)}</option>' for option in options)
            action = f'<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status"><input type="hidden" name="entity_id" value="{_e(entity_id)}"><select name="option">{option_tags}</select><button type="submit" data-async="true" data-status-target="vacuum_control_status">Set</button></form>'
        elif domain == "number":
            action = f'<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status"><input type="hidden" name="entity_id" value="{_e(entity_id)}"><input name="value" type="number" min="{_e(attrs.get("min", 0))}" max="{_e(attrs.get("max", 100))}" step="{_e(attrs.get("step", 1))}" value="{_e(control.get("state", ""))}"><button type="submit" data-async="true" data-status-target="vacuum_control_status">Set</button></form>'
        elif domain == "switch":
            next_state = "false" if str(control.get("state")).lower() == "on" else "true"
            label_text = "Turn Off" if next_state == "false" else "Turn On"
            action = f'<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status"><input type="hidden" name="entity_id" value="{_e(entity_id)}"><input type="hidden" name="state" value="{next_state}"><button type="submit" data-async="true" data-status-target="vacuum_control_status">{label_text}</button></form>'
        elif domain == "button":
            action = f'<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status"><input type="hidden" name="entity_id" value="{_e(entity_id)}"><button type="submit" data-async="true" data-status-target="vacuum_control_status">Press</button></form>'
        elif domain == "vacuum":
            speeds = attrs.get("fan_speed_list") if isinstance(attrs.get("fan_speed_list"), list) else []
            if speeds:
                option_tags = "".join(f'<option value="{_e(speed)}"{ " selected" if str(speed) == str(attrs.get("fan_speed")) else ""}>{_e(speed)}</option>' for speed in speeds)
                action = f'<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status"><input type="hidden" name="entity_id" value="{_e(entity_id)}"><select name="fan_speed">{option_tags}</select><button type="submit" data-async="true" data-status-target="vacuum_control_status">Set Suction</button></form>'
            else:
                action = "<span class='muted'>Status only</span>"
        elif domain == "fan":
            presets = attrs.get("preset_modes") if isinstance(attrs.get("preset_modes"), list) else []
            preset_tags = "".join(f'<option value="{_e(preset)}"{ " selected" if str(preset) == str(attrs.get("preset_mode")) else ""}>{_e(preset)}</option>' for preset in presets)
            action = f'<form action="/ui/vacuum/control" method="post" data-async="true" data-status-target="vacuum_control_status"><input type="hidden" name="entity_id" value="{_e(entity_id)}"><select name="preset_mode">{preset_tags}</select><button type="submit" data-async="true" data-status-target="vacuum_control_status">Set Preset</button></form>'
        else:
            action = "<span class='muted'>Status only</span>"
        action = action.replace('<select ', f'<select aria-label="{_e(label)}" ')
        action = action.replace('<input name="value"', f'<input aria-label="{_e(label)}" name="value"')
        for caption in ("Set", "Turn On", "Turn Off", "Press", "Set Suction", "Set Preset"):
            action = action.replace(f'>{caption}</button>', f'>{caption} {_e(label)}</button>')
        rows.append(f"<tr><td>{_e(label)}<br><span class='muted'>{_e(entity_id)}</span></td><td>{_e(control.get('state', ''))}</td><td>{action}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _vacuum_status(vacuum, status_sensor=None):
    if not vacuum:
        return "<p>Vacuum status has not refreshed yet.</p>"
    status = vacuum.get("state") or "unknown"
    if not vacuum.get("ok"):
        status = f"{status} - needs attention"
    sensor_state = (status_sensor or {}).get("state") or ""
    extra = f"<p><strong>Roborock status:</strong> {_e(sensor_state)}</p>" if sensor_state else ""
    return f"<p><strong>Current vacuum status:</strong> {_e(status)}</p>{extra}"


def _refrigerator_status(refrigerator):
    if not refrigerator:
        return "<p>Refrigerator status has not refreshed yet.</p>"
    labels = {
        "ice_maker": "Ice Maker",
        "fridge_door": "Fridge Door",
        "freezer_door": "Freezer Door",
        "filter_usage": "Water Filter Usage",
        "filter_status": "Filter Status",
    }
    rows = ["<table><thead><tr><th>Item</th><th>Status</th></tr></thead><tbody>"]
    for key, label in labels.items():
        item = refrigerator.get(key) or {}
        status = item.get("state") or "unknown"
        if item and not item.get("ok"):
            status = f"{status} - needs attention"
        rows.append(f"<tr><td>{_e(label)}</td><td>{_e(status)}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _settings_form(settings):
    gemini_status = "configured" if settings.get("gemini_configured") else "not configured"
    openai_status = "configured" if settings.get("openai_configured") else "not configured"
    pushover_status = "configured" if settings.get("pushover_configured") else "not configured"
    return f"""<form action="/ui/settings" method="post">
<p><strong>Gemini key:</strong> {_e(gemini_status)}</p>
<p><strong>OpenAI key:</strong> {_e(openai_status)}</p>
<p><strong>Pushover:</strong> {_e(pushover_status)}</p>
<label for="external_base_url">External Core URL For Phone Links</label>
<input id="external_base_url" name="external_base_url" value="{_e(settings.get('external_base_url', ''))}" placeholder="http://100.x.x.x:8099">
<label for="speaker_base_url">Speaker Media URL On Home Network</label>
<input id="speaker_base_url" name="speaker_base_url" value="{_e(settings.get('speaker_base_url', ''))}" placeholder="http://homeassistant.local:8099">
<label for="gemini_vision_model">Gemini Vision Model</label>
<input id="gemini_vision_model" name="gemini_vision_model" value="{_e(settings.get('gemini_vision_model', 'gemini-3.5-flash'))}">
<label for="doorbell_dedupe_seconds">Doorbell Duplicate Window, Seconds</label>
<input id="doorbell_dedupe_seconds" name="doorbell_dedupe_seconds" type="number" min="5" max="180" value="{_e(settings.get('doorbell_dedupe_seconds', 30))}">
<label for="fridge_stale_minutes">Refrigerator Update Freshness Window, Minutes</label>
<input id="fridge_stale_minutes" name="fridge_stale_minutes" type="number" min="5" max="240" value="{_e(settings.get('fridge_stale_minutes', 45))}">
<label for="vacuum_repeat_quiet_minutes">Vacuum Repeat Quiet Time, Minutes</label>
<input id="vacuum_repeat_quiet_minutes" name="vacuum_repeat_quiet_minutes" type="number" min="1" max="240" value="{_e(settings.get('vacuum_repeat_quiet_minutes', 20))}">
<label for="vacuum_announce_events">Vacuum Events To Announce</label>
<input id="vacuum_announce_events" name="vacuum_announce_events" value="{_e(', '.join(settings.get('vacuum_announce_events', [])))}">
<label for="gemini_api_key">New Gemini API Key</label>
<input id="gemini_api_key" name="gemini_api_key" type="password" autocomplete="off">
<label><input name="clear_gemini_api_key" type="checkbox" value="true"> Clear saved Gemini key</label>
<label for="openai_api_key">New OpenAI API Key</label>
<input id="openai_api_key" name="openai_api_key" type="password" autocomplete="off">
<label><input name="clear_openai_api_key" type="checkbox" value="true"> Clear saved OpenAI key</label>
<label for="pushover_user_key">Pushover User Key</label>
<input id="pushover_user_key" name="pushover_user_key" type="password" autocomplete="off">
<label for="pushover_api_token">Pushover API Token</label>
<input id="pushover_api_token" name="pushover_api_token" type="password" autocomplete="off">
<label><input name="clear_pushover" type="checkbox" value="true"> Clear saved Pushover keys</label>
<button type="submit">Save Settings</button>
</form>"""


def _vacuum_message_studio(settings):
    messages = _default_cinderella_messages(settings.get("cinderella_messages"))
    bucket_labels = [
        ("departure", "Departure"),
        ("washing", "Mop Washing"),
        ("emptying", "Dock Emptying"),
        ("drying", "Mop Drying"),
        ("returning", "Returning"),
        ("victory", "Finished"),
        ("paused", "Paused"),
        ("status_update", "Status Update"),
        ("vacuum_error_templates", "Vacuum Error Templates"),
        ("dock_error_templates", "Dock Error Templates"),
    ]
    fields = []
    for bucket, label in bucket_labels:
        value = "\n".join(messages.get(bucket) or [])
        fields.append(
            f'<label for="cinderella_{_e(bucket)}">{_e(label)}</label>'
            f'<textarea id="cinderella_{_e(bucket)}" name="cinderella_{_e(bucket)}" rows="3">{_e(value)}</textarea>'
        )
    specific_json = json.dumps(messages.get("specific_errors") or {}, indent=2, sort_keys=True)
    return f"""<form action="/ui/settings" method="post">
{''.join(fields)}
<label for="cinderella_specific_errors_json">Specific Error Messages JSON</label>
<textarea id="cinderella_specific_errors_json" name="cinderella_specific_errors_json" rows="10">{_e(specific_json)}</textarea>
<button type="submit">Save Robot Messages</button>
</form>"""


def _default_cinderella_messages(value):
    source = value if isinstance(value, dict) else {}
    defaults = {
        "departure": [
            "The floor goblin has been released.",
            "Dust, your time has come.",
            "Cinderella has chosen violence against dirt.",
        ],
        "washing": [
            "Mop spa day has begun.",
            "She is rinsing off the evidence.",
            "The mop is being reborn.",
        ],
        "emptying": [
            "Dumping today's bad decisions.",
            "The dirt vault is full.",
            "She is disposing of the evidence.",
        ],
        "drying": [
            "Drying cycle engaged.",
            "The mop is becoming socially acceptable again.",
            "Moisture is being aggressively removed.",
        ],
        "returning": [
            "Returning home like she pays rent.",
            "Cinderella is done being brave.",
            "Retreating with dignity... barely.",
        ],
        "victory": [
            "The floor has been defeated.",
            "Cinderella demands recognition.",
            "Victory has been achieved.",
        ],
        "paused": [
            "Paused for existential reasons.",
            "Cinderella is buffering.",
            "The robot has stopped and is judging silently.",
        ],
        "status_update": [
            "Cinderella has entered a weird little robot state.",
            "The vacuum has changed modes and would like attention.",
            "Cinderella reports a status change from the floor front.",
        ],
        "vacuum_error_templates": [
            "Cinderella has entered her villain arc. Error: {error}.",
            "The robot is having a moment. Error: {error}.",
            "This is not going well. Error: {error}.",
        ],
        "dock_error_templates": [
            "The dock is being weird again. Problem: {error}.",
            "Cinderella's parking spot has opinions. Problem: {error}.",
            "Dock drama detected. Issue: {error}.",
        ],
        "specific_errors": {},
    }
    merged = {**defaults}
    for key in defaults:
        if key == "specific_errors":
            if isinstance(source.get(key), dict):
                merged[key] = source[key]
        elif isinstance(source.get(key), list):
            merged[key] = source[key]
    return merged


def _voice_form(settings):
    engine = settings.get("tts_engine", "home_assistant")
    voice = settings.get("gemini_tts_voice", "Sulafat")
    speed = settings.get("gemini_tts_speed", "fast")
    engine_options = "".join(
        f'<option value="{_e(value)}"{ " selected" if value == engine else ""}>{_e(label)}</option>'
        for value, label in [
            ("home_assistant", "Home Assistant TTS"),
            ("gemini", "Gemini TTS"),
            ("openai", "OpenAI TTS"),
        ]
    )
    voice_options = "".join(
        f'<option value="{_e(item)}"{ " selected" if item == voice else ""}>{_e(item)}</option>'
        for item in [
            "Sulafat", "Kore", "Puck", "Iapetus", "Aoede", "Zephyr", "Charon", "Fenrir", "Leda", "Orus",
            "Callirrhoe", "Autonoe", "Enceladus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib",
            "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
            "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager",
        ]
    )
    speed_options = "".join(
        f'<option value="{_e(value)}"{ " selected" if value == speed else ""}>{_e(label)}</option>'
        for value, label in [("slow", "Slow"), ("normal", "Normal"), ("fast", "Fast"), ("very_fast", "Very Fast")]
    )
    openai_voice = settings.get("openai_tts_voice", "coral")
    openai_speed = settings.get("openai_tts_speed", "fast")
    openai_voice_options = "".join(
        f'<option value="{_e(item)}"{ " selected" if item == openai_voice else ""}>{_e(item)}</option>'
        for item in ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse", "marin", "cedar"]
    )
    openai_speed_options = "".join(
        f'<option value="{_e(value)}"{ " selected" if value == openai_speed else ""}>{_e(label)}</option>'
        for value, label in [("slow", "Slow"), ("normal", "Normal"), ("fast", "Fast"), ("very_fast", "Very Fast")]
    )
    keep_warm_checked = " checked" if settings.get("gemini_tts_keep_warm") else ""
    gemini_hidden = "" if engine == "gemini" else " hidden"
    openai_hidden = "" if engine == "openai" else " hidden"
    return f"""<form action="/ui/voice/settings" method="post">
<label for="tts_engine">Speech Engine</label>
<select id="tts_engine" name="tts_engine">{engine_options}</select>
<div data-tts-engine="gemini"{gemini_hidden}>
<h3>Gemini Voice</h3>
<label for="gemini_tts_model">Gemini TTS Model</label>
<input id="gemini_tts_model" name="gemini_tts_model" value="{_e(settings.get('gemini_tts_model', 'gemini-3.1-flash-tts-preview'))}">
<label for="gemini_tts_voice">Gemini Voice</label>
<select id="gemini_tts_voice" name="gemini_tts_voice">{voice_options}</select>
<label for="gemini_tts_speed">Speech Speed</label>
<select id="gemini_tts_speed" name="gemini_tts_speed">{speed_options}</select>
<label for="gemini_tts_style">Delivery Style</label>
<input id="gemini_tts_style" name="gemini_tts_style" value="{_e(settings.get('gemini_tts_style', 'warm, clear, friendly'))}">
<label for="gemini_tts_min_interval_seconds">Minimum Seconds Between Gemini TTS Calls</label>
<input id="gemini_tts_min_interval_seconds" name="gemini_tts_min_interval_seconds" type="number" min="0" max="600" value="{_e(settings.get('gemini_tts_min_interval_seconds', 0))}">
<label><input name="gemini_tts_keep_warm" type="checkbox" value="true"{keep_warm_checked}> Keep Gemini TTS Warm</label>
</div>
<div data-tts-engine="openai"{openai_hidden}>
<h3>OpenAI Voice</h3>
<label for="openai_tts_model">OpenAI TTS Model</label>
<input id="openai_tts_model" name="openai_tts_model" value="{_e(settings.get('openai_tts_model', 'gpt-4o-mini-tts'))}">
<label for="openai_tts_voice">OpenAI Voice</label>
<select id="openai_tts_voice" name="openai_tts_voice">{openai_voice_options}</select>
<label for="openai_tts_speed">OpenAI Speech Speed</label>
<select id="openai_tts_speed" name="openai_tts_speed">{openai_speed_options}</select>
<label for="openai_tts_instructions">OpenAI Voice Instructions</label>
<input id="openai_tts_instructions" name="openai_tts_instructions" value="{_e(settings.get('openai_tts_instructions', 'Warm, clear, friendly home assistant voice.'))}">
</div>
<button type="submit">Save Voice Settings</button>
</form>"""


def _voice_test_form():
    return """<form action="/ui/voice/test" method="post" data-async="true" data-status-target="voice_test_status">
<p id="voice_test_status" class="status-line" role="status" aria-live="polite"></p>
<label for="voice_test_message">Test Message</label>
<textarea id="voice_test_message" name="message" rows="3">Viper Core voice test is working.</textarea>
<button type="submit" data-async="true" data-status-target="voice_test_status">Test Voice</button>
</form>"""


def _diagnostics(state):
    devices = state.get("devices") or {}
    runtime = state.get("runtime") or {}
    checks = [
        ("Home Assistant API", (state.get("home_assistant") or {}).get("ok"), (state.get("home_assistant") or {}).get("message")),
        ("Required entities", (state.get("dependencies") or {}).get("ok"), (state.get("dependencies") or {}).get("message")),
        ("FFmpeg", bool(runtime.get("ffmpeg")), runtime.get("ffmpeg") or "not found"),
        ("Heat pumps", bool(devices.get("heat_pumps")) and all(item.get("ok") for item in devices.get("heat_pumps", [])), f"{len(devices.get('heat_pumps') or [])} configured"),
        ("Airflow controls", bool(devices.get("airflow")) and all(item.get("ok") for item in devices.get("airflow", [])), f"{len(devices.get('airflow') or [])} configured"),
        ("Vacuum", (devices.get("vacuum") or {}).get("ok"), (devices.get("vacuum") or {}).get("state", "unknown")),
        ("Refrigerator", all((devices.get("refrigerator") or {}).get(key, {}).get("ok") for key in ("ice_maker", "fridge_door", "freezer_door")), "door sensors and ice maker"),
    ]
    rows = ["<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead><tbody>"]
    if (state.get("control") or {}).get("installation_profile") == "doorbells":
        checks = checks[:3]
    for name, ok, detail in checks:
        rows.append(f"<tr><td>{_e(name)}</td><td>{'OK' if ok else 'Needs attention'}</td><td>{_e(detail or '')}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _recent_events(events):
    if not events:
        return "<p>No events yet.</p>"
    rows = ["<ul>"]
    for item in list(events)[-20:][::-1]:
        rows.append(f"<li>{_e(item.get('event_type', 'event'))}: {_e(item.get('message', ''))}</li>")
    rows.append("</ul>")
    return "".join(rows)


def _post_button(action, name, value, label, async_action=False, status_target="async_status"):
    action = "/" + str(action or "").lstrip("/")
    hidden = f'<input type="hidden" name="{_e(name)}" value="{_e(value)}">' if name else ""
    async_attrs = f' data-async="true" data-status-target="{_e(status_target)}"' if async_action else ""
    return f'<form action="{_e(action)}" method="post"{async_attrs}>{hidden}<button type="submit">{_e(label)}</button></form>'


def _display_name(value):
    return str(value or "").title().replace("'S", "'s")


def _format_bytes(value):
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return "0 B"


def _e(value):
    return escape("" if value is None else str(value), quote=True)
