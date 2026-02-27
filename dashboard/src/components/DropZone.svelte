<script>
  import { messagesStore } from '../stores/messages.svelte.js';
  import { settingsStore } from '../stores/settings.svelte.js';

  const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB
  const ALLOWED_TYPES = new Set([
    'image/png', 'image/jpeg', 'image/gif', 'image/webp',
    'application/pdf',
    'text/plain', 'text/markdown', 'text/csv',
    'application/json',
  ]);

  let dragging = $state(false);
  let uploading = $state(false);
  let error = $state('');
  let progress = $state('');
  let dragCounter = 0;

  function handleDragEnter(e) {
    e.preventDefault();
    dragCounter++;
    dragging = true;
  }

  function handleDragLeave(e) {
    e.preventDefault();
    dragCounter--;
    if (dragCounter <= 0) {
      dragging = false;
      dragCounter = 0;
    }
  }

  function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  }

  function handleDrop(e) {
    e.preventDefault();
    dragging = false;
    dragCounter = 0;
    const files = e.dataTransfer?.files;
    if (files?.length) {
      uploadFile(files[0]);
    }
  }

  function handleFileSelect(e) {
    const file = e.target?.files?.[0];
    if (file) uploadFile(file);
    // Reset input so same file can be re-selected
    e.target.value = '';
  }

  async function uploadFile(file) {
    error = '';
    progress = '';

    // Validate type
    if (!ALLOWED_TYPES.has(file.type)) {
      error = `Type "${file.type || 'unknown'}" not allowed. Use: images, PDF, or text files.`;
      setTimeout(() => error = '', 5000);
      return;
    }

    // Validate size
    if (file.size > MAX_FILE_SIZE) {
      const sizeMB = (file.size / 1024 / 1024).toFixed(1);
      error = `File too large (${sizeMB} MB, max 10 MB)`;
      setTimeout(() => error = '', 5000);
      return;
    }

    uploading = true;
    progress = `Uploading ${file.name}...`;

    try {
      const formData = new FormData();
      formData.append('file', file);
      formData.append('room', messagesStore.activeRoom);
      formData.append('from', settingsStore.operatorId);

      const res = await fetch('/api/upload', {
        method: 'POST',
        body: formData,
      });

      const data = await res.json();
      if (!res.ok) {
        error = data.error || `Upload failed (${res.status})`;
        setTimeout(() => error = '', 5000);
        return;
      }

      progress = `Uploaded ${data.filename}`;
      setTimeout(() => progress = '', 3000);
    } catch (e) {
      error = `Upload failed: ${e.message}`;
      setTimeout(() => error = '', 5000);
    } finally {
      uploading = false;
    }
  }
</script>

<!-- Invisible drag overlay wraps the chat area -->
<div
  class="dropzone-wrapper"
  ondragenter={handleDragEnter}
  ondragleave={handleDragLeave}
  ondragover={handleDragOver}
  ondrop={handleDrop}
>
  <slot />

  {#if dragging}
    <div class="drop-overlay">
      <div class="drop-content">
        <span class="drop-icon">+</span>
        <span class="drop-text">Drop file to upload</span>
        <span class="drop-hint">Images, PDF, text (max 10 MB)</span>
      </div>
    </div>
  {/if}
</div>

{#if error || progress}
  <div class="upload-status" class:is-error={!!error}>
    {#if uploading}
      <span class="spinner"></span>
    {/if}
    <span>{error || progress}</span>
  </div>
{/if}

<!-- Attach button (for click-to-upload) -->
<input
  type="file"
  id="file-upload-input"
  accept="image/*,.pdf,.txt,.md,.csv,.json"
  onchange={handleFileSelect}
  style="display:none"
/>

<style>
  .dropzone-wrapper {
    position: relative;
    display: contents;
  }

  .drop-overlay {
    position: absolute;
    inset: 0;
    background: rgba(108, 182, 255, 0.12);
    border: 2px dashed var(--info, #6cb6ff);
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
    pointer-events: none;
  }

  .drop-content {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
  }

  .drop-icon {
    font-size: 32px;
    color: var(--info, #6cb6ff);
    opacity: 0.8;
  }

  .drop-text {
    font-size: 14px;
    font-weight: 600;
    color: var(--info, #6cb6ff);
  }

  .drop-hint {
    font-size: 11px;
    color: var(--text-muted, #8b949e);
  }

  .upload-status {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 12px;
    font-size: 11px;
    color: var(--text-secondary, #adbac7);
    background: var(--bg-secondary, #1c2128);
    border-top: 1px solid var(--border-subtle, #373e47);
  }

  .upload-status.is-error {
    color: var(--error, #f47067);
  }

  .spinner {
    width: 12px;
    height: 12px;
    border: 2px solid var(--border, #444c56);
    border-top-color: var(--info, #6cb6ff);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
