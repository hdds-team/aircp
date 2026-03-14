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
 * Multi-repo (Idea #13 -- Phase 1 switch-mode):
 * - Fetches available repos (GET /api/github/repos)
 * - Switch active repo (POST /api/github/repos/switch)
 * - All fetch calls pass ?repo= param for the current repo
 *
 * No DDS subscription in Phase 1 -- HTTP polling only.
 * Reference: docs/_private/WIP_DASHBOARD_ISSUES_PANEL.md
 */
import { settingsStore } from './settings.svelte.js';

const SYNC_INTERVAL = 60_000;

let issues = $state([]);
let queue = $state([]);
let repos = $state([]);
let currentRepo = $state(null); // { name, owner, owner_repo, active }
let loading = $state(false);
let error = $state(null);
let _syncTimer = null;

// -- Derived state --

let openIssues = $derived(
  issues.filter(i => i.state === 'open')
);

let unassigned = $derived(
  openIssues.filter(i => !i.agents || i.agents.length === 0)
);

let inProgress = $derived(
  openIssues.filter(i => i.agents && i.agents.length > 0)
);

let closedIssues = $derived(
  issues.filter(i => i.state !== 'open')
);

let pendingCount = $derived(
  queue.filter(a => a.status === 'pending').length
);

// -- Repo query param helper --

function _repoParam() {
  return currentRepo ? `repo=${encodeURIComponent(currentRepo.name)}` : '';
}

function _appendRepo(url) {
  const rp = _repoParam();
  if (!rp) return url;
  return url + (url.includes('?') ? '&' : '?') + rp;
}

// -- Fetch --

async function _fetchRepos() {
  try {
    const res = await fetch('/api/github/repos');
    if (res.ok) {
      const data = await res.json();
      repos = data.repos || [];
      // Set currentRepo to the active one if not already set
      if (!currentRepo && repos.length > 0) {
        const active = repos.find(r => r.active) || repos[0];
        currentRepo = active;
      }
    }
  } catch (e) {
    console.warn('[issues] Failed to fetch repos:', e);
  }
}

async function _fetchIssues() {
  try {
    loading = true;
    const [issRes, qRes] = await Promise.all([
      fetch(_appendRepo('/api/github/issues?state=all')),
      fetch(_appendRepo('/api/github/queue')),
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
    // First: refresh open issues from GitHub (updates cache, marks closed)
    const refreshRes = await fetch(_appendRepo('/api/github/issues?refresh=1'));
    if (!refreshRes.ok) {
      const data = await refreshRes.json().catch(() => ({}));
      error = data.error || `HTTP ${refreshRes.status}`;
      return;
    }
    // Then: fetch all (open + closed) from cache for display
    const res = await fetch(_appendRepo('/api/github/issues?state=all'));
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

async function switchRepo(repoName) {
  try {
    const res = await fetch('/api/github/repos/switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: repoName }),
    });
    if (res.ok) {
      // Update local state
      const target = repos.find(r => r.name === repoName);
      if (target) {
        // Update active flags
        repos = repos.map(r => ({ ...r, active: r.name === repoName }));
        currentRepo = { ...target, active: true };
      }
      // Reload issues for new repo
      await _fetchIssues();
      return true;
    } else {
      const data = await res.json().catch(() => ({}));
      error = data.error || `HTTP ${res.status}`;
      return false;
    }
  } catch (e) {
    error = e.message;
    console.warn('[issues] Switch repo failed:', e);
    return false;
  }
}

async function assignAgent(issueNumber, agentId, role = 'investigate') {
  try {
    const payload = {
      issue_number: issueNumber,
      agent_id: agentId,
      role,
    };
    if (currentRepo) payload.repo = currentRepo.name;

    const res = await fetch('/api/github/assign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
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
  await _fetchRepos();
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
  get openIssues()   { return openIssues; },
  get closedIssues() { return closedIssues; },
  get queue()        { return queue; },
  get loading()      { return loading; },
  get error()        { return error; },
  get unassigned()   { return unassigned; },
  get inProgress()   { return inProgress; },
  get pendingCount() { return pendingCount; },
  get repos()        { return repos; },
  get currentRepo()  { return currentRepo; },
  assignAgent,
  approveAction,
  rejectAction,
  refreshFromGitHub,
  switchRepo,
  init,
  cleanup,
};
