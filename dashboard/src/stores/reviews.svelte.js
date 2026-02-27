/**
 * Reviews Store — Review tracking from DDS + HTTP sync
 *
 * HTTP fetch is the source of truth (replaces full list).
 * DDS provides real-time additions/updates between syncs.
 * Periodic re-sync every 30s to catch status changes.
 */
import { hdds } from '../lib/hdds-client.js';
import { TOPIC_REVIEWS } from '../lib/topics.js';
import { unwrapPayload } from '../lib/aircp-commands.js';
import { projectStore } from './project.svelte.js';
import { settingsStore } from './settings.svelte.js';

const SYNC_INTERVAL = 30_000;

let reviews = $state([]);
let unsub = null;
let _syncTimer = null;

function onReview(rawSample) {
  const sample = unwrapPayload(rawSample);
  const id = sample.request_id || sample.id;
  if (!id) return;

  const review = {
    id,
    file: sample.file_path || sample.file || '',
    requestedBy: sample.requested_by || '',
    reviewers: sample.reviewers || [],
    type: sample.review_type || sample.type || 'doc',
    status: sample.status || 'pending',
    consensus: sample.consensus || null,
    approvalCount: sample.approval_count ?? sample.response_count ?? 0,
    minApprovals: sample.min_approvals || 1,
    createdAt: sample.created_at || new Date().toISOString(),
    closedAt: sample.closed_at || null,
    updatedAt: new Date().toISOString(),
  };

  const existing = reviews.findIndex(r => r.id === id);
  if (existing >= 0) {
    reviews = reviews.map((r, i) => i === existing ? { ...r, ...review } : r);
  } else {
    reviews = [...reviews, review];
  }
}

let activeReviews = $derived(
  reviews.filter(r => r.status === 'pending')
);

let closedReviews = $derived(
  reviews.filter(r => r.status !== 'pending')
);

async function _fetchReviews() {
  try {
    const proj = projectStore.activeProject;
    const qs = proj ? `?project=${encodeURIComponent(proj)}` : '';
    const [activeRes, historyRes] = await Promise.all([
      fetch(`/api/review/list${qs}`),
      fetch(`/api/review/history?limit=10${proj ? `&project=${encodeURIComponent(proj)}` : ''}`),
    ]);

    const merged = [];

    if (activeRes.ok) {
      const data = await activeRes.json();
      for (const r of (data.reviews || [])) {
        merged.push(_mapReview(r));
      }
    }

    if (historyRes.ok) {
      const data = await historyRes.json();
      for (const r of (data.reviews || [])) {
        // Avoid duplicates (shouldn't happen, but safe)
        if (!merged.some(m => m.id === r.id)) {
          merged.push(_mapReview(r));
        }
      }
    }

    reviews = merged;
  } catch (e) {
    console.warn('[reviews] Failed to fetch:', e);
  }
}

function _mapReview(r) {
  return {
    id: r.id,
    file: r.file_path || '',
    requestedBy: r.requested_by || '',
    reviewers: r.reviewers || [],
    type: r.review_type || 'doc',
    status: r.status || 'pending',
    consensus: r.consensus || null,
    approvalCount: r.response_count ?? 0,
    minApprovals: r.min_approvals || 1,
    createdAt: r.created_at || '',
    closedAt: r.closed_at || null,
    updatedAt: r.closed_at || r.created_at || '',
  };
}

async function init() {
  cleanup();
  await _fetchReviews();
  unsub = hdds.subscribe(TOPIC_REVIEWS, onReview, { reliability: 'reliable' });
  _syncTimer = setInterval(_fetchReviews, SYNC_INTERVAL);
}

function cleanup() {
  unsub?.();
  unsub = null;
  clearInterval(_syncTimer);
  _syncTimer = null;
}

async function approve(requestId, comment) {
  const res = await fetch('/api/review/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      request_id: requestId,
      reviewer: settingsStore.operatorId,
      comment: comment || 'LGTM',
    }),
  });
  if (res.ok) await _fetchReviews();
  return res.ok;
}

async function comment(requestId, text) {
  if (!text) return false;
  const res = await fetch('/api/review/comment', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      request_id: requestId,
      reviewer: settingsStore.operatorId,
      comment: text,
    }),
  });
  if (res.ok) await _fetchReviews();
  return res.ok;
}

async function requestChanges(requestId, text) {
  if (!text) return false;
  const res = await fetch('/api/review/changes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      request_id: requestId,
      reviewer: settingsStore.operatorId,
      comment: text,
    }),
  });
  if (res.ok) await _fetchReviews();
  return res.ok;
}

async function requestReview(filePath, reviewers, type) {
  const body = {
    file: filePath,
    type: type || 'doc',
    requested_by: settingsStore.operatorId,
  };
  if (reviewers && reviewers.length) body.reviewers = reviewers;
  const proj = projectStore.activeProject;
  if (proj) body.project_id = proj;
  const res = await fetch('/api/review/request', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.ok) await _fetchReviews();
  return res.ok;
}

export const reviewsStore = {
  get reviews() { return reviews; },
  get activeReviews() { return activeReviews; },
  get closedReviews() { return closedReviews; },
  approve,
  comment,
  requestChanges,
  requestReview,
  init,
  cleanup,
};
