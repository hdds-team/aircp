/**
 * Messages Store — Chat messages from DDS topics + HTTP history bootstrap
 * 
 * On connect: fetches history from daemon HTTP (via /api proxy)
 * Then live: subscribes to aircp/messages/{room} via hdds-ws
 */
import { hdds } from '../lib/hdds-client.js';
import { TOPIC_MESSAGES, DEFAULT_ROOMS, SYSTEM_BOTS, AGENTS } from '../lib/topics.js';
import { Cdr2Buffer, aircp } from '../lib/aircp_generated.ts';
import { projectStore } from './project.svelte.js';
import { settingsStore } from './settings.svelte.js';

const MAX_MESSAGES = 500;
const HISTORY_LIMIT = 100;

// Reactive state
let messages = $state([]);
let activeRoom = $state('#general');
let pendingInsert = $state('');
let unsubscribers = [];
let _seenIds = new Set();
let _notificationsEnabled = localStorage.getItem('aircp_notif') !== 'off';
let _historyLoaded = false; // don't notify for history messages

// Derived — filter by room + active project (if set)
let roomMessages = $derived(
  messages.filter(m => {
    if (m.room !== activeRoom) return false;
    const proj = projectStore.activeProject;
    if (!proj) return true; // "All projects" — show everything
    return !m.project || m.project === proj || m.project === 'default';
  })
);

let unreadCounts = $derived.by(() => {
  const counts = {};
  for (const room of DEFAULT_ROOMS) {
    counts[room] = messages.filter(m => m.room === room && m._unread).length;
  }
  return counts;
});

function _addMessage(msg) {
  if (_seenIds.has(msg.id)) return;
  _seenIds.add(msg.id);
  if (_seenIds.size > MAX_MESSAGES * 2) {
    _seenIds = new Set([..._seenIds].slice(-MAX_MESSAGES));
  }
  messages = [...messages, msg].slice(-MAX_MESSAGES);
}

function _addMessages(msgs) {
  const newMsgs = msgs.filter(m => !_seenIds.has(m.id));
  newMsgs.forEach(m => _seenIds.add(m.id));
  messages = [...messages, ...newMsgs]
    .sort((a, b) => a.timestamp - b.timestamp)
    .slice(-MAX_MESSAGES);
}

/**
 * Decode raw CDR2 bytes from hdds-ws _raw field using generated decoder.
 * This is the primary decode path — hdds-ws sends { _raw: "base64", data: "first_cdr_string" }
 * where data is just the message ID (not parseable JSON), so CDR2 decode is required.
 */
function _decodeCdr2Raw(base64str) {
  try {
    const binary = atob(base64str);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    const buf = new Cdr2Buffer(bytes);
    return aircp.decodeMessage(buf);
  } catch {
    return null;
  }
}

function _extractContent(msg) {
  // payload_json is a JSON-encoded string inside the AIRCP Message
  const pj = msg.payload_json;
  if (pj) {
    const str = typeof pj === 'string' ? pj : JSON.stringify(pj);
    try {
      const parsed = JSON.parse(str);
      return parsed.content || parsed.text || parsed.message || str;
    } catch {
      return str;
    }
  }
  // Direct fields (HTTP history format)
  if (msg.payload && typeof msg.payload === 'object') {
    return msg.payload.content || msg.payload.text || '';
  }
  return msg.content || msg.message || msg.text || '';
}

function _parseTimestamp(msg) {
  const ts = msg.timestamp_ns || msg.ts || msg.timestamp;
  if (!ts) return new Date();
  if (typeof ts === 'bigint') {
    // BigInt nanoseconds → milliseconds
    return new Date(Number(ts / 1000000n));
  }
  if (typeof ts === 'string' && ts.length > 15) {
    // Nanoseconds string → milliseconds
    return new Date(Number(BigInt(ts) / 1000000n));
  }
  if (typeof ts === 'number') {
    if (ts > 1e15) return new Date(ts / 1e6); // nanoseconds
    if (ts > 1e12) return new Date(ts);         // milliseconds
    return new Date(ts * 1000);                  // seconds
  }
  return new Date(ts);
}

function onMessage(sample, info) {
  // hdds-ws sends { _raw: "base64_cdr2", data: "first_cdr_string" }
  // hdds-client's unwrap fails (data is a UUID, not JSON) so we get the envelope.
  // Decode _raw CDR2 to get the actual Message with all fields.
  let msg = sample;
  if (sample._raw && typeof sample._raw === 'string') {
    const decoded = _decodeCdr2Raw(sample._raw);
    if (decoded) msg = decoded;
  }

  const parsed = {
    id: msg.id || crypto.randomUUID(),
    room: msg.room || activeRoom,
    from: msg.from_id || msg.from || '?',
    fromType: msg.from_type || 'agent',
    content: _extractContent(msg),
    kind: msg.kind || 'CHAT',
    timestamp: _parseTimestamp(msg),
    roomSeq: Number(msg.room_seq ?? 0),
    project: msg.project || '',
    _unread: (msg.room || activeRoom) !== activeRoom,
  };

  _addMessage(parsed);

  // Notify on operator mention (only for live messages, not history)
  if (_historyLoaded) {
    _notifyIfMentioned(parsed);
  }
}

/**
 * Browser notification when operator is mentioned in a message.
 * Also fires for @all. Skips own messages.
 */
