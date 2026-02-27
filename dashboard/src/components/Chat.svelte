<script>
  import { messagesStore } from '../stores/messages.svelte.js';
  import { SYSTEM_BOTS } from '../lib/topics.js';
  import { settingsStore } from '../stores/settings.svelte.js';
  import DropZone from './DropZone.svelte';

  function openFilePicker() {
    document.getElementById('file-upload-input')?.click();
  }

  let inputText = $state('');
  let chatContainer;
  let inputEl;
  let searchOpen = $state(false);
  let searchQuery = $state('');
  let searchInputEl;

  // Auto-scroll to bottom on new messages
  $effect(() => {
    const _ = messagesStore.roomMessages.length;
    if (chatContainer) {
      requestAnimationFrame(() => {
        chatContainer.scrollTop = chatContainer.scrollHeight;
      });
    }
  });

  // Listen for appendToInput requests (click-to-mention from Sidebar)
  $effect(() => {
    const text = messagesStore.pendingInsert;
    if (text) {
      inputText += text;
      messagesStore.clearPendingInsert();
      inputEl?.focus();
    }
  });

  // Ctrl+F → open search
  $effect(() => {
    function onGlobalKeydown(e) {
      if (e.ctrlKey && e.key === 'f') {
        e.preventDefault();
        searchOpen = true;
        requestAnimationFrame(() => searchInputEl?.focus());
      }
    }
    document.addEventListener('keydown', onGlobalKeydown);
    return () => document.removeEventListener('keydown', onGlobalKeydown);
  });

  function closeSearch() {
    searchOpen = false;
    searchQuery = '';
  }

  function handleKeydown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
    // Shift+Enter: default textarea behavior (newline)
  }

  function autoResize(el) {
    if (!el) return;
    el.style.height = 'auto';
    const maxH = 120; // ~6 lines max
    el.style.height = Math.min(el.scrollHeight, maxH) + 'px';
    el.style.overflowY = el.scrollHeight > maxH ? 'auto' : 'hidden';
  }

  function handleInput(e) {
    autoResize(e.target);
  }

  function send() {
    const text = inputText.trim();
    if (!text) return;
    messagesStore.sendMessage(text);
    inputText = '';
    // Reset textarea height after send
    if (inputEl) {
      inputEl.style.height = 'auto';
    }
  }

  function formatTime(date) {
    if (!date) return '';
    return date.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  }

  function isSystemMsg(msg) {
    return SYSTEM_BOTS.has(msg.from) || msg.kind === 'CONTROL' || msg.kind === 'EVENT';
  }

  function formatDateSep(date) {
    const now = new Date();
    const d = new Date(date);
    const diffDays = Math.floor((now - d) / 86400000);
    if (diffDays === 0 && now.getDate() === d.getDate()) return 'Aujourd\'hui';
    if (diffDays <= 1 && now.getDate() - d.getDate() === 1) return 'Hier';
    return d.toLocaleDateString('fr-FR', { weekday: 'long', day: 'numeric', month: 'long' });
  }

  /**
   * Build grouped message list with date separators.
   * Returns array of: { type: 'date', label } | { type: 'msg', msg, isFirst, isSystem }
   */
  let groupedMessages = $derived.by(() => {
    const msgs = messagesStore.roomMessages;
    const result = [];
    let lastDate = '';
    let lastFrom = '';
    let lastTime = 0;

    for (const msg of msgs) {
      // Date separator
      const dateKey = msg.timestamp ? msg.timestamp.toDateString() : '';
      if (dateKey && dateKey !== lastDate) {
        result.push({ type: 'date', label: formatDateSep(msg.timestamp) });
        lastDate = dateKey;
        lastFrom = '';
        lastTime = 0;
      }

      const sys = isSystemMsg(msg);
      const elapsed = msg.timestamp ? (msg.timestamp.getTime() - lastTime) : Infinity;
      const sameGroup = !sys && msg.from === lastFrom && elapsed < 60000;

      result.push({ type: 'msg', msg, isFirst: !sameGroup, isSystem: sys });

      lastFrom = sys ? '' : msg.from;
      lastTime = msg.timestamp ? msg.timestamp.getTime() : 0;
    }
    return result;
  });

  /** Filtered messages when search is active */
  let displayMessages = $derived.by(() => {
    if (!searchQuery.trim()) return groupedMessages;
    const q = searchQuery.toLowerCase();
    return groupedMessages.filter(entry => {
      if (entry.type === 'date') return false; // hide date seps during search
      const msg = entry.msg;
      return (msg.content && msg.content.toLowerCase().includes(q)) ||
             (msg.from && msg.from.toLowerCase().includes(q));
    });
  });

  let searchMatchCount = $derived(
    searchQuery.trim() ? displayMessages.filter(e => e.type === 'msg').length : 0
  );

  function highlightSearch(html) {
    if (!searchQuery.trim()) return html;
    const q = searchQuery.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return html.replace(new RegExp(`(${q})`, 'gi'), '<mark>$1</mark>');
  }
