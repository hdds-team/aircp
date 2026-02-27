<script>
  let { onSelect, onCancel, startPath = '/projects/aircp' } = $props();

  let currentPath = $state(startPath);
  let entries = $state([]);
  let loading = $state(false);
  let error = $state('');

  async function browse(path) {
    loading = true;
    error = '';
    try {
      const res = await fetch(`/api/files?path=${encodeURIComponent(path)}`);
      const data = await res.json();
      if (data.error) {
        error = data.error;
        return;
      }
      if (data.type === 'file') {
        onSelect(data.path);
        return;
      }
      currentPath = data.path;
      entries = data.entries || [];
    } catch (e) {
      error = 'Failed to load';
    } finally {
      loading = false;
    }
  }

  function goUp() {
    const parent = currentPath.replace(/\/[^/]+\/?$/, '') || '/projects';
    browse(parent);
  }

  function pick(entry) {
    if (entry.type === 'dir') {
      browse(entry.path);
    } else {
      onSelect(entry.path);
    }
  }

  // Display path relative to /projects/ for readability
  let displayPath = $derived(
    currentPath.replace(/^\/projects\//, '')
  );

  // Load on mount
  $effect(() => {
    browse(startPath);
  });
</script>

<div class="fp">
  <div class="fp-header">
    <button class="fp-up" onclick={goUp} disabled={currentPath === '/projects'}>..</button>
    <span class="fp-path" title={currentPath}>{displayPath}/</span>
    <button class="fp-close" onclick={onCancel}>x</button>
  </div>

  {#if error}
    <div class="fp-error">{error}</div>
  {/if}

  <div class="fp-list">
    {#if loading}
      <div class="fp-loading">...</div>
    {:else}
      {#each entries as entry}
        <button class="fp-entry" class:fp-dir={entry.type === 'dir'} onclick={() => pick(entry)}>
          <span class="fp-icon">{entry.type === 'dir' ? '/' : ' '}</span>
          <span class="fp-name">{entry.name}</span>
        </button>
      {/each}
      {#if !entries.length && !error}
        <div class="fp-empty">Empty directory</div>
      {/if}
    {/if}
  </div>
</div>

<style>
  .fp {
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
    max-height: 250px;
    display: flex;
    flex-direction: column;
  }

  .fp-header {
    display: flex;
    align-items: center;
    gap: 4px;
    padding: 4px 6px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }

  .fp-up {
    font-size: 11px;
    padding: 1px 6px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-secondary);
    border-radius: 3px;
    cursor: pointer;
  }
  .fp-up:hover { color: var(--text-primary); }
  .fp-up:disabled { opacity: 0.3; cursor: not-allowed; }

  .fp-path {
    flex: 1;
    font-size: 10px;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .fp-close {
    font-size: 10px;
    padding: 1px 4px;
    border: none;
    background: transparent;
    color: var(--text-muted);
    cursor: pointer;
  }
  .fp-close:hover { color: var(--text-primary); }

  .fp-list {
    overflow-y: auto;
    flex: 1;
  }

  .fp-entry {
    display: flex;
    align-items: center;
    gap: 4px;
    width: 100%;
    padding: 3px 8px;
    border: none;
    border-radius: 0;
    background: transparent;
    color: var(--text-primary);
    font-size: 11px;
    cursor: pointer;
    text-align: left;
  }
  .fp-entry:hover { background: var(--bg-hover); }
  .fp-dir { color: var(--info); }
  .fp-dir .fp-name { font-weight: 600; }

  .fp-icon {
    font-size: 10px;
    color: var(--text-muted);
    width: 10px;
    text-align: center;
    flex-shrink: 0;
  }

  .fp-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .fp-error {
    padding: 6px 8px;
    font-size: 11px;
    color: var(--danger);
  }

  .fp-loading, .fp-empty {
    padding: 12px;
    text-align: center;
    font-size: 11px;
    color: var(--text-muted);
  }
</style>
