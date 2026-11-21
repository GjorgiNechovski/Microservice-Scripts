// The backend's API shapes (ui/backend), in the camelCase it sends.

export type PrerequisiteCheck = {
  id: string;
  label: string;
  required: boolean;
  ok: boolean;
  detail: string;
  fix: string | null;
};

export type FolderListing = {
  path: string;
  parent: string | null;
  folders: { name: string; path: string; hasCompose: boolean }[];
  roots: string[];
};

export type ComposeFileRole = 'base' | 'override' | 'overlay';

export type ScannedFile = { path: string; name: string; role: ComposeFileRole; selected: boolean };

/** name: the name Docker started it under (-p), for a project chosen from Docker's list. */
export type ScannedProject = { directory: string; files: ScannedFile[]; name?: string | null };

/** A compose project Docker knows, taken as it was started: its name and its files. */
export type KnownProject = { name: string; files: string[] };

export type ScanResult = { projects: ScannedProject[]; dockerfiles: string[]; warnings: string[] };

export type RunningProject = { name: string; status: string; files: string[]; directory: string | null };

export type ProjectSelection = { directory: string; files: string[]; name?: string | null };

export type ProjectReadResult = {
  directory: string;
  files: string[];
  name: string | null;
  ok: boolean;
  error: string | null;
  services: number;
};

export type ComponentKind = 'service' | 'infrastructure' | 'notModelled' | 'unsure';

export type Dependency = { id: string; via: string };

export type FoldedMember = { id: string; role: 'replica' | 'in front'; reason: string };

export type DraftComponent = {
  id: string;
  name: string;
  project: string;
  composeService: string;
  kind: ComponentKind;
  reasons: string[];
  image: string | null;
  builtFromSource: boolean;
  language: string | null;
  hasHealthcheck: boolean;
  healthcheckSource: string | null;
  restart: string | null;
  replicas: number | null;
  readinessPort: number | null;
  readinessSource: string | null;
  instances: number;
  instancesReason: string | null;
  dependsOn: Dependency[];
  folded: FoldedMember[];
  networks: string[];
};

export type NetworkSuggestion = { name: string; projects: string[]; components: number };

export type DraftResult = {
  components: DraftComponent[];
  services: Record<string, DraftComponent>;
  networks: NetworkSuggestion[];
  projectServices: Record<string, string[]>;
  notes: string[];
};

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

/** A route found in a service's code, and how it is reached from this machine. */
export type DraftRoute = {
  id: string;
  method: HttpMethod;
  /** As called from outside, relative to entryPoint. */
  path: string;
  /** e.g. http://localhost:8000; null when no published port leads to it. */
  entryPoint: string | null;
  /** The compose service (project/service) that handles it. */
  service: string;
  /** Gateways on the way, outermost first (project/service ids). */
  through: string[];
  servicePath: string;
  handler: string | null;
  source: string;
  framework: string;
  /** What it expects, as Swagger would show it, read from the code; null when it couldn't be read. */
  request?: RequestShape | null;
};

export type ParamShape = { name: string; in: 'path' | 'query'; type: string; example: string; required: boolean };

export type RequestShape = {
  params: ParamShape[];
  /** An example JSON body; null when it takes none or it couldn't be told. */
  body: string | null;
  bodyType: string | null;
  bodySource: string | null;
  notes: string[];
};

/** How much of a service's code could be read: its language, and how many routes were found. */
export type ServiceCode = { service: string; language: string | null; supported: boolean; routes: number };

export type RouteDraft = {
  routes: DraftRoute[];
  notes: string[];
  frameworks: string[];
  code?: ServiceCode[] | null;
  /** The languages routes can be read for. */
  languages?: string[] | null;
};

export type DraftResponse = { projects: ProjectReadResult[]; draft: DraftResult | null; routes?: RouteDraft | null };

export type LoadRequest = { name: string; method: HttpMethod; path: string; body: string | null; share: number };

/** The request that logs in, and where its response keeps the token. */
export type LoginRequest = {
  method: HttpMethod;
  path: string;
  body: string | null;
  /** auto: look in the usual places; body: the response body is the token; field: at tokenField. */
  tokenFrom: 'auto' | 'body' | 'field';
  tokenField: string | null;
};

/** How the requests say who they are: log in first, or send a token the user already has. */
export type Auth = { type: 'login' | 'token'; headerName: string; headerPrefix: string; login: LoginRequest | null };

export type LoadScript = { baseUrl: string; auth: Auth | null; requests: LoadRequest[] };

export type SpecLoad = {
  script: string;
  users: number[];
  duration: string;
  env: Record<string, string> | null;
  envPassthrough: string[] | null;
};

