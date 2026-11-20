import { describe, expect, it } from 'vitest';
import {
  STEP,
  componentBlockers,
  initialState,
  joinPath,
  mergeScan,
  nameProblems,
  saveDefaults,
  selections,
  servicesNotInDraft,
  setKind,
  slug,
  stepStatus,
  toSpecDocument,
  unfold,
  updateComponent,
  withRead,
} from './model';
import { draftedState } from './test/fixtures';
import type { DraftResponse } from './types';

describe('the components step', () => {
  it('waits for every unsure component to be decided', () => {
    const state = draftedState();
    expect(componentBlockers(state.components)).toEqual([
      '1 component still needs you to decide what it is.',
    ]);
    expect(componentBlockers(setKind(state.components, 'shop/billing', 'service'))).toEqual([]);
  });

  it('needs at least one service', () => {
    const state = draftedState();
    const noService = setKind(setKind(state.components, 'shop/api', 'notModelled'), 'shop/billing', 'infrastructure');
    expect(componentBlockers(noService)).toContain('Mark at least one service: the analysis needs an application tier.');
  });

  it('flags clashing and malformed names, but not those of components left out', () => {
    let components = updateComponent(draftedState().components, 'shop/billing', { name: 'api', kind: 'service' });
    expect(nameProblems(components)).toEqual({
      'shop/api': 'Another component has this name.',
      'shop/billing': 'Another component has this name.',
    });
    components = setKind(updateComponent(components, 'shop/billing', { name: 'bad name' }), 'shop/billing', 'notModelled');
    expect(nameProblems(components)).toEqual({});
  });

  it('separates a folded replica back into its own component', () => {
    const state = draftedState();
    const services = state.read!.draft!.services;
    const after = unfold(state.components, 'shop/db', 'shop/db-2', services);
    const db = after.find((c) => c.id === 'shop/db')!;
    expect(db.folded).toEqual([]);
    expect(db.instances).toBe(1);
    expect(after.find((c) => c.id === 'shop/db-2')?.kind).toBe('infrastructure');
    expect(servicesNotInDraft(after, services)).toEqual([]);
  });
});

describe('the spec', () => {
  it('holds what was decided, and leaves out what is not modelled', () => {
    const state = draftedState();
    state.components = setKind(state.components, 'shop/billing', 'notModelled');
    const spec = toSpecDocument(state);
    expect(spec.composeProjects).toEqual(['shop']);
    expect(spec.services.map((c) => c.name)).toEqual(['api']);
    expect(spec.infrastructure).toEqual([
      expect.objectContaining({ name: 'db', composeService: 'db', hasHealthcheck: true, instances: 2 }),
    ]);
  });

  it('defaults to a perf folder in the first project, named after it', () => {
    expect(saveDefaults(draftedState().read!)).toEqual({
      specName: 'shop',
      saveDirectory: '/repos/shop/perf',
      fileName: 'shop.toml',
    });
    expect(joinPath('C:\\code\\shop', 'perf')).toBe('C:\\code\\shop\\perf');
    expect(slug('FINKI blogging platform!')).toBe('finki-blogging-platform');
    expect(slug('***')).toBe('system');
  });
});

describe('reading compose files again', () => {
  const shop = draftedState().read!;
  const blog: DraftResponse = {
    ...shop,
    projects: [{ ...shop.projects[0], directory: '/repos/blog', files: ['/repos/blog/compose.yaml'], name: 'blog' }],
  };

  it('moves the save fields to the project picked after going back', () => {
    const first = withRead(initialState, shop);
    expect(first).toMatchObject({ specName: 'shop', saveDirectory: '/repos/shop/perf', fileName: 'shop.toml' });
    expect(withRead(first, blog)).toMatchObject({
      specName: 'blog',
      saveDirectory: '/repos/blog/perf',
      fileName: 'blog.toml',
      components: blog.draft!.components,
    });
  });

  it('keeps what the user typed', () => {
    const edited = { ...withRead(initialState, shop), specName: 'My shop', saveDirectory: '/specs' };
    expect(withRead(edited, blog)).toMatchObject({
      specName: 'My shop',
      saveDirectory: '/specs',
      fileName: 'blog.toml',
    });
  });

  it('follows the project after a read that found nothing to name it by', () => {
    const failed: DraftResponse = { projects: [{ ...shop.projects[0], ok: false, error: 'bad file' }], draft: null };
    const after = withRead(withRead(initialState, failed), blog);
    expect(after).toMatchObject({ specName: 'blog', saveDirectory: '/repos/blog/perf' });
  });

  it('forgets an earlier save', () => {
    const saved = { ...withRead(initialState, shop), savedPath: '/repos/shop/perf/shop.toml' };
    expect(withRead(saved, blog).savedPath).toBeNull();
  });
});

