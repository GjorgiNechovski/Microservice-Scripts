// The wizard's state and the rules over it, kept free of React so they can be
// tested on their own.

import {
  componentOf,
  featureComponents,
  featureList,
  initialLoad,
  initialAuth,
  isModelled,
  loadProblems,
  authProblems,
  routeDefaults,
  routeProblems,
  selectedRoutes,
  suggestBaseUrl,
  toLoadScript,
  secretVariable,
  triedErrors,
  usesSecret,
  validBaseUrl,
  type LoadSettings,
  type AuthSettings,
  type RouteChoice,
  type Tried,
} from './routes';
import type {
  AppProject,
  ComponentKind,
  DraftComponent,
  DraftResponse,
  LoadScript,
  ProjectSelection,
  RunState,
  ScannedFile,
  KnownProject,
  ScanResult,
  SpecComponent,
  SpecDocument,
} from './types';

export { isModelled } from './routes';

/** A folder with compose files, as the user has chosen to use it. */
export type ProjectChoice = {
  directory: string;
  files: ScannedFile[];
  /** The name the app is started with (-p); empty lets Compose decide. */
  name: string;
};

/** What the save step writes, and where. */
export type SaveFields = { specName: string; saveDirectory: string; fileName: string };

export type WizardState = {
  /** Folders to search: one per repository, or one holding several. */
  folders: string[];
  /** Compose files the user pointed at directly. */
  extraFiles: string[];
  /** Compose projects chosen from Docker's list: their name and files, as they were started. */
  knownProjects: KnownProject[];
  scan: ScanResult | null;
  projects: ProjectChoice[];
  read: DraftResponse | null;
  components: DraftComponent[];
  specName: string;
  saveDirectory: string;
  fileName: string;
  /**
   * The save fields as last suggested from the compose files. A field still
   * equal to its suggestion follows the next read; one the user changed is kept.
   */
  suggested: SaveFields | null;
  /** The routes found in the code, and the ones added by hand. */
  routes: RouteChoice[];
  /** Where the load test sends requests: the app's front door on this machine. */
  baseUrl: string;
  /** The base address as last suggested; one the user typed survives a new read. */
  suggestedBaseUrl: string;
  auth: AuthSettings;
  /** The login password, token or API key: never saved, passed to the run. */
  secret: string;
  /** The components of each feature, where the user changed the suggestion. */
  features: Record<string, string[]>;
  load: LoadSettings;
  /** Where the spec was saved, until anything that goes into it changes. */
  savedPath: string | null;
  /** The last time the routes were tried, and how each answered. */
  tried: Tried | null;
};

export const initialState: WizardState = {
  folders: [],
  extraFiles: [],
  knownProjects: [],
  scan: null,
  projects: [],
  read: null,
  components: [],
  specName: '',
  saveDirectory: '',
  fileName: '',
  suggested: null,
  routes: [],
  baseUrl: '',
  suggestedBaseUrl: '',
  auth: initialAuth,
  secret: '',
  features: {},
  load: initialLoad,
  savedPath: null,
  tried: null,
};

export const STEPS = ['Check', 'Project', 'Components', 'Routes', 'Features', 'Users', 'Save', 'Run'] as const;

export const STEP = {
  check: 0,
  project: 1,
  components: 2,
  routes: 3,
  features: 4,
  users: 5,
  save: 6,
  run: 7,
} as const;

export type PrerequisiteStatus = 'checking' | 'missing' | 'ready';

/** Whether a step is done, and what to tell the user either way. */
export type StepStatus = { ready: boolean; message: string };