</script>

<div class="chat">
  <div class="chat-header">
    <span class="room-name">{messagesStore.activeRoom}</span>
    <span class="msg-count">{messagesStore.roomMessages.length} msgs</span>
  </div>

  {#if searchOpen}
    <div class="search-bar">
      <span class="search-icon">🔍</span>
      <input
        bind:this={searchInputEl}
        type="text"
        placeholder="Search messages... (Escape to close)"
        bind:value={searchQuery}
        onkeydown={(e) => e.key === 'Escape' && closeSearch()}
      />
      {#if searchQuery.trim()}
        <span class="search-count">{searchMatchCount} match{searchMatchCount !== 1 ? 'es' : ''}</span>
      {/if}
      <button class="search-close" onclick={closeSearch}>✕</button>
    </div>
  {/if}

  <DropZone>
    <div class="chat-messages" bind:this={chatContainer}>
      {#each displayMessages as entry (entry.type === 'date' ? entry.label : entry.msg.id)}
        {#if entry.type === 'date'}
          <div class="date-sep"><span>{entry.label}</span></div>
        {:else if entry.isFirst}
          <div class="msg" class:system={entry.isSystem}>
            <span class="msg-time">{formatTime(entry.msg.timestamp)}</span>
            <span
              class="msg-nick"
              style="color: {messagesStore.getAgentColor(entry.msg.from)}"
            >&lt;{entry.msg.from}&gt;</span>
            <span class="msg-text">{@html highlightSearch(formatContent(entry.msg.content))}</span>
          </div>
        {:else}
          <div class="msg msg-cont">
            <span class="msg-text">{@html highlightSearch(formatContent(entry.msg.content))}</span>
          </div>
        {/if}
      {:else}
        <div class="empty">
          <span class="empty-icon">◇</span>
          {#if searchQuery.trim()}
            <span>No results for "{searchQuery}"</span>
          {:else}
            <span>En attente de messages sur {messagesStore.activeRoom}...</span>
            <span class="empty-hint">Connecté à hdds-ws, subscribe sur aircp/{messagesStore.activeRoom.replace('#', '')}</span>
          {/if}
        </div>
      {/each}
    </div>
  </DropZone>

  <div class="chat-input">
    <span class="input-nick" style="color: {settingsStore.operatorColor}">{settingsStore.operatorId}</span>
    <button class="attach-btn" onclick={openFilePicker} title="Attach file">+</button>
    <textarea
      bind:this={inputEl}
      placeholder="Message {messagesStore.activeRoom}... (Shift+Enter pour nouvelle ligne)"
      bind:value={inputText}
      onkeydown={handleKeydown}
      oninput={handleInput}
      rows="1"
    ></textarea>
    <button onclick={send}>Send</button>
  </div>
</div>

<script module>
  function _formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function _escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function formatContent(text) {
    if (!text) return '';

    // 0. [FILE:url|mime|name|size] → rich preview (before HTML escape)
    // All captured values are escaped to prevent XSS via crafted [FILE:...] tokens
    const fileBlocks = [];
    text = text.replace(/\[FILE:(\/uploads\/[^|]+)\|([^|]+)\|([^|]+)\|(\d+)\]/g,
      (_, url, mime, name, size) => {
        const safeUrl = _escHtml(url);
        const safeMime = _escHtml(mime);
        const safeName = _escHtml(name);
        const sizeStr = _formatFileSize(parseInt(size));
        let html;
        if (mime.startsWith('image/')) {
          html = `<div class="file-preview file-image">` +
            `<a href="${safeUrl}" target="_blank" rel="noopener">` +
            `<img src="${safeUrl}" alt="${safeName}" loading="lazy" />` +
            `</a>` +
            `<span class="file-meta">${safeName} (${sizeStr})</span>` +
            `</div>`;
        } else {
          const icon = safeMime === 'application/pdf' ? 'PDF' : 'TXT';
          html = `<div class="file-preview file-doc">` +
            `<a href="${safeUrl}" target="_blank" rel="noopener" class="file-link">` +
            `<span class="file-icon">${icon}</span>` +
            `<span class="file-name">${safeName}</span>` +
            `<span class="file-size">${sizeStr}</span>` +
            `</a>` +
            `</div>`;
        }
        fileBlocks.push(html);
        return `%%FILEBLOCK_${fileBlocks.length - 1}%%`;
      });

    // 1. Escape HTML
    let safe = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    // 2. Code blocks (``` ... ```) — extract before inline transforms
    const codeBlocks = [];
    safe = safe.replace(/```(?:\w*)\n?([\s\S]*?)```/g, (_, code) => {
      codeBlocks.push(code.replace(/\n$/, ''));
      return `%%CODEBLOCK_${codeBlocks.length - 1}%%`;
    });

    // 3. Inline code (must come before bold/italic to avoid conflicts)
    const inlineCodes = [];
    safe = safe.replace(/`([^`]+)`/g, (_, code) => {
      inlineCodes.push(code);
      return `%%INLINE_${inlineCodes.length - 1}%%`;
    });

    // 4. Bold **text** or __text__
    safe = safe.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/__(.+?)__/g, '<strong>$1</strong>');

    // 5. Italic *text* or _text_ (not inside words for _)
    safe = safe.replace(/(?<!\w)\*(.+?)\*(?!\w)/g, '<em>$1</em>');
    safe = safe.replace(/(?<!\w)_(.+?)_(?!\w)/g, '<em>$1</em>');

    // 6. @mentions
    safe = safe.replace(/@([\w-]+)/g, '<span class="mention">@$1</span>');

    // 7. URLs auto-linkified
    safe = safe.replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');

    // 8. List items: lines starting with - or *
    safe = safe.replace(/^(\s*)[*-] (.+)$/gm, '$1<span class="md-li">$2</span>');

    // 9. Headers (# ## ###)
    safe = safe.replace(/^### (.+)$/gm, '<span class="md-h3">$1</span>');
    safe = safe.replace(/^## (.+)$/gm, '<span class="md-h2">$1</span>');
    safe = safe.replace(/^# (.+)$/gm, '<span class="md-h1">$1</span>');

    // 10. Newlines → <br>
    safe = safe.replace(/\n/g, '<br>');

    // 11. Restore inline code
    safe = safe.replace(/%%INLINE_(\d+)%%/g, (_, i) => `<code>${inlineCodes[i]}</code>`);

    // 12. Restore code blocks
    safe = safe.replace(/%%CODEBLOCK_(\d+)%%/g, (_, i) => `<pre><code>${codeBlocks[i]}</code></pre>`);

    // 13. Restore file blocks (raw HTML — already safe, built by us)
    safe = safe.replace(/%%FILEBLOCK_(\d+)%%/g, (_, i) => fileBlocks[i]);

    return safe;
  }
</script>

<style>
  .chat {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
    background: var(--bg-primary);
  }

  .chat-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 12px;
    border-bottom: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }
  .room-name { font-weight: 600; color: var(--text-primary); }
  .msg-count { font-size: 11px; color: var(--text-muted); }

  /* Search bar */
  .search-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 12px;
    background: var(--bg-secondary);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .search-icon { font-size: 14px; flex-shrink: 0; }
  .search-bar input {
    flex: 1;
    font-size: 12px;
    padding: 4px 8px;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 3px;
  }
  .search-bar input:focus { border-color: var(--info); }
  .search-count {
    font-size: 11px;
    color: var(--text-secondary);
    flex-shrink: 0;
    white-space: nowrap;
  }
  .search-close {
    background: none;
    border: none;
    color: var(--text-secondary);
    padding: 2px 6px;
    cursor: pointer;
    font-size: 14px;
  }
  .search-close:hover { color: var(--text-primary); }

  .chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .date-sep {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 0 4px;
    font-size: 10px;
    color: var(--text-muted);
  }
  .date-sep::before, .date-sep::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border-subtle);
  }

  .msg {
    display: flex;
    gap: 8px;
    padding: 2px 0;
    line-height: 1.5;
  }
  .msg-cont {
    /* Continuation: indent to align with text after nick */
    padding-left: calc(42px + 8px);
    padding-top: 0;
  }
  .msg.system { opacity: 0.6; }
  .msg-time { color: var(--text-muted); font-size: 11px; flex-shrink: 0; min-width: 42px; }
  .msg-nick { font-weight: 600; flex-shrink: 0; white-space: nowrap; }
  .msg-text { color: var(--text-primary); word-break: break-word; position: relative; }

  .msg-text :global(.mention) {
    color: var(--info);
    font-weight: 600;
    background: var(--info);
    background: rgba(108, 182, 255, 0.1);
    padding: 0 3px;
    border-radius: 3px;
  }
  .msg-text :global(code) {
    background: var(--bg-tertiary);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 12px;
  }
  .msg-text :global(pre) {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-subtle);
    border-radius: 4px;
    padding: 6px 8px;
    margin: 4px 0;
    overflow-x: auto;
    white-space: pre;
  }
  .msg-text :global(pre code) {
    background: none;
    padding: 0;
    font-size: 12px;
  }
  .msg-text :global(strong) { font-weight: 700; }
  .msg-text :global(em) { font-style: italic; color: var(--text-secondary); }
  .msg-text :global(a) { color: var(--info); text-decoration: underline; }
  .msg-text :global(a:hover) { color: var(--accent); }
  .msg-text :global(.md-li) { display: block; padding-left: 12px; }
  .msg-text :global(.md-li)::before { content: '•'; position: absolute; margin-left: -12px; color: var(--text-muted); }
  .msg-text :global(.md-h1) { display: block; font-size: 15px; font-weight: 700; margin: 4px 0 2px; }
  .msg-text :global(.md-h2) { display: block; font-size: 14px; font-weight: 700; margin: 3px 0 2px; }
  .msg-text :global(.md-h3) { display: block; font-size: 13px; font-weight: 600; margin: 2px 0 1px; color: var(--text-secondary); }

  /* Search highlight */
  .msg-text :global(mark) {
    background: var(--warning);
    color: var(--bg-primary);
    padding: 0 2px;
    border-radius: 2px;
  }

  .empty {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    color: var(--text-muted);
  }
  .empty-icon { font-size: 32px; opacity: 0.3; }
  .empty-hint { font-size: 10px; opacity: 0.5; }

  .chat-input {
    display: flex;
    align-items: flex-end;
    gap: 8px;
    padding: 8px 12px;
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }
  .input-nick {
    color: var(--info); /* fallback, overridden by inline style */
    font-weight: 600;
    font-size: 12px;
    flex-shrink: 0;
    padding-bottom: 4px;
  }
  .chat-input textarea {
    flex: 1;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    color: var(--text-primary);
    border-radius: 4px;
    padding: 6px 8px;
    font-family: inherit;
    font-size: inherit;
    line-height: 1.4;
    resize: none;
    overflow-y: hidden;
    min-height: 28px;
    max-height: 120px;
  }
  .chat-input textarea:focus {
    outline: none;
    border-color: var(--info);
  }
  .chat-input button {
    align-self: flex-end;
  }
  .attach-btn {
    align-self: flex-end;
    width: 28px;
    height: 28px;
    padding: 0;
    font-size: 18px;
    line-height: 1;
    border-radius: 4px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    color: var(--text-secondary);
    cursor: pointer;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .attach-btn:hover {
    color: var(--info);
    border-color: var(--info);
  }

  /* File preview in messages */
  .msg-text :global(.file-preview) {
    margin: 4px 0;
    display: inline-block;
  }
  .msg-text :global(.file-image) {
    display: block;
  }
  .msg-text :global(.file-image img) {
    max-width: 320px;
    max-height: 240px;
    border-radius: 6px;
    border: 1px solid var(--border-subtle);
    cursor: pointer;
    display: block;
  }
  .msg-text :global(.file-image img:hover) {
    border-color: var(--info);
  }
  .msg-text :global(.file-meta) {
    display: block;
    font-size: 10px;
    color: var(--text-muted);
    margin-top: 2px;
  }
  .msg-text :global(.file-doc) {
    display: inline-flex;
  }
  .msg-text :global(.file-link) {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    background: var(--bg-tertiary);
    border: 1px solid var(--border-subtle);
    border-radius: 4px;
    text-decoration: none !important;
    color: var(--text-primary) !important;
    font-size: 12px;
  }
  .msg-text :global(.file-link:hover) {
    border-color: var(--info);
  }
  .msg-text :global(.file-icon) {
    font-size: 10px;
    font-weight: 700;
    padding: 2px 4px;
    background: var(--bg-secondary);
    border-radius: 3px;
    color: var(--text-secondary);
  }
  .msg-text :global(.file-name) {
    font-weight: 500;
  }
  .msg-text :global(.file-size) {
    font-size: 10px;
    color: var(--text-muted);
  }
</style>