describe('choosing compose files', () => {
  it("keeps the user's choices across a new search", () => {
    const scan = {
      projects: [
        {
          directory: '/r/a',
          files: [
            { path: '/r/a/compose.yaml', name: 'compose.yaml', role: 'base' as const, selected: true },
            { path: '/r/a/x.compose.yaml', name: 'x.compose.yaml', role: 'overlay' as const, selected: false },
          ],
        },
      ],
      dockerfiles: [],
      warnings: [],
    };
    const before = mergeScan([], scan);
    before[0].files[1].selected = true;
    before[0].name = 'custom';
    const after = mergeScan(before, scan);
    expect(after[0].name).toBe('custom');
    expect(selections(after)).toEqual([
      { directory: '/r/a', files: ['/r/a/compose.yaml', '/r/a/x.compose.yaml'], name: 'custom' },
    ]);
  });
});

describe('telling the user whether they can go on', () => {
  it('waits for the checks, then says so either way', () => {
    expect(stepStatus(0, initialState, 'checking').ready).toBe(false);
    expect(stepStatus(0, initialState, 'missing').message).toContain('marked in red');
    expect(stepStatus(0, initialState, 'ready')).toEqual({
      ready: true,
      message: 'Everything needed is here. Go on to choose your project.',
    });
  });

  it('walks the project step through what is still to do', () => {
    const say = (s: Parameters<typeof stepStatus>[1]) => stepStatus(1, s, 'ready').message;
    expect(say(initialState)).toBe('Add the folder your app is in to begin.');
    const folder = { ...initialState, folders: ['/repos/shop'] };
    expect(say(folder)).toBe('Searching the folders…');
    const scanned = mergeScan([], {
      projects: [{ directory: '/repos/shop', files: [{ path: '/repos/shop/compose.yaml', name: 'compose.yaml', role: 'base', selected: true }] }],
      dockerfiles: [],
      warnings: [],
    });
    const found = { ...folder, scan: { projects: [], dockerfiles: [], warnings: [] }, projects: scanned };
    expect(say(found)).toBe('Press "Read the chosen compose files" to continue.');
    const unticked = { ...found, projects: [{ ...scanned[0], files: [{ ...scanned[0].files[0], selected: false }] }] };
    expect(say(unticked)).toBe('Tick at least one compose file.');
    const read = { ...draftedState(), folders: ['/repos/shop'], scan: found.scan, projects: scanned };
    expect(stepStatus(1, read, 'ready')).toEqual({
      ready: true,
      message: 'Read 3 components from 1 project. Go on to check them.',
    });
  });

  it('names what blocks the components step, then lets the user on', () => {
    const state = draftedState();
    expect(stepStatus(2, state, 'ready')).toEqual({
      ready: false,
      message: '1 component still needs you to decide what it is.',
    });
    const decided = { ...state, components: setKind(state.components, 'shop/billing', 'infrastructure') };
    expect(stepStatus(2, decided, 'ready').ready).toBe(true);
  });

  it('is done once the spec is saved', () => {
    expect(stepStatus(STEP.save, draftedState(), 'ready').ready).toBe(false);
    expect(stepStatus(STEP.save, { ...draftedState(), savedPath: '/repos/shop/perf/shop.toml' }, 'ready').ready).toBe(true);
  });
});

