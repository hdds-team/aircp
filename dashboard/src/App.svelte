<script>
  import { onMount, onDestroy } from 'svelte';
  import './app.css';
  import { settingsStore } from './stores/settings.svelte.js';
  import { connectionStore } from './stores/connection.svelte.js';
  import { messagesStore } from './stores/messages.svelte.js';
  import { agentsStore } from './stores/agents.svelte.js';
  import { projectStore } from './stores/project.svelte.js';
  import { tasksStore } from './stores/tasks.svelte.js';
  import { reviewsStore } from './stores/reviews.svelte.js';
  import { issuesStore } from './stores/issues.svelte.js';
  import { modeStore } from './stores/mode.svelte.js';
  import { workflowStore } from './stores/workflow.svelte.js';
  import Login from './components/Login.svelte';
  import TopBar from './components/TopBar.svelte';
  import Sidebar from './components/Sidebar.svelte';
  import Chat from './components/Chat.svelte';
  import IntelPanel from './components/IntelPanel.svelte';
  import StatusBar from './components/StatusBar.svelte';

  let intelOpen = $state(true);
  let storesReady = $state(false);

  function toggleIntel() { intelOpen = !intelOpen; }

  async function initStores() {
    connectionStore.init();
    await projectStore.init();
    await Promise.all([
      messagesStore.init(),
      agentsStore.init(),
      tasksStore.init(),
      reviewsStore.init(),
      issuesStore.init(),
      modeStore.init(),
    ]);
    workflowStore.init();
    storesReady = true;
    console.log(`[app] Stores initialized (operator: ${settingsStore.operatorId})`);
  }

  // Init stores on mount if already logged in, or when login happens
  $effect(() => {
    if (settingsStore.isLoggedIn && !storesReady) {
      initStores();
    }
  });

  onDestroy(() => {
    messagesStore.cleanup();
    agentsStore.cleanup();
    tasksStore.cleanup();
    reviewsStore.cleanup();
    issuesStore.cleanup();
    modeStore.cleanup();
    workflowStore.cleanup();
    connectionStore.disconnect();
  });
</script>

{#if !settingsStore.isLoggedIn}
  <Login />
{:else}
  <div class="layout">
    <TopBar {intelOpen} {toggleIntel} />

    <div class="main">
      <Sidebar />
      <Chat />
      {#if intelOpen}
        <IntelPanel />
      {/if}
    </div>

    <StatusBar />
  </div>
{/if}

<style>
  .layout {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
  }

  .main {
    display: flex;
    flex: 1;
    min-height: 0;
  }
</style>
