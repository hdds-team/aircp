/**
 * Tasks Store — Task tracking from DDS + HTTP sync
 *
 * HTTP fetch is the source of truth (replaces full list).
 * DDS provides real-time additions/updates between syncs.
 * Periodic re-sync every 30s to catch status changes (cancelled, done).
 */
import { hdds } from '../lib/hdds-client.js';
import { TOPIC_TASKS } from '../lib/topics.js';
import { unwrapPayload } from '../lib/aircp-commands.js';
import { projectStore } from './project.svelte.js';
import { settingsStore } from './settings.svelte.js';

const SYNC_INTERVAL = 30_000; // re-fetch from HTTP every 30s

let tasks = $state([]);
let unsub = null;
let _syncTimer = null;

function onTask(rawSample) {
  const sample = unwrapPayload(rawSample);
  const id = sample.task_id || sample.id;
  if (!id) return;

  const task = {
    id,
    description: sample.description || '',
    agent: sample.agent_id || sample.agent || '?',
    status: sample.status || 'pending',
    progress: sample.progress || null,
    currentStep: sample.current_step || null,
    createdAt: sample.created_at || new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    result: sample.result || null,
  };

  const existing = tasks.findIndex(t => t.id === id);
  if (existing >= 0) {
    tasks = tasks.map((t, i) => i === existing ? { ...t, ...task } : t);
  } else {
    tasks = [...tasks, task];
  }
}

let activeTasks = $derived(
  tasks.filter(t => ['pending', 'in_progress'].includes(t.status))
);

let completedTasks = $derived(
  tasks.filter(t => ['done', 'failed', 'cancelled', 'stale'].includes(t.status))
);

async function _fetchTasks() {
  try {
    const proj = projectStore.activeProject;
    const url = proj ? `/api/tasks?project=${encodeURIComponent(proj)}` : '/api/tasks';
    const res = await fetch(url);
    if (!res.ok) return;
    const data = await res.json();
    const list = data.tasks || [];
    tasks = list.map(t => ({
      id: t.id || t.task_id,
      description: t.description || '',
      agent: t.agent_id || t.agent || '?',
      status: t.status || 'pending',
      progress: t.progress || null,
      currentStep: t.current_step || null,
      createdAt: t.created_at || '',
      updatedAt: t.updated_at || '',
      result: t.result || null,
    }));
  } catch (e) {
    console.warn('[tasks] Failed to fetch:', e);
  }
}

async function init() {
  cleanup();
  await _fetchTasks();
  unsub = hdds.subscribe(TOPIC_TASKS, onTask, { reliability: 'reliable' });
  _syncTimer = setInterval(_fetchTasks, SYNC_INTERVAL);
}

function cleanup() {
  unsub?.();
  unsub = null;
  clearInterval(_syncTimer);
  _syncTimer = null;
}

async function createTask(description, agentId) {
  const body = { description, agent_id: agentId || '@alpha' };
  const proj = projectStore.activeProject;
  if (proj) body.project_id = proj;
  const res = await fetch('/api/task', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.ok) await _fetchTasks();
  return res.ok;
}

async function claimTask(taskId) {
  const res = await fetch('/api/task/claim', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_id: taskId,
      agent_id: settingsStore.operatorId,
    }),
  });
  if (res.ok) await _fetchTasks();
  return res.ok;
}

async function completeTask(taskId, status) {
  const res = await fetch('/api/task/complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_id: taskId,
      status: status || 'done',
    }),
  });
  if (res.ok) await _fetchTasks();
  return res.ok;
}

export const tasksStore = {
  get tasks() { return tasks; },
  get activeTasks() { return activeTasks; },
  get completedTasks() { return completedTasks; },
  createTask,
  claimTask,
  completeTask,
  init,
  cleanup,
};
