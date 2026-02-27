<script>
  import { messagesStore } from '../stores/messages.svelte.js';
  import { agentsStore } from '../stores/agents.svelte.js';
  import { settingsStore } from '../stores/settings.svelte.js';
  import { DEFAULT_ROOMS } from '../lib/topics.js';
</script>

<aside class="sidebar">
  <!-- Channels -->
  <div class="section">
    <div class="section-title">Channels</div>
    {#each DEFAULT_ROOMS as room}
      <button
        class="channel"
        class:active={messagesStore.activeRoom === room}
        onclick={() => messagesStore.switchRoom(room)}
      >
        <span class="channel-name">{room}</span>
        {#if messagesStore.unreadCounts[room] > 0}
          <span class="badge">{messagesStore.unreadCounts[room]}</span>
        {/if}
      </button>
    {/each}
  </div>

  <!-- Nicklist -->
  <div class="section nicks">
    <div class="section-title">
      Agents
      <span class="count">{agentsStore.onlineCount}/{agentsStore.totalCount}</span>
    </div>
    {#each agentsStore.agentList as agent}
      <button
        class="nick"
        class:is-operator={agent.id === settingsStore.operatorId}
        style="--agent-color: {agent.color}"
        title="{agent.id} — {agent.model}\n{agent.role}\nHealth: {agent.health}\nActivity: {agent.activity}"
        onclick={() => agent.id !== settingsStore.operatorId && messagesStore.appendToInput(`${agent.id} `)}
      >
        <span class="health health-{agent.health}">
          {agentsStore.getHealthIcon(agent.health)}
        </span>
        <span class="nick-name">{agent.id}</span>
        <span class="nick-activity">{agentsStore.getActivityIcon(agent.activity)}</span>
      </button>
    {/each}
  </div>
</aside>

<style>
  .sidebar {
    width: var(--sidebar-width);
    min-width: var(--sidebar-width);
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow-y: auto;
    flex-shrink: 0;
  }

  .section { padding: 8px 0; }
  .section-title {
    display: flex;
    justify-content: space-between;
    font-size: 10px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 4px 12px;
  }
  .count { color: var(--text-muted); font-weight: 400; }

  .channel {
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: 100%;
    padding: 4px 12px;
    border: none;
    border-radius: 0;
    background: transparent;
    color: var(--text-secondary);
    text-align: left;
    font-size: 13px;
  }
  .channel:hover { background: var(--bg-hover); color: var(--text-primary); }
  .channel.active {
    background: var(--bg-active);
    color: var(--text-primary);
    font-weight: 600;
  }
  .badge {
    background: var(--accent);
    color: var(--bg-primary);
    font-size: 10px;
    font-weight: 700;
    padding: 0 5px;
    border-radius: 8px;
    min-width: 16px;
    text-align: center;
  }

  .nicks { flex: 1; }
  .nick {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 12px;
    font-size: 12px;
    color: var(--text-secondary);
    width: 100%;
    border: none;
    border-radius: 0;
    background: transparent;
    text-align: left;
    cursor: pointer;
  }
  .nick:hover { background: var(--bg-hover); }
  .nick.is-operator { cursor: default; opacity: 0.7; }
  .nick.is-operator:hover { background: transparent; }
  .nick-name { color: var(--agent-color, var(--text-primary)); }
  .nick-activity { margin-left: auto; font-size: 11px; color: var(--text-muted); }
  .health { font-size: 8px; }
</style>
