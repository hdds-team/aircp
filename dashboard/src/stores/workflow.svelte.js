/**
 * Workflow Store — Workflow pipeline state from DDS
 * Supports both standard and veloce (parallel multi-agent) modes.
 */
import { hdds } from '../lib/hdds-client.js';
import { TOPIC_WORKFLOWS } from '../lib/topics.js';
import { unwrapPayload } from '../lib/aircp-commands.js';

export const PHASES = [
  { id: 'request', label: 'REQ', icon: '\u{1F4CB}' },
  { id: 'brainstorm', label: 'BRAIN', icon: '\u{1F9E0}' },
  { id: 'vote', label: 'VOTE', icon: '\u{1F5F3}' },
  { id: 'code', label: 'CODE', icon: '\u2328' },
  { id: 'review', label: 'REVIEW', icon: '\u{1F50D}' },
  { id: 'test', label: 'TEST', icon: '\u{1F9EA}' },
  { id: 'livrable', label: 'SHIP', icon: '\u{1F680}' },
];

export const VELOCE_PHASES = [
  { id: 'request', label: 'REQ', icon: '\u{1F4CB}' },
  { id: 'brainstorm', label: 'BRAIN', icon: '\u{1F9E0}' },
  { id: 'vote', label: 'VOTE', icon: '\u{1F5F3}' },
  { id: 'architecture', label: 'ARCH', icon: '\u{1F3D7}' },
  { id: 'decompose', label: 'SPLIT', icon: '\u2702' },
  { id: 'decompose_vote', label: 'D-VOTE', icon: '\u{1F5F3}' },
  { id: 'parallel_code', label: 'PARA', icon: '\u26A1' },
  { id: 'review', label: 'X-REV', icon: '\u{1F50D}' },
  { id: 'integrate', label: 'GLUE', icon: '\u{1F9E9}' },
  { id: 'review_final', label: 'F-REV', icon: '\u{1F50E}' },
  { id: 'test', label: 'TEST', icon: '\u{1F9EA}' },
  { id: 'livrable', label: 'SHIP', icon: '\u{1F680}' },
];

let active = $state(false);
let feature = $state('');
let currentPhase = $state('');
let leadAgent = $state('');
let phaseStarted = $state(null);
let phaseTimeout = $state(0);
let extensions = $state(0);

// Veloce-specific state
let mode = $state('standard');
let chunks = $state([]);
let gateOpen = $state(false);
let chunksDone = $state(0);
let chunksTotal = $state(0);
let chunksActive = $state(0);

let unsub = null;

function onWorkflow(rawSample) {
  const sample = unwrapPayload(rawSample);
  if (sample.active !== undefined) active = sample.active;
  if (sample.feature) feature = sample.feature;
  if (sample.current_phase) currentPhase = sample.current_phase;
  if (sample.lead) leadAgent = sample.lead;
  if (sample.phase_started) phaseStarted = new Date(sample.phase_started);
  if (sample.phase_timeout) phaseTimeout = sample.phase_timeout;
  if (sample.extensions !== undefined) extensions = sample.extensions;

  // Veloce fields
  if (sample.mode) mode = sample.mode;
  if (sample.chunks) {
    const ci = sample.chunks;
    chunks = ci.chunks || [];
    gateOpen = ci.gate_open || false;
    chunksDone = ci.done || 0;
    chunksTotal = ci.total || 0;
    chunksActive = ci.active || 0;
  }
}

let isVeloce = $derived(mode === 'veloce');

let phases = $derived(isVeloce ? VELOCE_PHASES : PHASES);

let currentPhaseIndex = $derived(
  phases.findIndex(p => p.id === currentPhase)
);

let phaseProgress = $derived.by(() => {
  if (!phaseStarted || !phaseTimeout) return 0;
  const elapsed = (Date.now() - phaseStarted.getTime()) / 1000;
  return Math.min(100, (elapsed / phaseTimeout) * 100);
});

function init() {
  cleanup();
  unsub = hdds.subscribe(TOPIC_WORKFLOWS, onWorkflow, { reliability: 'reliable' });
}

function cleanup() { unsub?.(); unsub = null; }

export const workflowStore = {
  get active() { return active; },
  get feature() { return feature; },
  get currentPhase() { return currentPhase; },
  get leadAgent() { return leadAgent; },
  get currentPhaseIndex() { return currentPhaseIndex; },
  get phaseProgress() { return phaseProgress; },
  get extensions() { return extensions; },
  get isVeloce() { return isVeloce; },
  get mode() { return mode; },
  get phases() { return phases; },
  get chunks() { return chunks; },
  get gateOpen() { return gateOpen; },
  get chunksDone() { return chunksDone; },
  get chunksTotal() { return chunksTotal; },
  get chunksActive() { return chunksActive; },
  PHASES,
  VELOCE_PHASES,
  init,
  cleanup,
};
