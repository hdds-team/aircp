/**
 * Settings Store — Operator identity (login)
 *
 * Operator ID is stored in localStorage. All stores and components
 * read from here instead of hardcoding any user ID.
 * If null, the dashboard shows a login screen.
 */
import { AGENTS } from '../lib/topics.js';

const STORAGE_KEY = 'aircp_operator_id';
const DEFAULT_COLOR = '#57b8ff';

let operatorId = $state(localStorage.getItem(STORAGE_KEY) || null);

let operatorColor = $derived(
  operatorId ? (AGENTS[operatorId]?.color || DEFAULT_COLOR) : DEFAULT_COLOR
);

let isLoggedIn = $derived(!!operatorId);

function setOperatorId(id) {
  if (!id) return;
  const normalized = id.startsWith('@') ? id : `@${id}`;
  operatorId = normalized;
  localStorage.setItem(STORAGE_KEY, normalized);
}

function logout() {
  operatorId = null;
  localStorage.removeItem(STORAGE_KEY);
}

export const settingsStore = {
  get operatorId() { return operatorId; },
  get operatorColor() { return operatorColor; },
  get isLoggedIn() { return isLoggedIn; },
  setOperatorId,
  logout,
};
