/**
 * Project Store — Workspace scoping for the dashboard
 *
 * Fetches project list from HTTP API.
 * Active project is persisted in localStorage.
 * Other stores use activeProject to scope their fetches.
 */

const STORAGE_KEY = 'aircp_active_project';

let projects = $state([]);
let activeProject = $state(localStorage.getItem(STORAGE_KEY) || null);

async function fetchProjects() {
  try {
    const res = await fetch('/api/projects');
    if (!res.ok) return;
    const data = await res.json();
    projects = data.projects || [];

    // If saved project no longer exists, reset
    if (activeProject && !projects.some(p => p.id === activeProject)) {
      activeProject = null;
      localStorage.removeItem(STORAGE_KEY);
    }
  } catch (e) {
    console.warn('[project] Failed to fetch:', e);
  }
}

function switchProject(projectId) {
  if (projectId === activeProject) return;
  activeProject = projectId || null;
  if (activeProject) {
    localStorage.setItem(STORAGE_KEY, activeProject);
  } else {
    localStorage.removeItem(STORAGE_KEY);
  }
}

let activeProjectName = $derived(
  projects.find(p => p.id === activeProject)?.name || activeProject || 'All'
);

async function init() {
  await fetchProjects();
}

export const projectStore = {
  get projects() { return projects; },
  get activeProject() { return activeProject; },
  get activeProjectName() { return activeProjectName; },
  switchProject,
  fetchProjects,
  init,
};
