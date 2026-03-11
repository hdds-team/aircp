/**
 * Issues Store -- GitHub issue tracking from daemon HTTP API
 *
 * Phase 1 (read-only MVP):
 * - Fetches cached issues from daemon (GET /api/github/issues)
 * - Fetches pending action queue (GET /api/github/queue)
 * - Assign agents to issues (POST /api/github/assign)
 * - Approve/reject queued actions (POST /api/github/approve, /reject)
 * - Polls every 60s (issues change slower than tasks/reviews)
 *
 * No DDS subscription in Phase 1 -- HTTP polling only.
 * Reference: docs/_private/WIP_DASHBOARD_ISSUES_PANEL.md
 */
import { settingsStore } from './settings.svelte.js';

const SYNC_INTERVAL = 60_000;

let issues = $state([]);
let queue = $state([]);
let loading = $state(false);
let error = $state(null);
let _syncTimer = null;

// -- Derived state --

let unassigned = $derived(
  issues.filter(i => !i.agents || i.agents.length === 0)
);

let inProgress = $derived(
  issues.filter(i => i.agents && i.agents.length > 0)
);

let pendingCount = $derived(
  queue.filter(a => a.status === 'pending').length
);

// -- Fetch --

async function _fetchIssues() {
  try {
    loading = true;
    const [issRes, qRes] = await Promise.all([
      fetch('/api/github/issues'),
      fetch('/api/github/queue'),
    ]);

    if (issRes.ok) {
      const data = await issRes.json();
      issues = (data.issues || []).map(_mapIssue);
    }

    if (qRes.ok) {
      const data = await qRes.json();
      queue = data.queue || [];
    }

    error = null;
  } catch (e) {
    error = e.message;
    console.warn('[issues] Fetch failed:', e);
  } finally {
    loading = false;
  }
}

function _mapIssue(raw) {
  return {
    number: raw.issue_number || raw.number,
    title: raw.title || '',
    body: raw.body || '',
    state: raw.state || 'open',
    labels: _parseLabels(raw.labels),
    url: raw.html_url || raw.url || '',
    authorLogin: raw.author_login || '',
    commentsCount: raw.comments_count || 0,
    createdAt: raw.created_at || '',
    updatedAt: raw.updated_at || '',
    agents: (raw.agents || []).map(a => ({
      agent: a.agent_id || a.agent,
      role: a.role || 'investigate',
      taskId: a.task_id || null,
    })),
  };
}

function _parseLabels(labels) {
  if (!labels) return [];
  if (typeof labels === 'string') {
    try { labels = JSON.parse(labels); } catch { return []; }
  }
  return labels.map(l => typeof l === 'string' ? l : l.name || '');
}

// -- Actions --

async function refreshFromGitHub() {
  try {
    loading = true;
    const res = await fetch('/api/github/issues?refresh=1');
    if (res.ok) {
      const data = await res.json();
      issues = (data.issues || []).map(_mapIssue);
      error = null;
    } else {
      const data = await res.json().catch(() => ({}));
      error = data.error || `HTTP ${res.status}`;
    }
  } catch (e) {
    error = e.message;
  } finally {
    loading = false;
  }
}

async function assignAgent(issueNumber, agentId, role = 'investigate') {
  try {
    const res = await fetch('/api/github/assign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        issue_number: issueNumber,
        agent_id: agentId,
        role,
      }),
    });
    if (res.ok) await _fetchIssues();
    return res.ok;
  } catch (e) {
    console.warn('[issues] Assign failed:', e);
    return false;
  }
}

async function approveAction(actionId) {
  try {
    const res = await fetch('/api/github/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action_id: actionId,
        approved_by: settingsStore.operatorId,
      }),
    });
    if (res.ok) await _fetchIssues();
    return res.ok;
  } catch (e) {
    console.warn('[issues] Approve failed:', e);
    return false;
  }
}

async function rejectAction(actionId) {
  try {
    const res = await fetch('/api/github/reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action_id: actionId,
        rejected_by: settingsStore.operatorId,
      }),
    });
    if (res.ok) await _fetchIssues();
    return res.ok;
  } catch (e) {
    console.warn('[issues] Reject failed:', e);
    return false;
  }
}

// -- Lifecycle --

async function init() {
  cleanup();
  await _fetchIssues();
  _syncTimer = setInterval(_fetchIssues, SYNC_INTERVAL);
}

function cleanup() {
  clearInterval(_syncTimer);
  _syncTimer = null;
}

// -- Export --

export const issuesStore = {
  get issues()       { return issues; },
  get queue()        { return queue; },
  get loading()      { return loading; },
  get error()        { return error; },
  get unassigned()   { return unassigned; },
  get inProgress()   { return inProgress; },
  get pendingCount() { return pendingCount; },
  assignAgent,
  approveAction,
  rejectAction,
  refreshFromGitHub,
  init,
  cleanup,
};
