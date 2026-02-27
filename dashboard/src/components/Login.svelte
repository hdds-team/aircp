<script>
  import { settingsStore } from '../stores/settings.svelte.js';

  let nick = $state('');
  let error = $state('');

  function login() {
    const raw = nick.trim().replace(/^@/, '');
    if (!raw) {
      error = 'Enter a nick';
      return;
    }
    if (raw.length < 2 || raw.length > 20) {
      error = '2-20 characters';
      return;
    }
    if (!/^[\w-]+$/.test(raw)) {
      error = 'Letters, digits, _ or - only';
      return;
    }
    settingsStore.setOperatorId(raw);
  }

  function handleKeydown(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      login();
    }
  }
</script>

<div class="login-overlay">
  <div class="login-box">
    <div class="logo">aIRCp</div>
    <div class="subtitle">Multi-Agent Dashboard</div>

    <div class="field">
      <label for="nick">Nickname</label>
      <div class="input-row">
        <span class="at">@</span>
        <input
          id="nick"
          type="text"
          placeholder="your-nick"
          bind:value={nick}
          onkeydown={handleKeydown}
          autofocus
        />
      </div>
      {#if error}
        <span class="error">{error}</span>
      {/if}
    </div>

    <button onclick={login}>Connect</button>
  </div>
</div>

<style>
  .login-overlay {
    position: fixed;
    inset: 0;
    background: var(--bg-primary);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }

  .login-box {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 16px;
    padding: 40px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 8px;
    min-width: 300px;
  }

  .logo {
    font-size: 28px;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: -1px;
  }

  .subtitle {
    font-size: 11px;
    color: var(--text-muted);
    margin-top: -12px;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
  }

  label {
    font-size: 11px;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .input-row {
    display: flex;
    align-items: center;
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
  }

  .input-row:focus-within {
    border-color: var(--info);
  }

  .at {
    padding: 0 0 0 10px;
    color: var(--text-muted);
    font-weight: 600;
    flex-shrink: 0;
  }

  .input-row input {
    flex: 1;
    border: none;
    background: transparent;
    padding: 8px 10px 8px 4px;
    font-size: 14px;
  }

  .input-row input:focus {
    outline: none;
    border: none;
  }

  .error {
    font-size: 11px;
    color: var(--danger);
  }

  button {
    width: 100%;
    padding: 8px;
    font-size: 13px;
    font-weight: 600;
    border-color: var(--accent);
    color: var(--accent);
    cursor: pointer;
  }

  button:hover {
    background: var(--accent);
    color: var(--bg-primary);
  }
</style>
