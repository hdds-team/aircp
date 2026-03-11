<script>
  import { agentsStore } from '../stores/agents.svelte.js';
  import { tasksStore } from '../stores/tasks.svelte.js';
  import { reviewsStore } from '../stores/reviews.svelte.js';
  import { issuesStore } from '../stores/issues.svelte.js';
  import { workflowStore } from '../stores/workflow.svelte.js';
  import { modeStore } from '../stores/mode.svelte.js';
  import { AGENTS } from '../lib/topics.js';
  import { settingsStore } from '../stores/settings.svelte.js';
  import FilePicker from './FilePicker.svelte';

  let activeTab = $state('agents');
  let now = $state(Date.now());
  let completedOpen = $state(false);
  let closedReviewsOpen = $state(false);

  // Issues panel state
  let assignOpen = $state(null);
  let assignAgent = $state('');
  let assignRole = $state('triage');
  let assignBusy = $state(false);

  // Request Review form state
  let reqRevOpen = $state(false);
  let reqRevFile = $state('');
  let reqRevType = $state('code');
  let reqRevReviewers = $state([]);
  let reqRevBusy = $state(false);
  let filePickerOpen = $state(false);

  const reviewerOptions = Object.keys(AGENTS).filter(id => id !== settingsStore.operatorId);
  const agentOptions = Object.keys(AGENTS).filter(id => id !== '@naskel');

  function toggleReviewer(id) {
    if (reqRevReviewers.includes(id)) {
      reqRevReviewers = reqRevReviewers.filter(r => r !== id);
    } else {
      reqRevReviewers = [...reqRevReviewers, id];
    }
  }

  async function submitReviewRequest() {
    const file = reqRevFile.trim();
    if (!file || reqRevBusy) return;
    reqRevBusy = true;
    try {
      const ok = await reviewsStore.requestReview(file, reqRevReviewers, reqRevType);
      if (ok) {
        reqRevOpen = false;
        reqRevFile = '';
        reqRevReviewers = [];
        reqRevType = 'code';
      }
    } catch (e) {
      console.warn('[review] Request failed:', e);
    } finally {
      reqRevBusy = false;
    }
  }

  function cancelReviewRequest() {
    reqRevOpen = false;
    reqRevFile = '';
    reqRevReviewers = [];
    reqRevType = 'code';
  }

  // Review action form state
  let reviewAction = $state(null);   // { id, type: 'approve'|'comment'|'changes' }
  let reviewComment = $state('');
  let reviewBusy = $state(false);

  function openReviewAction(id, type) {
    reviewAction = { id, type };
    reviewComment = type === 'approve' ? 'LGTM' : '';
  }

  function cancelReviewAction() {
    reviewAction = null;
    reviewComment = '';
    reviewBusy = false;
  }

  async function submitReviewAction() {
    if (!reviewAction || reviewBusy) return;
    reviewBusy = true;
    try {
      const { id, type } = reviewAction;
      let ok = false;
      if (type === 'approve') ok = await reviewsStore.approve(id, reviewComment);
      else if (type === 'comment') ok = await reviewsStore.comment(id, reviewComment);
      else if (type === 'changes') ok = await reviewsStore.requestChanges(id, reviewComment);
      if (ok) cancelReviewAction();
    } catch (e) {
      console.warn('[review] Action failed:', e);
    } finally {
      reviewBusy = false;
    }
  }

  async function submitAssign(issueNumber) {
    if (!assignAgent || assignBusy) return;
    assignBusy = true;
    try {
      const ok = await issuesStore.assignAgent(issueNumber, assignAgent, assignRole);
      if (ok) {
        assignOpen = null;
        assignAgent = '';
        assignRole = 'triage';
      }
    } catch (e) {
      console.warn('[issues] Assign failed:', e);
    } finally {
      assignBusy = false;
    }
  }

  const tabs = [
    { id: 'agents', label: 'Agents' },
    { id: 'tasks', label: 'Tasks' },
    { id: 'reviews', label: 'Rev' },
    { id: 'issues', label: 'Issues' },
    { id: 'workflow', label: 'Workflow' },
    { id: 'controls', label: 'Ctrl' },
  ];

  // Tick every second for live timestamps
  let _timer;
  $effect(() => {
    _timer = setInterval(() => { now = Date.now(); }, 1000);
    return () => clearInterval(_timer);
  });

  function timeAgo(ts) {
    if (!ts) return '';
    const diff = Math.max(0, Math.floor((now - ts) / 1000));
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  function timeAgoFromISO(isoStr) {
    if (!isoStr) return '';
    return timeAgo(new Date(isoStr).getTime());
  }

  function loadColor(load) {
    if (load > 0.7) return 'var(--danger)';
    if (load > 0.4) return 'var(--warning)';
    return 'var(--success)';
  }

  async function retryReview(review) {
    try {
      const res = await fetch('/review/request', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file: review.file,
          reviewers: review.reviewers,
          type: review.type,
          requested_by: review.requestedBy,
        }),
      });
      if (res.ok) {
        // Refresh store to pick up the new review
        reviewsStore.init();
      }
    } catch (e) {
      console.warn('[reviews] Retry failed:', e);
    }
  }

  // Sorted agent list: online > away > dead
  let sortedAgents = $derived(
    [...agentsStore.agentList].sort((a, b) => {
      const order = { online: 0, away: 1, dead: 2 };
      return (order[a.health] ?? 2) - (order[b.health] ?? 2) || a.id.localeCompare(b.id);
    })
  );