function _notifyIfMentioned(msg) {
  if (!_notificationsEnabled) return;
  const opId = settingsStore.operatorId;
  if (msg.from === opId) return; // don't notify own messages
  const content = (msg.content || '').toLowerCase();
  if (!content.includes(opId.toLowerCase()) && !content.includes('@all')) return;

  // Browser Notification API
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification(`${msg.from} in ${msg.room}`, {
      body: (msg.content || '').slice(0, 120),
      icon: '🔔',
      tag: msg.id, // prevents duplicates
    });
  }

  // Also try to play a subtle notification sound
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 880;
    osc.type = 'sine';
    gain.gain.value = 0.1;
    osc.start();
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
    osc.stop(ctx.currentTime + 0.3);
  } catch { /* audio not available */ }
}

function setNotifications(enabled) {
  _notificationsEnabled = enabled;
}

/** Parse raw HTTP history messages into our internal format */
function _parseHistoryMessages(rawMessages, room, markUnread = false) {
  return rawMessages.map(m => {
    const from = typeof m.from === 'object' ? m.from.id : (m.from || m.from_id || '?');
    return {
      id: m.id || crypto.randomUUID(),
      room: m.room || room,
      from,
      fromType: (typeof m.from === 'object' ? m.from.type : m.from_type) || 'agent',
      content: _extractContent(m),
      kind: m.kind || 'CHAT',
      timestamp: _parseTimestamp(m),
      roomSeq: m.room_seq || m.seq || 0,
      project: m.project || '',
      _unread: markUnread ? (m.room || room) !== activeRoom : false,
    };
  });
}

/** Bootstrap: fetch history from daemon HTTP for a room */
async function _fetchHistory(room) {
  try {
    const proj = projectStore.activeProject;
    const projQs = proj ? `&project=${encodeURIComponent(proj)}` : '';
    const res = await fetch(`/api/history?room=${encodeURIComponent(room)}&limit=${HISTORY_LIMIT}${projQs}`);
    if (!res.ok) return;
    const data = await res.json();
    const rawMessages = data.messages || data.history || [];

    const parsed = _parseHistoryMessages(rawMessages, room);
    _addMessages(parsed);
    console.log(`[messages] Loaded ${parsed.length} history for ${room}${proj ? ` [${proj}]` : ''}`);
  } catch (e) {
    console.warn(`[messages] Failed to fetch history for ${room}:`, e);
  }
}

function switchRoom(room) {
  activeRoom = room;
  messages = messages.map(m =>
    m.room === room ? { ...m, _unread: false } : m
  );
}

function sendMessage(content, room) {
  const r = room || activeRoom;

  /** @type {import('../lib/aircp_generated.ts').aircp.Message} */
  const msg = {
    id: crypto.randomUUID(),
    room: r,
    from_id: settingsStore.operatorId,
    from_type: aircp.SenderType.USER,
    kind: aircp.MessageKind.CHAT,
    payload_json: JSON.stringify({ content }),
    timestamp_ns: BigInt(Date.now()) * 1000000n,
    protocol_version: '0.3.0',
    broadcast: true,
    to_agent_id: '',
    room_seq: 0n,
    project: projectStore.activeProject || '',
  };

  // Encode as CDR2 bytes using hddsgen-generated encoder
  const buf = new Cdr2Buffer(new ArrayBuffer(8192));
  aircp.encodeMessage(msg, buf);
  const bytes = buf.toBytes();

  // base64 encode for hdds-ws _raw passthrough
  let binary = '';
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }

  hdds.publish(TOPIC_MESSAGES(r), { _raw: btoa(binary) });
}

async function init() {
  cleanup();

  // 1. Bootstrap history from HTTP
  for (const room of DEFAULT_ROOMS) {
    await _fetchHistory(room);
  }

  // History loaded — future messages are live → enable notifications
  _historyLoaded = true;

  // 2. Subscribe to live DDS topics
  for (const room of DEFAULT_ROOMS) {
    const unsub = hdds.subscribe(TOPIC_MESSAGES(room), onMessage, {
      reliability: 'reliable',
      history_depth: 50,
    });
    unsubscribers.push(unsub);
  }
}

/** Re-fetch history on project switch.
 * Clears message cache and reloads from daemon to ensure
 * "All projects" shows every message, not just the previously fetched subset. */
async function refetchHistory() {
  _historyLoaded = false;
  messages = [];
  _seenIds = new Set();
  for (const room of DEFAULT_ROOMS) {
    await _fetchHistory(room);
  }
  _historyLoaded = true;
}

function cleanup() {
  unsubscribers.forEach(fn => fn());
  unsubscribers = [];
}

function getAgentColor(from) {
  return AGENTS[from]?.color || '#8b949e';
}

function isSystem(from) {
  return SYSTEM_BOTS.has(from);
}

function appendToInput(text) {
  pendingInsert = text;
}

function clearPendingInsert() {
  pendingInsert = '';
}

export const messagesStore = {
  get messages() { return messages; },
  get roomMessages() { return roomMessages; },
  get activeRoom() { return activeRoom; },
  get unreadCounts() { return unreadCounts; },
  get pendingInsert() { return pendingInsert; },
  switchRoom,
  sendMessage,
  refetchHistory,
  setNotifications,
  appendToInput,
  clearPendingInsert,
  getAgentColor,
  isSystem,
  init,
  cleanup,
};
