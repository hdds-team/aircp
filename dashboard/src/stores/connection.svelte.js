/**
 * Connection Store — HDDS-WS connection state
 */
import { hdds } from '../lib/hdds-client.js';

let state = $state('disconnected');
let domain = $state(null);
let uptimeStart = $state(null);

function init() {
  hdds.on('state', (s) => {
    state = s;
    if (s === 'connected' && !uptimeStart) {
      uptimeStart = Date.now();
    }
  });
  hdds.on('welcome', (msg) => { domain = msg.domain; });
  hdds.connect();
}

function disconnect() {
  hdds.disconnect();
}

export const connectionStore = {
  get state() { return state; },
  get domain() { return domain; },
  get connected() { return state === 'connected'; },
  get uptimeStart() { return uptimeStart; },
  init,
  disconnect,
};