</script>

<aside class="intel">
  <!-- Tab bar -->
  <div class="tabs">
    {#each tabs as tab}
      <button
        class="tab"
        class:active={activeTab === tab.id}
        onclick={() => activeTab = tab.id}
      >
        {tab.label}
        {#if tab.id === 'tasks' && tasksStore.activeTasks.length > 0}
          <span class="tab-badge">{tasksStore.activeTasks.length}</span>
        {/if}
        {#if tab.id === 'reviews' && reviewsStore.activeReviews.length > 0}
          <span class="tab-badge">{reviewsStore.activeReviews.length}</span>
        {/if}
        {#if tab.id === 'issues' && issuesStore.pendingCount > 0}
          <span class="tab-badge">{issuesStore.pendingCount}</span>
        {/if}
      </button>
    {/each}
  </div>

  <div class="tab-content">
    <!-- AGENTS -->
    {#if activeTab === 'agents'}
      <div class="panel">
        {#each sortedAgents as agent}
          <div class="agent-card">
            <div class="agent-header">
              <span class="health health-{agent.health}">{agentsStore.getHealthIcon(agent.health)}</span>
              <span class="agent-name" style="color: {agent.color}">{agent.id}</span>
              <span class="agent-model">{agent.model}</span>
            </div>
            <div class="agent-meta">
              <span class="agent-activity">{agentsStore.getActivityIcon(agent.activity)} {agent.activity}</span>
              {#if agent.lastSeen}
                <span class="agent-seen">Last seen: {timeAgo(agent.lastSeen)}</span>
              {/if}
            </div>
            {#if agent.currentTask}
              <div class="agent-meta">
                <span class="agent-task">#{agent.currentTask}</span>
                {#if agent.progress}
                  <span class="agent-progress">{agent.progress}</span>
                {/if}
              </div>
            {/if}
            {#if agent.load > 0}
              <div class="load-bar">
                <div class="load-fill" style="width: {Math.min(agent.load * 100, 100)}%; background: {loadColor(agent.load)}"></div>
              </div>
            {/if}
          </div>
        {/each}
      </div>

    <!-- TASKS -->
    {:else if activeTab === 'tasks'}
      <div class="panel">
        {#if tasksStore.activeTasks.length === 0 && tasksStore.completedTasks.length === 0}
          <div class="empty-tab">No tasks</div>
        {/if}
        {#each tasksStore.activeTasks as task}
          <div class="task-card">
            <div class="task-header">
              <span class="task-id">#{task.id}</span>
              <span class="task-status status-{task.status}">{task.status}</span>
            </div>
            <div class="task-desc">{task.description}</div>
            <div class="task-meta">
              <span style="color: var(--text-muted)">{task.agent}</span>
              <span class="task-time">{timeAgoFromISO(task.createdAt)}</span>
            </div>
            {#if task.status === 'in_progress' && task.progress}
              <div class="task-progress-bar">
                <div class="task-progress-fill" style="width: {typeof task.progress === 'number' ? task.progress : parseInt(task.progress) || 0}%"></div>
              </div>
            {/if}
            <div class="task-actions">
              {#if task.status === 'pending'}
                <button class="ta-btn ta-claim" onclick={() => tasksStore.claimTask(task.id)}>Claim</button>
              {/if}
              <button class="ta-btn ta-done" onclick={() => tasksStore.completeTask(task.id, 'done')}>Done</button>
              <button class="ta-btn ta-fail" onclick={() => tasksStore.completeTask(task.id, 'failed')}>Fail</button>
              <button class="ta-btn ta-cancel" onclick={() => tasksStore.completeTask(task.id, 'cancelled')}>Cancel</button>
            </div>
          </div>
        {/each}

        {#if tasksStore.completedTasks.length > 0}
          <button class="completed-toggle" onclick={() => completedOpen = !completedOpen}>
            {completedOpen ? '\u25BE' : '\u25B8'} Completed ({tasksStore.completedTasks.length})
          </button>
          {#if completedOpen}
            {#each tasksStore.completedTasks as task}
              <div class="task-card task-done">
                <div class="task-header">
                  <span class="task-id">#{task.id}</span>
                  <span class="task-status status-{task.status}">{task.status}</span>
                </div>
                <div class="task-desc">{task.description}</div>
                <div class="task-meta">
                  <span style="color: var(--text-muted)">{task.agent}</span>
                  <span class="task-time">{timeAgoFromISO(task.createdAt)}</span>
                </div>
              </div>
            {/each}
          {/if}
        {/if}
      </div>

    <!-- REVIEWS -->
    {:else if activeTab === 'reviews'}
      <div class="panel">
        {#if reqRevOpen}
          <div class="req-rev-form">
            <div class="req-rev-title">Request Review</div>
            <div class="req-rev-file-row">
              <input
                type="text"
                class="req-rev-input"
                placeholder="File path (e.g. agents/base_agent.py)"
                bind:value={reqRevFile}
                onkeydown={(e) => e.key === 'Enter' && submitReviewRequest()}
                onkeyup={(e) => e.key === 'Escape' && cancelReviewRequest()}
              />
              <button class="req-rev-browse" onclick={() => filePickerOpen = !filePickerOpen}>...</button>
            </div>
            {#if filePickerOpen}
              <FilePicker
                onSelect={(path) => { reqRevFile = path; filePickerOpen = false; }}
                onCancel={() => filePickerOpen = false}
              />
            {/if}
            <div class="req-rev-row">
              <label class="req-rev-label">Type</label>
              <select class="req-rev-select" bind:value={reqRevType}>
                <option value="code">Code (2 approvals)</option>
                <option value="doc">Doc (1 approval)</option>
              </select>
            </div>
            <div class="req-rev-row">
              <label class="req-rev-label">Reviewers</label>
              <div class="req-rev-tags">
                {#each reviewerOptions as id}
                  <button
                    class="req-rev-tag"
                    class:selected={reqRevReviewers.includes(id)}
                    style="color: {AGENTS[id]?.color || 'var(--text-secondary)'}"
                    onclick={() => toggleReviewer(id)}
                  >{id}</button>
                {/each}
              </div>
            </div>
            {#if !reqRevReviewers.length}
              <div class="req-rev-hint">No reviewer selected = auto-assign</div>
            {/if}
            <div class="req-rev-actions">
              <button class="success" disabled={!reqRevFile.trim() || reqRevBusy} onclick={submitReviewRequest}>
                {reqRevBusy ? '...' : 'Request'}
              </button>
              <button onclick={cancelReviewRequest}>Cancel</button>
            </div>
          </div>
        {:else}
          <button class="req-rev-btn" onclick={() => reqRevOpen = true}>+ Request Review</button>
        {/if}

        {#if reviewsStore.activeReviews.length === 0 && reviewsStore.closedReviews.length === 0 && !reqRevOpen}
          <div class="empty-tab">No reviews</div>
        {/if}
        {#each reviewsStore.activeReviews as review}
          <div class="review-card">
            <div class="review-header">
              <span class="review-id">#{review.id}</span>
              <span class="review-status status-{review.status}">{review.status}</span>
            </div>
            <div class="review-file" title={review.file}>{review.file.split('/').slice(-2).join('/')}</div>
            <div class="review-meta">
              <span class="review-author" style="color: {AGENTS[review.requestedBy]?.color || 'var(--text-muted)'}">{review.requestedBy}</span>
              <span class="review-arrow">&rarr;</span>
              {#each review.reviewers as reviewer}
                <span class="review-reviewer" style="color: {AGENTS[reviewer]?.color || 'var(--text-muted)'}">{reviewer}</span>
              {/each}
            </div>
            <div class="review-meta">
              <span class="review-progress">{review.approvalCount}/{review.minApprovals} approvals</span>
              <span class="review-type">{review.type}</span>
              <span class="review-time">{timeAgoFromISO(review.createdAt)}</span>
            </div>

            {#if reviewAction && reviewAction.id === review.id}
              <div class="review-action-form">
                <input
                  type="text"
                  class="review-action-input"
                  placeholder={reviewAction.type === 'approve' ? 'LGTM' : 'Comment...'}
                  bind:value={reviewComment}
                  onkeydown={(e) => e.key === 'Enter' && submitReviewAction()}
                  onkeyup={(e) => e.key === 'Escape' && cancelReviewAction()}
                />
                <button
                  class="ra-submit"
                  class:success={reviewAction.type === 'approve'}
                  class:danger={reviewAction.type === 'changes'}
                  disabled={reviewBusy || (reviewAction.type !== 'approve' && !reviewComment.trim())}
                  onclick={submitReviewAction}
                >
                  {reviewBusy ? '...' : reviewAction.type === 'approve' ? 'OK' : reviewAction.type === 'changes' ? 'Send' : 'Post'}
                </button>
                <button class="ra-cancel" onclick={cancelReviewAction}>x</button>
              </div>
            {:else}
              <div class="review-actions">
                <button class="ra-btn ra-approve" onclick={() => openReviewAction(review.id, 'approve')}>Approve</button>
                <button class="ra-btn ra-comment" onclick={() => openReviewAction(review.id, 'comment')}>Comment</button>
                <button class="ra-btn ra-changes" onclick={() => openReviewAction(review.id, 'changes')}>Changes</button>
              </div>
            {/if}
          </div>
        {/each}

        {#if reviewsStore.closedReviews.length > 0}
          <button class="completed-toggle" onclick={() => closedReviewsOpen = !closedReviewsOpen}>
            {closedReviewsOpen ? '\u25BE' : '\u25B8'} Closed ({reviewsStore.closedReviews.length})
          </button>
          {#if closedReviewsOpen}
            {#each reviewsStore.closedReviews as review}
              <div class="review-card review-closed">
                <div class="review-header">
                  <span class="review-id">#{review.id}</span>
                  <span class="review-consensus consensus-{review.consensus || 'unknown'}">{review.consensus || review.status}</span>
                </div>
                <div class="review-file" title={review.file}>{review.file.split('/').slice(-2).join('/')}</div>
                <div class="review-meta">
                  <span style="color: var(--text-muted)">{review.requestedBy}</span>
                  {#if review.consensus === 'timeout'}
                    <button class="retry-btn" onclick={() => retryReview(review)} title="Retry review">&circlearrowright;</button>
                  {/if}
                  <span class="review-time">{timeAgoFromISO(review.closedAt || review.createdAt)}</span>
                </div>
              </div>
            {/each}
          {/if}
        {/if}
      </div>

    <!-- ISSUES -->
    {:else if activeTab === 'issues'}
      <div class="panel">
        <div class="issues-toolbar">
          <button class="issues-refresh" onclick={() => issuesStore.refreshFromGitHub()} disabled={issuesStore.loading}>
            {issuesStore.loading ? '...' : '↻ Refresh'}
          </button>
          {#if issuesStore.error}
            <span class="issues-error" title={issuesStore.error}>⚠</span>
          {/if}
        </div>

        {#if issuesStore.issues.length === 0 && !issuesStore.loading}
          <div class="empty-tab">No issues &mdash; click Refresh to fetch from GitHub</div>
        {/if}

        <!-- Unassigned -->
        {#if issuesStore.unassigned.length > 0}
          <div class="issues-section-title">Unassigned ({issuesStore.unassigned.length})</div>
          {#each issuesStore.unassigned as issue}
            <div class="issue-card">
              <div class="issue-header">
                <span class="issue-number">#{issue.number}</span>
                <span class="issue-title">{issue.title}</span>
                <a class="issue-link" href={issue.url} target="_blank" rel="noopener">&nearr;</a>
              </div>
              {#if issue.labels.length}
                <div class="issue-labels">
                  {#each issue.labels as label}
                    <span class="label-chip">{label}</span>
                  {/each}
                </div>
              {/if}
              {#if assignOpen === issue.number}
                <div class="assign-form">
                  <select class="assign-select" bind:value={assignAgent}>
                    <option value="">Agent...</option>
                    {#each agentOptions as id}
                      <option value={id}>{id}</option>
                    {/each}
                  </select>
                  <select class="assign-select" bind:value={assignRole}>
                    <option value="triage">triage</option>
                    <option value="investigate">investigate</option>
                    <option value="code">code</option>
                    <option value="review">review</option>
                  </select>
                  <button class="ia-btn ia-confirm" disabled={!assignAgent || assignBusy} onclick={() => submitAssign(issue.number)}>
                    {assignBusy ? '...' : 'Assign'}
                  </button>
                  <button class="ia-btn ia-cancel" onclick={() => assignOpen = null}>x</button>
                </div>
              {:else}
                <button class="assign-btn" onclick={() => { assignOpen = issue.number; assignAgent = ''; assignRole = 'triage'; }}>
                  + Assign agent
                </button>
              {/if}
            </div>
          {/each}
        {/if}

        <!-- In Progress -->
        {#if issuesStore.inProgress.length > 0}
          <div class="issues-section-title">In Progress ({issuesStore.inProgress.length})</div>
          {#each issuesStore.inProgress as issue}
            <div class="issue-card">
              <div class="issue-header">
                <span class="issue-number">#{issue.number}</span>
                <span class="issue-title">{issue.title}</span>
                <a class="issue-link" href={issue.url} target="_blank" rel="noopener">&nearr;</a>
              </div>
              {#if issue.labels.length}
                <div class="issue-labels">
                  {#each issue.labels as label}
                    <span class="label-chip">{label}</span>
                  {/each}
                </div>
              {/if}
              <div class="issue-agents">
                {#each issue.agents as a}
                  <span class="agent-role" style="color: {AGENTS[a.agent]?.color || 'var(--text-secondary)'}">
                    {a.agent} <span class="role-badge">{a.role}</span>
                  </span>
                {/each}
                {#if issue.agents.some(a => a.taskId)}
                  <span class="linked-task">&rarr; #{issue.agents.find(a => a.taskId)?.taskId}</span>
                {/if}
              </div>
              <button class="assign-btn" onclick={() => { assignOpen = issue.number; assignAgent = ''; assignRole = 'code'; }}>
                + Assign another
              </button>
            </div>
          {/each}
        {/if}

        <!-- Approval Queue -->
        {#if issuesStore.queue.length > 0}
          <div class="issues-section-title">Approval Queue ({issuesStore.queue.length})</div>
          {#each issuesStore.queue as action}
            <div class="queue-card">
              <div class="queue-header">
                <span class="queue-issue">#{action.issue_number}</span>
                <span class="queue-agent" style="color: {AGENTS[action.actor_id]?.color || 'var(--text-secondary)'}">{action.actor_id}</span>
                <span class="queue-action-type">{action.action_type}</span>
                <span class="queue-time">{timeAgoFromISO(action.created_at)}</span>
              </div>
              {#if action.preview}
                <div class="queue-preview">{action.preview}</div>
              {/if}
              <div class="queue-actions">
                <button class="qa-btn qa-approve" onclick={() => issuesStore.approveAction(action.id)}>&check; Approve</button>
                <button class="qa-btn qa-reject" onclick={() => issuesStore.rejectAction(action.id)}>&cross; Reject</button>
              </div>
            </div>
          {/each}
        {/if}
      </div>

    <!-- WORKFLOW -->
    {:else if activeTab === 'workflow'}
      <div class="panel">
        {#if !workflowStore.active}
          <div class="empty-tab">No active workflow</div>
        {:else}
          <div class="wf-header">
            <div class="wf-feature">{workflowStore.feature}</div>
            {#if workflowStore.isVeloce}
              <span class="wf-mode-badge veloce">VELOCE</span>
            {/if}
          </div>
          <div class="wf-lead">Lead: {workflowStore.leadAgent}</div>

          <!-- Phase pipeline (dynamic based on mode) -->
          <div class="wf-pipeline">
            {#each workflowStore.phases as phase, i}
              <div
                class="wf-phase"
                class:done={i < workflowStore.currentPhaseIndex}
                class:current={i === workflowStore.currentPhaseIndex}
                class:pending={i > workflowStore.currentPhaseIndex}
              >
                <span class="wf-icon">{phase.icon}</span>
                <span class="wf-label">{phase.label}</span>
              </div>
              {#if i < workflowStore.phases.length - 1}
                <span class="wf-arrow">&rarr;</span>
              {/if}
            {/each}
          </div>
          {#if workflowStore.currentPhaseIndex >= 0}
            <div class="wf-progress-bar">
              <div class="wf-progress-fill" style="width: {workflowStore.phaseProgress}%"></div>
            </div>
          {/if}

          <!-- Veloce: Chunk panel -->
          {#if workflowStore.isVeloce && workflowStore.chunks.length > 0}
            <div class="wf-chunks-section">
              <div class="wf-chunks-header">
                <span class="wf-chunks-title">Chunks</span>
                <span class="wf-chunks-count">{workflowStore.chunksDone}/{workflowStore.chunksTotal} done</span>
                <span class="wf-gate" class:open={workflowStore.gateOpen} class:waiting={!workflowStore.gateOpen}>
                  {workflowStore.gateOpen ? 'GATE OPEN' : 'WAITING'}
                </span>
              </div>
              {#each workflowStore.chunks as chunk}
                <div class="chunk-card" class:chunk-done={chunk.status === 'done'} class:chunk-cancelled={chunk.status === 'cancelled'}>
                  <div class="chunk-header">
                    <span class="chunk-id">{chunk.chunk_id || '?'}</span>
                    <span class="chunk-status chunk-status-{chunk.status}">{chunk.status}</span>
                  </div>
                  <div class="chunk-meta">
                    <span class="chunk-agent">{chunk.agent_id}</span>
                  </div>
                  {#if chunk.status === 'in_progress'}
                    <div class="chunk-progress-bar">
                      <div class="chunk-progress-fill pulse"></div>
                    </div>
                  {:else if chunk.status === 'done'}
                    <div class="chunk-progress-bar">
                      <div class="chunk-progress-fill full"></div>
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {/if}
        {/if}
      </div>

    <!-- CONTROLS -->
    {:else if activeTab === 'controls'}
      <div class="panel controls">
        <div class="ctrl-section">
          <div class="ctrl-title">Mode</div>
          <div class="ctrl-buttons">
            {#each ['neutral', 'focus', 'review', 'build'] as m}
              <button
                class:active={modeStore.mode === m}
                onclick={() => modeStore.setMode(m)}
              >{m}</button>
            {/each}
          </div>
        </div>

        <div class="ctrl-section">
          <div class="ctrl-title">STFU</div>
          <div class="ctrl-buttons">
            {#if modeStore.muted}
              <button class="success" onclick={() => modeStore.unstfu()}>Unmute</button>
              <span class="ctrl-info">{modeStore.muteRemaining}s left</span>
            {:else}
              <button class="danger" onclick={() => modeStore.stfu(5)}>STFU 5m</button>
              <button class="danger" onclick={() => modeStore.stfu(15)}>15m</button>
              <button class="danger" onclick={() => modeStore.stfu(60)}>1h</button>
            {/if}
          </div>
        </div>

        <div class="ctrl-section">
          <div class="ctrl-title">Emergency</div>
          <button class="danger" onclick={() => modeStore.stop()}>&#9632; STOP ALL</button>
        </div>
      </div>
    {/if}
  </div>
</aside>

<style>
  .intel {
    width: var(--intel-width);
    min-width: var(--intel-width);
    background: var(--bg-secondary);
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
  }

  .tabs {
    display: flex;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .tab {
    flex: 1;
    padding: 6px 4px;
    border: none;
    border-radius: 0;
    background: transparent;
    font-size: 11px;
    color: var(--text-muted);
    text-align: center;
    position: relative;
  }
  .tab:hover { color: var(--text-secondary); }
  .tab.active {
    color: var(--text-primary);
    border-bottom: 2px solid var(--accent);
  }
  .tab-badge {
    background: var(--accent);
    color: var(--bg-primary);
    font-size: 9px;
    padding: 0 4px;
    border-radius: 6px;
    margin-left: 3px;
  }

  .tab-content { flex: 1; overflow-y: auto; }
  .panel { padding: 8px; display: flex; flex-direction: column; gap: 6px; }
  .empty-tab {
    text-align: center;
    color: var(--text-muted);
    padding: 24px;
    font-size: 12px;
  }

  /* Agent cards */
  .agent-card {
    padding: 6px 8px;
    background: var(--bg-tertiary);
    border-radius: 4px;
    border: 1px solid var(--border-subtle);
  }
  .agent-header { display: flex; align-items: center; gap: 6px; }
  .health { font-size: 8px; }
  .agent-name { font-weight: 600; font-size: 12px; }
  .agent-model { margin-left: auto; font-size: 10px; color: var(--text-muted); }
  .agent-meta {
    display: flex; gap: 8px; margin-top: 3px;
    font-size: 11px; color: var(--text-secondary);
  }
  .agent-task { color: var(--info); }
  .agent-progress { color: var(--warning); }
  .agent-seen { margin-left: auto; font-size: 10px; color: var(--text-muted); }
  .load-bar {
    height: 3px; background: var(--bg-active);
    border-radius: 2px; margin-top: 4px; overflow: hidden;
  }
  .load-fill {
    height: 100%;
    border-radius: 2px; transition: width 0.5s ease;
  }

  /* Task cards */
  .task-card {
    padding: 6px 8px;
    background: var(--bg-tertiary);
    border-radius: 4px;
    border: 1px solid var(--border-subtle);
  }
  .task-header { display: flex; justify-content: space-between; }
  .task-id { font-weight: 600; font-size: 12px; color: var(--info); }
  .task-status { font-size: 10px; font-weight: 600; text-transform: uppercase; }
  .status-pending { color: var(--text-muted); }
  .status-in_progress { color: var(--warning); }
  .task-desc { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
  .task-meta { display: flex; justify-content: space-between; margin-top: 3px; font-size: 11px; }
  .task-progress { color: var(--warning); }
  .task-time { color: var(--text-muted); font-size: 10px; }
  .task-progress-bar {
    height: 3px; background: var(--bg-active);
    border-radius: 2px; margin-top: 4px; overflow: hidden;
  }
  .task-progress-fill {
    height: 100%; background: var(--warning);
    border-radius: 2px; transition: width 0.5s ease;
  }
  .task-done { opacity: 0.5; }
  .task-actions {
    display: flex;
    gap: 4px;
    margin-top: 5px;
  }
  .ta-btn {
    font-size: 10px;
    padding: 2px 6px;
    border-radius: 3px;
    border: 1px solid var(--border);
    background: transparent;
    cursor: pointer;
    color: var(--text-secondary);
  }
  .ta-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
  .ta-claim { color: var(--info); border-color: var(--info); }
  .ta-claim:hover { background: var(--info); color: var(--bg-primary); }
  .ta-done { color: var(--success); border-color: var(--success); }
  .ta-done:hover { background: var(--success); color: var(--bg-primary); }
  .ta-fail { color: var(--danger); border-color: var(--danger); }
  .ta-fail:hover { background: var(--danger); color: var(--bg-primary); }
  .ta-cancel { color: var(--text-muted); }
  .completed-toggle {
    border: none; background: transparent;
    color: var(--text-muted); font-size: 11px;
    padding: 6px 4px; cursor: pointer; text-align: left;
    width: 100%;
  }
  .completed-toggle:hover { color: var(--text-secondary); }

  /* Review cards */
  .review-card {
    padding: 6px 8px;
    background: var(--bg-tertiary);
    border-radius: 4px;
    border: 1px solid var(--border-subtle);
  }
  .review-header { display: flex; justify-content: space-between; }
  .review-id { font-weight: 600; font-size: 12px; color: var(--info); }
  .review-status { font-size: 10px; font-weight: 600; text-transform: uppercase; }
  .review-file {
    font-size: 11px; color: var(--text-secondary); margin-top: 2px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .review-meta {
    display: flex; gap: 6px; align-items: center;
    margin-top: 3px; font-size: 11px;
  }
  .review-author { font-weight: 600; }
  .review-arrow { color: var(--text-muted); font-size: 10px; }
  .review-reviewer { font-weight: 500; }
  .review-progress { color: var(--warning); }
  .review-type { color: var(--text-muted); font-size: 10px; text-transform: uppercase; }
  .review-time { color: var(--text-muted); font-size: 10px; margin-left: auto; }
  .review-closed { opacity: 0.5; }
  .review-closed:hover { opacity: 0.8; }
  .retry-btn {
    border: none; background: transparent; color: var(--warning);
    font-size: 13px; cursor: pointer; padding: 0 2px; line-height: 1;
  }
  .retry-btn:hover { color: var(--accent); }
  .review-consensus { font-size: 10px; font-weight: 600; text-transform: uppercase; }
  .consensus-approved { color: var(--success); }
  .consensus-changes_requested { color: var(--danger); }
  .consensus-timeout { color: var(--text-muted); }

  /* Review action buttons */
  .review-actions {
    display: flex;
    gap: 4px;
    margin-top: 5px;
  }
  .ra-btn {
    flex: 1;
    font-size: 10px;
    padding: 2px 0;
    border-radius: 3px;
    border: 1px solid var(--border);
    background: transparent;
    cursor: pointer;
  }
  .ra-approve { color: var(--success); border-color: var(--success); }
  .ra-approve:hover { background: var(--success); color: var(--bg-primary); }
  .ra-comment { color: var(--text-secondary); }
  .ra-comment:hover { color: var(--text-primary); background: var(--bg-hover); }
  .ra-changes { color: var(--danger); border-color: var(--danger); }
  .ra-changes:hover { background: var(--danger); color: var(--bg-primary); }

  /* Review action inline form */
  .review-action-form {
    display: flex;
    gap: 4px;
    margin-top: 5px;
    align-items: center;
  }
  .review-action-input {
    flex: 1;
    font-size: 11px;
    padding: 2px 6px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 3px;
    font-family: inherit;
  }
  .review-action-input:focus { border-color: var(--info); outline: none; }
  .ra-submit {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 3px;
    cursor: pointer;
  }
  .ra-submit:disabled { opacity: 0.4; cursor: not-allowed; }
  .ra-cancel {
    font-size: 10px;
    padding: 2px 6px;
    border: none;
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
  }
  .ra-cancel:hover { color: var(--text-primary); }

  /* Request Review form */
  .req-rev-btn {
    width: 100%;
    font-size: 11px;
    padding: 5px 0;
    border: 1px dashed var(--border);
    background: transparent;
    color: var(--text-secondary);
    border-radius: 4px;
    cursor: pointer;
  }
  .req-rev-btn:hover { border-color: var(--info); color: var(--info); }
  .req-rev-form {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 8px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border);
    border-radius: 4px;
  }
  .req-rev-title {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  .req-rev-file-row {
    display: flex;
    gap: 4px;
  }
  .req-rev-input {
    flex: 1;
    font-size: 11px;
    padding: 4px 8px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 3px;
    font-family: inherit;
    min-width: 0;
  }
  .req-rev-input:focus { border-color: var(--info); outline: none; }
  .req-rev-browse {
    font-size: 11px;
    padding: 3px 8px;
    border: 1px solid var(--border);
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    border-radius: 3px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .req-rev-browse:hover { color: var(--info); border-color: var(--info); }
  .req-rev-row {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .req-rev-label {
    font-size: 10px;
    color: var(--text-muted);
    text-transform: uppercase;
  }
  .req-rev-select {
    font-size: 11px;
    padding: 3px 6px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 3px;
    font-family: inherit;
  }
  .req-rev-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 3px;
  }
  .req-rev-tag {
    font-size: 10px;
    padding: 1px 6px;
    border: 1px solid var(--border);
    background: transparent;
    border-radius: 3px;
    cursor: pointer;
  }
  .req-rev-tag:hover { background: var(--bg-hover); }
  .req-rev-tag.selected {
    border-color: currentColor;
    background: var(--bg-active);
    font-weight: 600;
  }
  .req-rev-hint {
    font-size: 10px;
    color: var(--text-muted);
    font-style: italic;
  }
  .req-rev-actions {
    display: flex;
    gap: 4px;
  }
  .req-rev-actions button {
    flex: 1;
    font-size: 11px;
    padding: 3px 0;
  }
  .req-rev-actions button:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Issue cards */
  .issue-card {
    padding: 6px 8px;
    background: var(--bg-tertiary);
    border-radius: 4px;
    border: 1px solid var(--border-subtle);
  }
  .issue-header { display: flex; align-items: center; gap: 4px; }
  .issue-number { font-weight: 600; font-size: 12px; color: var(--info); flex-shrink: 0; }
  .issue-title {
    font-size: 12px; color: var(--text-secondary);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    flex: 1;
  }
  .issue-link {
    font-size: 10px; color: var(--text-muted);
    text-decoration: none; flex-shrink: 0;
  }
  .issue-link:hover { color: var(--accent); }
  .issue-labels { display: flex; flex-wrap: wrap; gap: 3px; margin-top: 3px; }
  .label-chip {
    font-size: 9px; padding: 1px 5px;
    background: var(--bg-active); border-radius: 8px;
    color: var(--text-secondary);
  }
  .issue-agents { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 3px; font-size: 11px; }
  .agent-role { font-weight: 600; }
  .role-badge {
    font-size: 9px; color: var(--text-muted);
    font-weight: 400; text-transform: uppercase;
  }
  .linked-task { color: var(--info); font-size: 10px; }

  /* Assign form */
  .assign-btn {
    width: 100%; font-size: 10px; padding: 3px 0;
    border: 1px dashed var(--border); background: transparent;
    color: var(--text-muted); border-radius: 3px; cursor: pointer;
    margin-top: 5px;
  }
  .assign-btn:hover { border-color: var(--info); color: var(--info); }
  .assign-form { display: flex; gap: 4px; align-items: center; margin-top: 5px; }
  .assign-select {
    font-size: 10px; padding: 2px 4px;
    background: var(--bg-primary); border: 1px solid var(--border);
    color: var(--text-primary); border-radius: 3px; font-family: inherit;
  }
  .ia-btn {
    font-size: 10px; padding: 2px 6px;
    border-radius: 3px; border: 1px solid var(--border);
    background: transparent; cursor: pointer;
  }
  .ia-confirm { color: var(--success); border-color: var(--success); }
  .ia-confirm:hover { background: var(--success); color: var(--bg-primary); }
  .ia-confirm:disabled { opacity: 0.4; cursor: not-allowed; }
  .ia-cancel { color: var(--text-muted); border: none; }

  /* Issues toolbar */
  .issues-toolbar {
    display: flex; align-items: center; gap: 6px;
  }
  .issues-refresh {
    font-size: 10px; padding: 3px 8px;
    border: 1px solid var(--border); background: transparent;
    color: var(--text-secondary); border-radius: 3px; cursor: pointer;
  }
  .issues-refresh:hover { border-color: var(--info); color: var(--info); }
  .issues-refresh:disabled { opacity: 0.4; cursor: not-allowed; }
  .issues-error { color: var(--warning); font-size: 14px; cursor: help; }
  .issues-section-title {
    font-size: 10px; font-weight: 600; color: var(--text-muted);
    text-transform: uppercase; padding: 4px 0 2px;
    border-bottom: 1px solid var(--border-subtle);
  }

  /* Queue cards */
  .queue-card {
    padding: 6px 8px;
    background: var(--bg-tertiary);
    border-radius: 4px;
    border: 1px solid var(--warning);
  }
  .queue-header { display: flex; align-items: center; gap: 6px; font-size: 11px; }
  .queue-issue { font-weight: 600; color: var(--info); }
  .queue-agent { font-weight: 600; }
  .queue-action-type {
    font-size: 9px; text-transform: uppercase;
    color: var(--text-muted);
    background: var(--bg-active); padding: 1px 5px; border-radius: 3px;
  }
  .queue-time { margin-left: auto; font-size: 10px; color: var(--text-muted); }
  .queue-preview {
    font-size: 11px; color: var(--text-secondary);
    margin-top: 4px; font-style: italic;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .queue-actions { display: flex; gap: 4px; margin-top: 5px; }
  .qa-btn {
    flex: 1; font-size: 10px; padding: 3px 0;
    border-radius: 3px; cursor: pointer;
    border: 1px solid var(--border); background: transparent;
  }
  .qa-approve { color: var(--success); border-color: var(--success); }
  .qa-approve:hover { background: var(--success); color: var(--bg-primary); }
  .qa-reject { color: var(--danger); border-color: var(--danger); }
  .qa-reject:hover { background: var(--danger); color: var(--bg-primary); }

  /* Workflow */
  .wf-feature { font-weight: 600; font-size: 13px; padding: 4px 0; }
  .wf-lead { font-size: 11px; color: var(--text-secondary); margin-bottom: 8px; }
  .wf-pipeline {
    display: flex; flex-wrap: wrap; align-items: center; gap: 3px;
    font-size: 11px;
  }
  .wf-phase {
    display: flex; flex-direction: column; align-items: center;
    padding: 4px 6px; border-radius: 4px;
    border: 1px solid var(--border-subtle);
  }
  .wf-phase.done { opacity: 0.5; border-color: var(--success); }
  .wf-phase.current {
    border-color: var(--warning);
    background: var(--bg-active);
    font-weight: 600;
  }
  .wf-phase.pending { opacity: 0.3; }
  .wf-icon { font-size: 14px; }
  .wf-label { font-size: 9px; color: var(--text-secondary); }
  .wf-arrow { color: var(--text-muted); font-size: 10px; }

  .wf-progress-bar {
    height: 3px; background: var(--bg-active);
    border-radius: 2px; margin-top: 8px; overflow: hidden;
  }
  .wf-progress-fill {
    height: 100%; background: var(--warning);
    border-radius: 2px; transition: width 1s linear;
  }

  /* Workflow header with mode badge */
  .wf-header { display: flex; align-items: center; gap: 8px; }
  .wf-mode-badge {
    font-size: 9px; font-weight: 700; text-transform: uppercase;
    padding: 1px 6px; border-radius: 3px;
  }
  .wf-mode-badge.veloce {
    background: var(--warning); color: var(--bg-primary);
    animation: veloce-pulse 2s ease-in-out infinite;
  }
  @keyframes veloce-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.7; }
  }

  /* Veloce chunks section */
  .wf-chunks-section {
    margin-top: 10px;
    display: flex; flex-direction: column; gap: 4px;
  }
  .wf-chunks-header {
    display: flex; align-items: center; gap: 6px;
    font-size: 11px; padding-bottom: 4px;
    border-bottom: 1px solid var(--border-subtle);
  }
  .wf-chunks-title { font-weight: 600; color: var(--text-primary); }
  .wf-chunks-count { color: var(--text-secondary); }
  .wf-gate {
    margin-left: auto; font-size: 9px; font-weight: 700;
    padding: 1px 5px; border-radius: 3px; text-transform: uppercase;
  }
  .wf-gate.open { background: var(--success); color: var(--bg-primary); }
  .wf-gate.waiting { background: var(--bg-active); color: var(--warning); }

  .chunk-card {
    padding: 5px 8px;
    background: var(--bg-tertiary);
    border-radius: 4px;
    border: 1px solid var(--border-subtle);
  }
  .chunk-done { opacity: 0.5; }
  .chunk-cancelled { opacity: 0.3; }
  .chunk-header { display: flex; justify-content: space-between; align-items: center; }
  .chunk-id { font-weight: 600; font-size: 11px; color: var(--text-primary); }
  .chunk-status { font-size: 9px; font-weight: 600; text-transform: uppercase; }
  .chunk-status-pending { color: var(--text-muted); }
  .chunk-status-in_progress { color: var(--warning); }
  .chunk-status-done { color: var(--success); }
  .chunk-status-cancelled { color: var(--danger); }
  .chunk-meta { font-size: 10px; color: var(--text-secondary); margin-top: 2px; }
  .chunk-agent { font-weight: 500; }

  .chunk-progress-bar {
    height: 3px; background: var(--bg-active);
    border-radius: 2px; margin-top: 4px; overflow: hidden;
  }
  .chunk-progress-fill {
    height: 100%; border-radius: 2px;
    transition: width 0.5s ease;
  }
  .chunk-progress-fill.pulse {
    width: 60%; background: var(--warning);
    animation: chunk-pulse 1.5s ease-in-out infinite;
  }
  .chunk-progress-fill.full {
    width: 100%; background: var(--success);
  }
  @keyframes chunk-pulse {
    0%, 100% { width: 40%; opacity: 0.7; }
    50% { width: 80%; opacity: 1; }
  }

  /* Controls */
  .controls { gap: 12px; }
  .ctrl-section { display: flex; flex-direction: column; gap: 4px; }
  .ctrl-title { font-size: 10px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; }
  .ctrl-buttons { display: flex; gap: 4px; flex-wrap: wrap; }
  .ctrl-info { font-size: 11px; color: var(--text-muted); display: flex; align-items: center; }
</style>
