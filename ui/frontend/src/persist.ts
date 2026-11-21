import { STEPS, initialState, stepStatus, type WizardState } from './model';
import { initialAuth, initialLoad } from './routes';

/**
 * The wizard as the user left it, kept in this browser so a reload (or
 * restarting the app, which keeps its address) doesn't start over. The
 * password or key is never kept: it is asked for again.
 */
const KEY = 'microservice-scripts.wizard';

/** Raised when what is kept changes shape so that an older copy can't be used. */
const VERSION = 1;

export type Restored = { state: WizardState; active: number; savedAt: number };

type Kept = { version: number; savedAt: number; active: number; state: WizardState };

export function keep(state: WizardState, active: number): void {
  try {
    const kept: Kept = { version: VERSION, savedAt: Date.now(), active, state: { ...state, secret: '' } };
    localStorage.setItem(KEY, JSON.stringify(kept));
  } catch {
    // storage full or blocked: the wizard still works, it just won't survive a reload
  }
}

export function forget(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // nothing kept to forget
  }
}

/** The wizard as last kept, or null when there is none (or it can't be read). */
export function restore(): Restored | null {
  let kept: Partial<Kept>;
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    kept = JSON.parse(raw) as Partial<Kept>;
  } catch {
    return null;
  }
  if (kept.version !== VERSION || !kept.state || typeof kept.state !== 'object') return null;
  // fields added since it was kept get their defaults
  const state: WizardState = {
    ...initialState,
    ...kept.state,
    auth: { ...initialAuth, ...kept.state.auth },
    load: { ...initialLoad, ...kept.state.load },
    secret: '',
  };
  if (!hasProgress(state)) return null;
  return { state, active: resumeStep(state, kept.active ?? 0), savedAt: kept.savedAt ?? 0 };
}

function hasProgress(state: WizardState): boolean {
  return state.folders.length + state.extraFiles.length + state.knownProjects.length > 0;
}

/**
 * The step to open: where the user was, or the first step before it that is
 * no longer done (so the wizard never opens past something missing).
 */
export function resumeStep(state: WizardState, active: number): number {
  const last = Math.min(Math.max(0, Math.floor(active)), STEPS.length - 1);
  for (let step = 1; step < last; step++) {
    if (!stepStatus(step, state, 'ready').ready) return step;
  }
  return last;
}