export type SpecComponent = {
  name: string;
  project: string;
  composeService: string;
  readinessPort: number | null;
  readinessSource: string | null;
  hasHealthcheck: boolean;
  instances: number;
};

export type SpecDocument = {
  name: string;
  composeProjects: string[];
  services: SpecComponent[];
  infrastructure: SpecComponent[];
  projectServices: Record<string, string[]>;
  operations: Record<string, string[]>;
  load: SpecLoad | null;
};

export type SaveResult = {
  path: string;
  backup: string | null;
  scriptPath: string | null;
  scriptBackup: string | null;
};

export type TryResult = {
  name: string;
  method: string;
  url: string;
  status: number | null;
  milliseconds: number;
  snippet: string | null;
  error: string | null;
  ok: boolean;
};

export type TryResponse = { login: TryResult | null; tokenFound: boolean | null; requests: TryResult[] };

/** Where the token was found: a field of the JSON response (a.b.0.c), or null for the response body itself. */
export type FoundToken = { value: string; field: string | null };

/** The login request sent once, as Postman shows a response. */
export type LoginTry = {
  result: TryResult;
  body: string | null;
  contentType: string | null;
  token: FoundToken | null;
  setsCookie: boolean;
};

/** A chosen compose project, as started: its files, the -p name if one was given, the name Compose uses. */
export type AppProject = { directory: string; files: string[]; name: string | null; composeName: string };

export type AppProjectStatus = {
  name: string;
  state: 'running' | 'partly' | 'stopped' | 'missing';
  running: number;
  notRunning: number;
  status: string;
};

export type AppStatus = {
  projects: AppProjectStatus[];
  running: boolean;
  baseUrl: string | null;
  /** What the app's address answered; null when nothing did. */
  baseStatus: number | null;
  /** How to start it by hand, one command per project. */
  commands: string[];
};

export type StartStatus = { state: 'idle' | 'starting' | 'started' | 'failed'; lines: { n: number; text: string }[]; next: number };

/** What goes into observability/.env (the key is only ever sent, never shown). */
export type MonitoringSettings = {
  appNetwork: string;
  traces: boolean;
  honeycombKey: string | null;
  scrapeDir: string | null;
  namespace: string | null;
};

export type ScrapeTarget = { job: string; url: string; health: string; error: string | null };

export type MonitoringStatus = {
  envExists: boolean;
  settings: MonitoringSettings | null;
  hasKey: boolean;
  prometheusUp: boolean;
  /** The networks the running Prometheus is on. */
  joined: string[];
  targets: ScrapeTarget[];
  networks: string[];
  envPath: string;
  command: string;
};

/** wait: still starting, the check runs again by itself. */
export type PreflightCheck = { key: string; label: string; status: 'ok' | 'wait' | 'warn' | 'fail'; detail: string };

export type PreflightResult = { checks: PreflightCheck[]; faultPlan: string | null; canStart: boolean };

export type RunState = 'idle' | 'running' | 'stopping' | 'finished' | 'failed' | 'stopped';

export type RunLine = { n: number; text: string; error: boolean };

export type RunStatus = {
  state: RunState;
  specPath: string | null;
  faultInjection: boolean;
  outDir: string | null;
  startedAt: string | null;
  endedAt: string | null;
  exitCode: number | null;
  lines: RunLine[];
  next: number;
  results: string[];
};

export type ResultKind = 'single' | 'sweep';

/** A finished analysis found on disk: a single run, or a load sweep over several user counts. */
export type ResultSummary = {
  folder: string;
  kind: ResultKind;
  system: string | null;
  /** When the report was written: the end of that run. */
  writtenAt: string;
  /** A sweep's user counts. */
  levels: number[];
  /** A later run in the same folder started then, and didn't finish. */
  unfinishedAfter: string | null;
  /** This session's run wrote it, and nothing it reads changed since. */
  current: boolean;
  /** Its spec was saved again after it, at this time. */
  specSavedAt: string | null;
};

/** otherRun: written by a different run than the report next to it. */
export type ResultText = { name: string; text: string; writtenAt: string; otherRun: boolean };

export type ResultChart = { name: string; writtenAt: string; otherRun: boolean };

/** A single run, one level of a sweep (users), or a sweep's comparison across levels. */
export type ResultPart = { folder: string; users: number | null; texts: ResultText[]; charts: ResultChart[] };

export type ResultView = { summary: ResultSummary; overview: ResultPart | null; parts: ResultPart[] };

export type ResultList = { results: ResultSummary[]; searched: string[] };
