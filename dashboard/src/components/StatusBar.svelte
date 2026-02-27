<script>
  import { connectionStore } from '../stores/connection.svelte.js';
  import { agentsStore } from '../stores/agents.svelte.js';
  import { tasksStore } from '../stores/tasks.svelte.js';
  import { workflowStore } from '../stores/workflow.svelte.js';
  import { modeStore } from '../stores/mode.svelte.js';
  import { settingsStore } from '../stores/settings.svelte.js';
  import { SYSTEM_BOTS } from '../lib/topics.js';

  let now = $state(Date.now());
  let _timer;
  $effect(() => {
    _timer = setInterval(() => { now = Date.now(); }, 1000);
    return () => clearInterval(_timer);
  });

  let uptime = $derived.by(() => {
    const start = connectionStore.uptimeStart;
    if (!start) return '';
    const diff = Math.max(0, Math.floor((now - start) / 1000));
    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  });

  // Filter out system bots, only show real agents
  let visibleAgents = $derived(
    agentsStore.agentList.filter(a => !SYSTEM_BOTS.has(a.id))
  );
</script>

<div class="statusbar">
  <span class="item">
    {#if connectionStore.connected}
      <span class="dot ok">{'\u25CF'}</span> DDS domain {connectionStore.domain ?? '?'}
    {:else}
      <span class="dot err">{'\u25CF'}</span> {connectionStore.state}
    {/if}
  </span>

  {#if uptime}
    <span class="sep">{'\u2502'}</span>
    <span class="item uptime">{'\u2191'} {uptime}</span>
  {/if}

  <span class="sep">{'\u2502'}</span>

  <span class="item agents-bar">
    {#each visibleAgents as agent (agent.id)}
      <span
        class="agent-dot"
        title="{agent.id} ({agent.role}) - {agent.health}{agent.currentTask ? ' | Task: ' + agent.currentTask : ''}"
      >
        <span
          class="dot health-{agent.health}"
          style="color: {agent.color}"
        >{agentsStore.getHealthIcon(agent.health)}</span>
        <span class="agent-name" style="color: {agent.health === 'dead' ? 'var(--text-muted)' : agent.color}">
          {agent.id.replace('@', '')}
        </span>
      </span>
    {/each}
  </span>

  <span class="sep">{'\u2502'}</span>

  <span class="item">
    {'\uD83D\uDCCB'} {tasksStore.activeTasks.length} tasks
  </span>

  {#if workflowStore.active}
    <span class="sep">{'\u2502'}</span>
    <span class="item wf">
      {'\u2699'} {workflowStore.currentPhase || '?'}
      {#if workflowStore.feature}
        {'\u2014'} {workflowStore.feature}
      {/if}
    </span>
  {/if}

  <span class="spacer"></span>

  {#if connectionStore.connected}
    <span class="item dds-live"><span class="dot ok">{'\u25CF'}</span> DDS LIVE</span>
  {/if}

  <span class="item mode mode-{modeStore.mode}">
    {modeStore.mode}
  </span>

  <span class="sep">|</span>
  <button class="op-btn" onclick={() => settingsStore.logout()}>
    <span style="color: {settingsStore.operatorColor}">{settingsStore.operatorId}</span>
  </button>

  <span class="item version">v3.0-hdds</span>
</div>

<style>
  .statusbar {
    display: flex;
    align-items: center;
    height: var(--statusbar-height);
    padding: 0 12px;
    background: var(--bg-tertiary);
    border-top: 1px solid var(--border);
    font-size: 11px;
    color: var(--text-muted);
    gap: 8px;
    flex-shrink: 0;
  }

  .item { display: flex; align-items: center; gap: 4px; }
  .sep { color: var(--border); }
  .spacer { flex: 1; }

  .dot { font-size: 8px; }
  .dot.ok { color: var(--success); }
  .dot.err { color: var(--danger); }

  .uptime { color: var(--text-secondary); }
  .wf { color: var(--warning); }
  .mode { font-weight: 600; text-transform: uppercase; }
  .version { font-size: 10px; opacity: 0.5; }
  .dds-live { color: var(--success); font-weight: 600; font-size: 10px; }
  .op-btn {
    background: none;
    border: none;
    font-size: 11px;
    font-weight: 600;
    padding: 0 4px;
    cursor: pointer;
    color: var(--text-secondary);
  }
  .op-btn:hover { opacity: 0.7; }

  /* Agent dots in statusbar */
  .agents-bar {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .agent-dot {
    display: flex;
    align-items: center;
    gap: 2px;
    cursor: default;
  }

  .agent-dot .dot {
    font-size: 9px;
    line-height: 1;
  }

  .agent-name {
    font-size: 10px;
    font-weight: 500;
    opacity: 0.9;
  }

  /* Health-based dot styling */
  .dot.health-online { opacity: 1; }
  .dot.health-away { opacity: 0.6; }
  .dot.health-dead { opacity: 0.3; color: var(--text-muted) !important; }
</style>
