/**
 * aIRCp Topic Map — DDS topics used by the dashboard
 * 
 * Each topic maps to an HDDS-WS subscription.
 * The daemon publishes on these topics, dashboard subscribes.
 */

// Chat messages per room (bidirectional)
// Must match daemon transport.py: topic_name = f"aircp/{room.lstrip('#')}"
export const TOPIC_MESSAGES = (room) => `aircp/${room.replace(/^#/, '')}`;

// Agent presence / heartbeats (subscribe only)
export const TOPIC_PRESENCE = 'aircp/presence';

// Task updates (subscribe only)
export const TOPIC_TASKS = 'aircp/tasks';

// Review updates (subscribe only)
export const TOPIC_REVIEWS = 'aircp/reviews';

// Workflow state changes (subscribe only)
export const TOPIC_WORKFLOWS = 'aircp/workflows';

// Mode changes (subscribe only)
export const TOPIC_MODE = 'aircp/mode';

// Commands from dashboard → daemon (publish only)
export const TOPIC_COMMANDS = 'aircp/commands';

// Rooms
export const DEFAULT_ROOMS = ['#general', '#brainstorm'];

// Agent definitions
export const AGENTS = {
  '@alpha':    { model: 'Claude Opus 4',   role: 'Lead dev',       color: '#f47067' },
  '@beta':     { model: 'Claude Opus 3',   role: 'QA / Review',    color: '#dcbdfb' },
  '@sonnet':   { model: 'Claude Sonnet 4', role: 'Analyse',        color: '#6cb6ff' },
  '@haiku':    { model: 'Claude Haiku',    role: 'Triage rapide',  color: '#8ddb8c' },
  '@mascotte': { model: 'Qwen3 (local)',   role: 'Assistant local', color: '#f69d50' },
  '@theta':    { model: 'LMStudio',        role: 'Assistant local', color: '#c084fc' },
  '@codex':    { model: 'GPT-5',           role: 'Code review',    color: '#e2c541' },
  '@naskel':   { model: 'Human',           role: 'Orchestrator',   color: '#57b8ff' },
};

// System bots (not shown as agents)
export const SYSTEM_BOTS = new Set([
  '@system', '@workflow', '@idea', '@review', '@taskman', '@watchdog', '@tips', '@brainstorm'
]);
