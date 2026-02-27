/**
 * Mode Store — Coordination mode via DDS
 *
 * Commands go through DDS (CDR2-encoded AIRCP Messages on aircp/commands).
 * Live state updates come via DDS subscription on aircp/mode.
 * Initial state bootstrapped from HTTP.
 */
import { hdds } from '../lib/hdds-client.js';
import { TOPIC_MODE } from '../lib/topics.js';
import { publishCommand, unwrapPayload } from '../lib/aircp-commands.js';

let mode = $state('neutral');
let lead = $state('');
let muted = $state(false);
let muteRemaining = $state(0);
let unsub = null;

function onMode(rawSample) {
  const sample = unwrapPayload(rawSample);
  if (sample.mode) mode = sample.mode;
  if (sample.lead !== undefined) lead = sample.lead;
  if (sample.muted !== undefined) muted = sample.muted;
  if (sample.mute_remaining !== undefined) muteRemaining = sample.mute_remaining;
}

async function _fetchMode() {
  try {
    const [modeRes, muteRes] = await Promise.all([
      fetch('/api/mode'),
      fetch('/api/mute-status'),
    ]);
    if (modeRes.ok) {
      const data = await modeRes.json();
      mode = data.mode || 'neutral';
      lead = data.lead || '';
    }
    if (muteRes.ok) {
      const data = await muteRes.json();
      muted = data.muted || false;
      muteRemaining = data.remaining_seconds || 0;
    }
  } catch (e) {
    console.warn('[mode] Failed to fetch:', e);
  }
}

function setMode(newMode, newLead) {
  mode = newMode; // optimistic update
  lead = newLead || '';
  publishCommand('mode/set', { mode: newMode, lead: newLead || '' });
}

function stfu(minutes = 5) {
  muted = true; // optimistic update
  publishCommand('stfu', { minutes });
}

function unstfu() {
  muted = false; // optimistic update
  publishCommand('unstfu');
}

function stop() {
  mode = 'neutral'; // optimistic update
  lead = '';
  publishCommand('stop');
}

async function init() {
  cleanup();
  await _fetchMode();
  unsub = hdds.subscribe(TOPIC_MODE, onMode);
}

function cleanup() { unsub?.(); unsub = null; }

export const modeStore = {
  get mode() { return mode; },
  get lead() { return lead; },
  get muted() { return muted; },
  get muteRemaining() { return muteRemaining; },
  setMode,
  stfu,
  unstfu,
  stop,
  init,
  cleanup,
};
