/**
 * Agents Store — Presence from DDS + HTTP bootstrap
 */
import { hdds } from '../lib/hdds-client.js';
import { TOPIC_PRESENCE, AGENTS } from '../lib/topics.js';
import { unwrapPayload } from '../lib/aircp-commands.js';
import { settingsStore } from './settings.svelte.js';

let agents = $state({});
let unsub = null;
let _tickInterval;

const ONLINE_THRESHOLD = 30;
const AWAY_THRESHOLD = 120;

function onPresence(rawSample) {
  const sample = unwrapPayload(rawSample);
  const id = sample.agent_id || sample.from_id;
  if (!id) return;

  agents = {
    ...agents,
    [id]: {
      id,
      model: AGENTS[id]?.model || sample.model || '?',
      role: AGENTS[id]?.role || sample.role || '?',
      color: AGENTS[id]?.color || '#8b949e',
      health: sample.health || 'online',
      activity: sample.activity || sample.status || 'idle',
      currentTask: sample.current_task || sample.task_id || null,
      progress: sample.progress || null,
      load: sample.load || 0,
      lastSeen: Date.now(),
      capabilities: sample.capabilities || [],
    },
  };
}

let agentList = $derived(
  Object.values(agents).sort((a, b) => {
    const opId = settingsStore.operatorId;
    if (a.id === opId) return -1;
    if (b.id === opId) return 1;
    const healthOrder = { online: 0, away: 1, dead: 2 };
    return (healthOrder[a.health] || 2) - (healthOrder[b.health] || 2)
      || a.id.localeCompare(b.id);
  })
);

let onlineCount = $derived(
  Object.values(agents).filter(a => a.health === 'online').length
);

let totalCount = $derived(Object.keys(agents).length);

function tick() {
  const now = Date.now();
  let changed = false;
  const updated = { ...agents };
  for (const [id, agent] of Object.entries(updated)) {
    const elapsed = (now - agent.lastSeen) / 1000;
    let newHealth = 'online';
    if (elapsed > AWAY_THRESHOLD) newHealth = 'dead';
    else if (elapsed > ONLINE_THRESHOLD) newHealth = 'away';
    if (agent.health !== newHealth) {
      updated[id] = { ...agent, health: newHealth };
      changed = true;
    }
  }
  if (changed) agents = updated;
}

/** Bootstrap agents from daemon HTTP */
async function _fetchAgents() {
  try {
    const res = await fetch('/api/agents/presence');
    if (!res.ok) return;
    const data = await res.json();
    const list = data.agents || data || [];
    for (const a of list) {
      const id = a.agent_id || a.id;
      if (!id) continue;
      agents = {
        ...agents,
        [id]: {
          id,
          model: AGENTS[id]?.model || a.model || '?',
          role: AGENTS[id]?.role || a.role || '?',
          color: AGENTS[id]?.color || '#8b949e',
          health: a.health || (a.seconds_since_heartbeat > AWAY_THRESHOLD ? 'dead' : a.seconds_since_heartbeat > ONLINE_THRESHOLD ? 'away' : 'online'),
          activity: a.activity || a.status || 'idle',
          currentTask: a.current_task || a.task_id || null,
          progress: a.progress || null,
          load: a.load || 0,
          lastSeen: a.seconds_since_heartbeat != null ? Date.now() - (a.seconds_since_heartbeat * 1000) : Date.now(),
          capabilities: a.capabilities || [],
        },
      };
    }
    console.log(`[agents] Loaded ${list.length} agents from HTTP`);
  } catch (e) {
    console.warn('[agents] Failed to fetch:', e);
  }
}

async function init() {
  cleanup();

  // Seed known agents
  for (const [id, info] of Object.entries(AGENTS)) {
    if (!agents[id]) {
      agents = { ...agents, [id]: {
        id, ...info, health: 'dead', activity: 'idle',
        currentTask: null, progress: null, load: 0,
        lastSeen: 0, capabilities: [],
      }};
    }
  }

  // Bootstrap from HTTP
  await _fetchAgents();

  // Live DDS
  unsub = hdds.subscribe(TOPIC_PRESENCE, onPresence);
  _tickInterval = setInterval(tick, 10000);
}

function cleanup() {
  unsub?.();
  unsub = null;
  clearInterval(_tickInterval);
}

function getHealthIcon(health) {
  return { online: '\u25CF', away: '\u25CB', dead: '\u2715' }[health] || '?';
}

function getActivityIcon(activity) {
  return {
    coding: '\u2328', reviewing: '\u2295', brainstorming: '\u25CE',
    chatting: '\u25B8', working: '\u2328', idle: '\u00B7', away: '\u25CB',
  }[activity] || '\u00B7';
}

export const agentsStore = {
  get agents() { return agents; },
  get agentList() { return agentList; },
  get onlineCount() { return onlineCount; },
  get totalCount() { return totalCount; },
  getHealthIcon,
  getActivityIcon,
  init,
  cleanup,
};
