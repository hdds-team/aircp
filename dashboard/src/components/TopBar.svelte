<script>
  import { connectionStore } from '../stores/connection.svelte.js';
  import { modeStore } from '../stores/mode.svelte.js';
  import { messagesStore } from '../stores/messages.svelte.js';
  import { projectStore } from '../stores/project.svelte.js';
  import { tasksStore } from '../stores/tasks.svelte.js';
  import { reviewsStore } from '../stores/reviews.svelte.js';
  import { AGENTS } from '../lib/topics.js';
  import { settingsStore } from '../stores/settings.svelte.js';

  let { intelOpen, toggleIntel } = $props();

  const stateColors = {
    connected: 'var(--success)',
    connecting: 'var(--warning)',
    disconnected: 'var(--danger)',
  };

  let taskFormOpen = $state(false);
  let taskDesc = $state('');
  let taskAgent = $state('');

  let projectFormOpen = $state(false);
  let projectId = $state('');
  let projectName = $state('');

  let ideaFormOpen = $state(false);
  let ideaText = $state('');
  let workflowMode = $state(localStorage.getItem('aircp_wf_mode') || 'veloce');

  // Notifications state
  let notificationsEnabled = $state(
    localStorage.getItem('aircp_notif') !== 'off'
  );

  const agentIds = Object.keys(AGENTS).filter(id => id !== settingsStore.operatorId);

  async function createTask() {
    const desc = taskDesc.trim();
    if (!desc) return;
    try {
      const body = { description: desc, agent_id: taskAgent || '@alpha' };
      if (projectStore.activeProject) body.project_id = projectStore.activeProject;
      await fetch('/api/task', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      taskDesc = '';
      taskAgent = '';
      taskFormOpen = false;
    } catch (e) {
      console.warn('[topbar] Failed to create task:', e);
    }
  }

  function cancelTask() {
    taskFormOpen = false;
    taskDesc = '';
    taskAgent = '';
  }

  async function submitIdea() {
    const idea = ideaText.trim();
    if (!idea || idea.length < 5) return;
    try {
      await fetch('/api/idea', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idea, created_by: settingsStore.operatorId, mode: workflowMode }),
      });
      ideaText = '';
      ideaFormOpen = false;
    } catch (e) {
      console.warn('[topbar] Failed to submit idea:', e);
    }
  }

  function cancelIdea() {
    ideaFormOpen = false;
    ideaText = '';
  }

  async function createProject() {
    const id = projectId.trim().toLowerCase().replace(/\s+/g, '-');
    if (!id) return;
    try {
      const res = await fetch('/api/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, name: projectName.trim() || id }),
      });
      if (res.ok) {
        projectId = '';
        projectName = '';
        projectFormOpen = false;
        await projectStore.fetchProjects();
        projectStore.switchProject(id);
        tasksStore.init();
        reviewsStore.init();
        messagesStore.refetchHistory();
      }
    } catch (e) {
      console.warn('[topbar] Failed to create project:', e);
    }
  }

  function cancelProject() {
    projectFormOpen = false;
    projectId = '';
    projectName = '';
  }

  function onProjectChange(e) {
    const val = e.target.value || null;
    projectStore.switchProject(val);
    // Re-fetch scoped stores
    tasksStore.init();
    reviewsStore.init();
    messagesStore.refetchHistory();
  }

  function toggleNotifications() {
    notificationsEnabled = !notificationsEnabled;
    localStorage.setItem('aircp_notif', notificationsEnabled ? 'on' : 'off');
    messagesStore.setNotifications(notificationsEnabled);

    // Request permission on first enable
    if (notificationsEnabled && 'Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission();
    }
  }
</script>

