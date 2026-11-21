import { describe, expect, it } from 'vitest';
import { STEP } from './model';
import { forget, keep, restore, resumeStep } from './persist';
import { draftedState } from './test/fixtures';

const filled = () => ({ ...draftedState(), folders: ['/repos/shop'], secret: 'hunter2' });

describe('keeping the wizard over a reload', () => {
  it('gives back what was filled in, but never the password', () => {
    keep(filled(), STEP.project);
    const back = restore();
    expect(back?.state.components.map((c) => c.id)).toEqual(['shop/api', 'shop/db', 'shop/billing']);
    expect(back?.state.secret).toBe('');
    expect(localStorage.getItem('microservice-scripts.wizard')).not.toContain('hunter2');
    expect(back?.active).toBe(STEP.project);
  });

  it('has nothing to give back before a folder is chosen, after forgetting, or from something unreadable', () => {
    expect(restore()).toBeNull();
    keep({ ...filled(), folders: [] }, 3);
    expect(restore()).toBeNull();
    keep(filled(), 1);
    forget();
    expect(restore()).toBeNull();
    localStorage.setItem('microservice-scripts.wizard', '{not json');
    expect(restore()).toBeNull();
    localStorage.setItem('microservice-scripts.wizard', JSON.stringify({ version: 999, state: filled(), active: 2 }));
    expect(restore()).toBeNull();
  });

  it('fills in fields added since it was kept', () => {
    const { tried: _tried, ...older } = filled();
    localStorage.setItem('microservice-scripts.wizard', JSON.stringify({ version: 1, savedAt: 1, active: 0, state: older }));
    expect(restore()?.state.tried).toBeNull();
  });

  it('never opens past a step that is no longer done', () => {
    // the folders were never searched in this state, so the project step isn't done
    expect(resumeStep(filled(), STEP.features)).toBe(STEP.project);
    const searched = { ...filled(), scan: { projects: [], folders: [] } as never };
    expect(resumeStep(searched, STEP.features)).toBe(STEP.project);
    expect(resumeStep(filled(), 0)).toBe(0);
    expect(resumeStep(filled(), 99)).toBe(STEP.project);
  });
});