export function stepStatus(
  step: number,
  state: WizardState,
  prerequisites: PrerequisiteStatus,
  run?: RunState,
): StepStatus {
  if (step === 0) {
    if (prerequisites === 'checking') return { ready: false, message: 'Checking what this machine has…' };
    return prerequisites === 'ready'
      ? { ready: true, message: 'Everything needed is here. Go on to choose your project.' }
      : { ready: false, message: 'Fix the items marked in red, then press "Check again".' };
  }
  if (step === 1) {
    if (state.folders.length + state.extraFiles.length + state.knownProjects.length === 0) {
      return { ready: false, message: 'Add the folder your app is in to begin.' };
    }
    if (!state.scan) return { ready: false, message: 'Searching the folders…' };
    if (state.projects.length === 0) {
      return { ready: false, message: 'No compose files found yet: add another folder, or point at the file.' };
    }
    if (selections(state.projects).length === 0) return { ready: false, message: 'Tick at least one compose file.' };
    if (!state.read) return { ready: false, message: 'Press "Read the chosen compose files" to continue.' };
    const read = state.read.projects.filter((p) => p.ok).length;
    if (!state.read.draft || read === 0) {
      return { ready: false, message: "Compose couldn't read the chosen files: see the message above." };
    }
    const failed = state.read.projects.length - read;
    return {
      ready: true,
      message:
        `Read ${state.components.length} components from ${read} project${read === 1 ? '' : 's'}` +
        `${failed ? ` (${failed} couldn't be read)` : ''}. Go on to check them.`,
    };
  }
  const blocked = (blockers: string[]): StepStatus => ({
    ready: false,
    message: blockers.length === 1 ? blockers[0] : `${blockers[0]} (and ${blockers.length - 1} more, shown above)`,
  });
  if (step === STEP.components) {
    const blockers = componentBlockers(state.components);
    return blockers.length > 0
      ? blocked(blockers)
      : { ready: true, message: 'Every component is placed. Go on to pick the routes to test.' };
  }
  if (step === STEP.routes) {
    const blockers = routesBlockers(state);
    if (blockers.length > 0) return blocked(blockers);
    const picked = selectedRoutes(state.routes, state.components);
    const features = featureList(picked).length;
    const errors = triedErrors(state.tried, picked).length;
    return {
      ready: true,
      message:
        `${picked.length} route${picked.length === 1 ? '' : 's'} in ${features} feature${features === 1 ? '' : 's'}` +
        (errors > 0
          ? `, but ${errors} answered with an error when tried: see the red badges.`
          : '. Go on to check what each feature needs.'),
    };
  }
  if (step === STEP.features) {
    const blockers = featuresBlockers(state);
    return blockers.length > 0
      ? blocked(blockers)
      : { ready: true, message: 'Every feature has the components it needs. Go on to choose how many users.' };
  }
  if (step === STEP.users) {
    const blockers = loadProblems(state.load);
    return blockers.length > 0
      ? blocked(blockers)
      : { ready: true, message: 'Go on to save the spec and the load test.' };
  }
  if (step === STEP.save) {
    return state.savedPath
      ? { ready: true, message: 'Saved. Go on to start the analysis.' }
      : { ready: false, message: 'Check the files, then press "Save".' };
  }
  switch (run) {
    case 'running':
      return { ready: false, message: 'Running: follow it above. You can leave this page open and come back.' };
    case 'stopping':
      return { ready: false, message: 'Stopping: putting back anything the run stopped…' };
    case 'finished':
      return { ready: true, message: 'Finished. See the results: the button on the right.' };
    case 'failed':
      return { ready: false, message: 'The run ended with an error: its output above says why.' };
    case 'stopped':
      return { ready: false, message: 'Stopped. Start again when you are ready.' };
    default:
      return { ready: false, message: 'Check the list above, then press "Start".' };
  }
}

/** What still stops the routes step from being done. */
export function routesBlockers(state: WizardState): string[] {
  const blockers: string[] = [];
  const picked = selectedRoutes(state.routes, state.components);
  if (state.load.script === 'own') {
    if (!state.load.ownScript.trim()) blockers.push('Give the path of your k6 script.');
  } else {
    if (!validBaseUrl(state.baseUrl)) blockers.push('Give the address the load test sends requests to (http://…).');
    blockers.push(...authProblems(state.auth));
  }
  if (picked.length === 0) blockers.push('Pick at least one route.');
  const broken = picked.filter((r) => routeProblems(r).length > 0).length;
  if (broken > 0) blockers.push(`Fix the ${broken === 1 ? 'route' : `${broken} routes`} marked in red.`);
  return blockers;
}

/** What still stops the features step from being done. */
export function featuresBlockers(state: WizardState): string[] {
  const services = state.read?.draft?.services ?? {};
  const picked = selectedRoutes(state.routes, state.components);
  return [
    ...featureList(picked)
      .filter((f) => featureComponents(f.name, picked, state.features, state.components, services).length === 0)
      .map((f) => `Pick the components feature '${f.name}' needs.`),
    ...unreachedServices(state).map(
      (c) => `No feature reaches ${c.name}: add it to one, or leave it out of the analysis (shown above).`,
    ),
  ];
}

/**
 * Services no feature needs: no request of the load test reaches them, so the
 * run can't measure them, and the analyzer marks a run with an unmeasured
 * service as not to be trusted.
 */
export function unreachedServices(state: WizardState): DraftComponent[] {
  const services = state.read?.draft?.services ?? {};
  const picked = selectedRoutes(state.routes, state.components);
  const needed = new Set(
    featureList(picked).flatMap((f) => featureComponents(f.name, picked, state.features, state.components, services)),
  );
  return state.components.filter((c) => c.kind === 'service' && !needed.has(c.id));
}

/** The chosen compose projects that were read, as the app is started: same files, same -p name. */
export function appProjects(state: WizardState): AppProject[] {
  return (state.read?.projects ?? [])
    .filter((p) => p.ok && p.name)
    .map((p) => ({
      directory: p.directory,
      files: p.files,
      name: state.projects.find((c) => c.directory === p.directory)?.name.trim() || null,
      composeName: p.name as string,
    }));
}

/** The load script the backend writes, or null when the user brings their own. */
export function toScript(state: WizardState): LoadScript | null {
  return state.load.script === 'own'
    ? null
    : toLoadScript(state.baseUrl, state.auth, selectedRoutes(state.routes, state.components));
}

/** The parts of the state that go into the saved files: changing one means saving again. */
const SAVED_FROM = [
  'read',
  'components',
  'routes',
  'baseUrl',
  'auth',
  'features',
  'load',
  'specName',
  'saveDirectory',
  'fileName',
] as const satisfies readonly (keyof WizardState)[];

/** An update, after which an earlier save is forgotten if anything that was saved changed. */
export function settle(previous: WizardState, next: WizardState): WizardState {
  if (next.savedPath && next.savedPath === previous.savedPath && SAVED_FROM.some((k) => previous[k] !== next[k])) {
    return { ...next, savedPath: null };
  }
  return next;
}

/**
 * The projects of a new scan, keeping what the user already chose for files
 * and folders seen before.
 */
export function mergeScan(previous: ProjectChoice[], scan: ScanResult): ProjectChoice[] {
  return scan.projects.map((p) => {
    const before = previous.find((q) => q.directory === p.directory);
    return {
      directory: p.directory,
      // the user's own choice first, then the name Docker started it under
      name: before ? before.name : (p.name ?? ''),
      files: p.files.map((f) => {
        const known = before?.files.find((g) => g.path === f.path);
        return known ? { ...f, selected: known.selected } : f;
      }),
    };
  });
}

/** What the backend reads: only folders with at least one chosen file. */
export function selections(projects: ProjectChoice[]): ProjectSelection[] {
  return projects
    .filter((p) => p.files.some((f) => f.selected))
    .map((p) => ({
      directory: p.directory,
      files: p.files.filter((f) => f.selected).map((f) => f.path),
      name: p.name.trim() || null,
    }));
}

const COMPONENT_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

/** Problems with component names, by component id. */
export function nameProblems(components: DraftComponent[]): Record<string, string> {
  const problems: Record<string, string> = {};
  const modelled = components.filter(isModelled);
  for (const c of modelled) {
    if (!COMPONENT_NAME.test(c.name)) {
      problems[c.id] = "Letters, digits, '-', '_' and '.' only.";
    } else if (modelled.filter((o) => o.name === c.name).length > 1) {
      problems[c.id] = 'Another component has this name.';
    }
  }
  return problems;
}

/** What still stops the components step from being done. */
export function componentBlockers(components: DraftComponent[]): string[] {
  const blockers: string[] = [];
  const unsure = components.filter((c) => c.kind === 'unsure').length;
  if (unsure > 0) {
    blockers.push(`${unsure} component${unsure === 1 ? '' : 's'} still need${unsure === 1 ? 's' : ''} you to decide what ${unsure === 1 ? 'it is' : 'they are'}.`);
  }
  if (!components.some((c) => c.kind === 'service')) {
    blockers.push('Mark at least one service: the analysis needs an application tier.');
  }
  if (Object.keys(nameProblems(components)).length > 0) {
    blockers.push('Fix the component names marked in red.');
  }
  return blockers;
}

export function updateComponent(
  components: DraftComponent[],
  id: string,
  patch: Partial<DraftComponent>,
): DraftComponent[] {
  return components.map((c) => (c.id === id ? { ...c, ...patch } : c));
}

export function setKind(components: DraftComponent[], id: string, kind: ComponentKind): DraftComponent[] {
  return updateComponent(components, id, { kind });
}

/**
 * Takes a folded compose service back out of its component, to model it on
 * its own (a pooler you want counted as a single point of failure, say).
 */
export function unfold(
  components: DraftComponent[],
  parentId: string,
  memberId: string,
  services: Record<string, DraftComponent>,
): DraftComponent[] {
  const parent = components.find((c) => c.id === parentId);
  const member = parent?.folded.find((f) => f.id === memberId);
  const own = services[memberId];
  if (!parent || !member || !own) return components;
  const instances = member.role === 'replica' ? Math.max(1, parent.instances - 1) : parent.instances;
  return [
    ...updateComponent(components, parentId, {
      folded: parent.folded.filter((f) => f.id !== memberId),
      instances,
      instancesReason: instances === parent.instances ? parent.instancesReason : null,
    }),
    { ...own, folded: [] },
  ];
}

/** Compose services not in the draft at all, neither as a component nor folded into one. */
export function servicesNotInDraft(
  components: DraftComponent[],
  services: Record<string, DraftComponent>,
): DraftComponent[] {
  const used = new Set(components.flatMap((c) => [c.id, ...c.folded.map((f) => f.id)]));
  return Object.values(services).filter((s) => !used.has(s.id));
}

export function slug(name: string): string {
  const s = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return s || 'system';
}

/** Joins a path the way the folder it starts from is written (Windows or not). */
export function joinPath(directory: string, name: string): string {
  const windows = directory.includes('\\') && !directory.includes('/');
  const sep = windows ? '\\' : '/';
  return directory.endsWith(sep) ? directory + name : directory + sep + name;
}

/** Defaults for the save step: a perf/ folder in the first project, named after the system. */
export function saveDefaults(read: DraftResponse): SaveFields {
  const first = read.projects.find((p) => p.ok);
  const name = first?.name ?? 'my-system';
  return {
    specName: name,
    saveDirectory: first ? joinPath(first.directory, 'perf') : '',
    fileName: `${slug(name)}.toml`,
  };
}

/**
 * The state once compose files are read: their components, and save fields for
 * the projects just read. Going back to pick another project moves every field
 * the user hasn't changed to the new one; the ones they typed are kept. Any
 * earlier save no longer matches, so it's forgotten.
 */
export function withRead(s: WizardState, read: DraftResponse): WizardState {
  const next = saveDefaults(read);
  const pick = (field: keyof SaveFields) =>
    s[field].trim() === '' || s[field] === s.suggested?.[field] ? next[field] : s[field];
  const found = read.routes?.routes ?? [];
  const suggestedBaseUrl = suggestBaseUrl(found);
  const baseUrl = s.baseUrl.trim() === '' || s.baseUrl === s.suggestedBaseUrl ? suggestedBaseUrl : s.baseUrl;
  return {
    ...s,
    read,
    components: read.draft?.components ?? [],
    specName: pick('specName'),
    saveDirectory: pick('saveDirectory'),
    fileName: pick('fileName'),
    suggested: next,
    routes: routeDefaults(found, baseUrl),
    baseUrl,
    suggestedBaseUrl,
    features: {},
    savedPath: null,
  };
}

function toSpecComponent(c: DraftComponent): SpecComponent {
  return {
    name: c.name,
    project: c.project,
    composeService: c.composeService,
    readinessPort: c.readinessPort,
    readinessSource: c.readinessSource,
    hasHealthcheck: c.hasHealthcheck,
    instances: c.kind === 'infrastructure' ? c.instances : 1,
  };
}

/** The spec the backend writes, from the wizard's state. */
export function toSpecDocument(state: WizardState): SpecDocument {
  const projects = (state.read?.projects ?? []).filter((p) => p.ok && p.name).map((p) => p.name as string);
  const services = state.read?.draft?.services ?? {};
  const picked = selectedRoutes(state.routes, state.components);
  const operations: Record<string, string[]> = {};
  for (const f of featureList(picked)) {
    operations[f.name] = featureComponents(f.name, picked, state.features, state.components, services)
      .map((id) => componentOf(id, state.components)?.name)
      .filter((n): n is string => !!n);
  }
  const generated = state.load.script === 'generate';
  return {
    name: state.specName.trim(),
    composeProjects: projects,
    services: state.components.filter((c) => c.kind === 'service').map(toSpecComponent),
    infrastructure: state.components.filter((c) => c.kind === 'infrastructure').map(toSpecComponent),
    projectServices: state.read?.draft?.projectServices ?? {},
    operations,
    load: {
      script: generated ? 'load.js' : state.load.ownScript.trim(),
      users: state.load.users,
      duration: state.load.duration,
      env: generated
        ? { BASE_URL: state.baseUrl.trim(), THINK_TIME: String(state.load.thinkTime) }
        : { THINK_TIME: String(state.load.thinkTime) },
      envPassthrough: generated && usesSecret(state.auth) ? [secretVariable(state.auth)] : null,
    },
  };
}