<div class="topbar">
  <div class="left">
    <span class="logo">aIRCp</span>
    <span class="tagline">Réseau d'agents IA souverains sur HDDS</span>
  </div>

  <div class="center">
    {#if projectFormOpen}
      <div class="project-form">
        <input
          class="project-input"
          type="text"
          placeholder="project-id"
          bind:value={projectId}
          onkeydown={(e) => e.key === 'Enter' && createProject()}
          onkeyup={(e) => e.key === 'Escape' && cancelProject()}
        />
        <input
          class="project-input project-name"
          type="text"
          placeholder="Display name"
          bind:value={projectName}
          onkeydown={(e) => e.key === 'Enter' && createProject()}
          onkeyup={(e) => e.key === 'Escape' && cancelProject()}
        />
        <button class="success" onclick={createProject}>OK</button>
        <button onclick={cancelProject}>✕</button>
      </div>
    {:else}
      <select class="project-select" onchange={onProjectChange} value={projectStore.activeProject || ''}>
        <option value="">All projects</option>
        {#each projectStore.projects as p}
          <option value={p.id}>{p.name || p.id}</option>
        {/each}
      </select>
      <button class="action-btn project-add-btn" onclick={() => projectFormOpen = true}>+</button>
    {/if}

    <span class="conn-dot" style="color: {stateColors[connectionStore.state]}">●</span>
    <span class="conn-label">{connectionStore.state}</span>

    {#if modeStore.mode !== 'neutral'}
      <span class="mode-badge mode-{modeStore.mode}">
        {modeStore.mode.toUpperCase()}
        {#if modeStore.lead}
          → {modeStore.lead}
        {/if}
      </span>
    {/if}

    {#if modeStore.muted}
      <span class="mute-badge">🔇 STFU</span>
    {/if}
  </div>

  <div class="actions">
    {#if taskFormOpen}
      <div class="task-form">
        <input
          class="task-input"
          type="text"
          placeholder="Task description..."
          bind:value={taskDesc}
          onkeydown={(e) => e.key === 'Enter' && createTask()}
        />
        <select class="task-select" bind:value={taskAgent}>
          <option value="">auto</option>
          {#each agentIds as id}
            <option value={id}>{id}</option>
          {/each}
        </select>
        <button class="success" onclick={createTask}>OK</button>
        <button onclick={cancelTask}>✕</button>
      </div>
    {:else}
      <button class="action-btn" onclick={() => taskFormOpen = true}>+ Task</button>
    {/if}

    <select
      class="mode-select"
      bind:value={workflowMode}
      onchange={() => localStorage.setItem('aircp_wf_mode', workflowMode)}
      title="Workflow mode for new ideas"
    >
      <option value="veloce">Veloce</option>
      <option value="standard">Standard</option>
    </select>

    {#if ideaFormOpen}
      <div class="idea-form">
        <input
          class="idea-input"
          type="text"
          placeholder="Votre idee..."
          bind:value={ideaText}
          onkeydown={(e) => e.key === 'Enter' && submitIdea()}
          onkeyup={(e) => e.key === 'Escape' && cancelIdea()}
        />
        <button class="success" onclick={submitIdea}>GO</button>
        <button onclick={cancelIdea}>✕</button>
      </div>
    {:else}
      <button class="action-btn idea-btn" onclick={() => ideaFormOpen = true}>Idea</button>
    {/if}

    <button
      class="action-btn notif-btn"
      class:notif-on={notificationsEnabled}
      onclick={toggleNotifications}
      title="Browser notifications on @mention"
    >
      🔔 {notificationsEnabled ? 'On' : 'Off'}
    </button>
  </div>

  <div class="right">
    <button class="intel-toggle" class:active={intelOpen} onclick={toggleIntel}>
      {intelOpen ? '◂ Intel' : 'Intel ▸'}
    </button>
    <button class="stop-btn danger" onclick={() => modeStore.stop()}>■ STOP</button>
  </div>
</div>

<style>
  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: var(--topbar-height);
    padding: 0 12px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    gap: 16px;
    flex-shrink: 0;
  }

  .left { display: flex; align-items: center; gap: 10px; }
  .logo {
    font-size: 15px;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: -0.5px;
  }
  .tagline {
    font-size: 10px;
    color: var(--text-muted);
    display: none;
  }
  @media (min-width: 900px) { .tagline { display: inline; } }

  .center { display: flex; align-items: center; gap: 10px; }
  .project-select {
    font-size: 11px;
    padding: 2px 6px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--accent);
    border-radius: 3px;
    font-weight: 600;
  }
  .project-form { display: flex; align-items: center; gap: 4px; }
  .project-input {
    width: 110px;
    font-size: 11px;
    padding: 2px 6px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 3px;
  }
  .project-name { width: 130px; }
  .project-add-btn {
    padding: 2px 6px;
    font-size: 11px;
    color: var(--accent);
    border-color: var(--accent);
  }
  .project-add-btn:hover { background: var(--accent); color: var(--bg-primary); }
  .conn-dot { font-size: 10px; }
  .conn-label { font-size: 11px; color: var(--text-secondary); }

  .mode-badge {
    font-size: 11px;
    font-weight: 600;
    padding: 1px 8px;
    border-radius: 3px;
    border: 1px solid currentColor;
  }
  .mute-badge {
    font-size: 11px;
    color: var(--danger);
    border: 1px solid var(--danger);
    padding: 1px 8px;
    border-radius: 3px;
  }

  .actions { display: flex; align-items: center; gap: 6px; }
  .action-btn {
    font-size: 11px;
    padding: 2px 10px;
    border: 1px solid var(--border);
    background: var(--bg-tertiary);
    color: var(--text-secondary);
    border-radius: 3px;
    cursor: pointer;
  }
  .action-btn:hover { background: var(--bg-hover); color: var(--text-primary); }
  .task-form {
    display: flex; align-items: center; gap: 4px;
  }
  .task-input {
    width: 180px;
    font-size: 11px;
    padding: 2px 6px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 3px;
  }
  .task-select {
    font-size: 11px;
    padding: 2px 4px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 3px;
  }

  .mode-select {
    font-size: 11px;
    padding: 2px 4px;
    background: var(--bg-primary);
    border: 1px solid var(--warning);
    color: var(--warning);
    border-radius: 3px;
    font-weight: 600;
    cursor: pointer;
  }
  .mode-select:hover { background: var(--bg-hover); }
  .mode-select option { color: var(--text-primary); background: var(--bg-primary); }

  .idea-form { display: flex; align-items: center; gap: 4px; }
  .idea-input {
    width: 220px;
    font-size: 11px;
    padding: 2px 6px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 3px;
  }
  .idea-btn { color: var(--warning); border-color: var(--warning); }
  .idea-btn:hover { background: var(--warning); color: var(--bg-primary); }

  .notif-btn { color: var(--text-muted); }
  .notif-btn.notif-on { color: var(--success); border-color: var(--success); }
  .notif-btn:hover { color: var(--text-primary); }

  .right { display: flex; align-items: center; gap: 6px; }
  .stop-btn { background: transparent; }
  .stop-btn:hover { background: var(--accent-dim); }
</style>
