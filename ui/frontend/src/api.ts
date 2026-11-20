import type {
  DraftResponse,
  FolderListing,
  Auth,
  LoadScript,
  LoginTry,
  AppProject,
  AppStatus,
  StartStatus,
  MonitoringSettings,
  MonitoringStatus,
  PreflightResult,
  PrerequisiteCheck,
  ResultList,
  ResultView,
  ProjectSelection,
  RunningProject,
  RunStatus,
  SaveResult,
  ScanResult,
  SpecDocument,
  TryResponse,
} from './types';

const TOKEN_KEY = 'ms-setup-token';
let token = '';

/**
 * Takes the launch token from the address the app opened (?token=...), keeps
 * it for this tab, and removes it from the address bar. Without it the
 * backend refuses every call. Returns whether there is one.
 */
export function initToken(): boolean {
  const url = new URL(window.location.href);
  const fromUrl = url.searchParams.get('token');
  if (fromUrl) {
    token = fromUrl;
    try {
      sessionStorage.setItem(TOKEN_KEY, fromUrl);
    } catch {
      // storage blocked: the token still works until the page is reloaded
    }
    url.searchParams.delete('token');
    window.history.replaceState(null, '', url.toString());
    return true;
  }
  try {
    token = sessionStorage.getItem(TOKEN_KEY) ?? '';
  } catch {
    token = '';
  }
  return token !== '';
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function messageOf(status: number, body: unknown): string {
  if (status === 401) {
    return "This page's key is missing or out of date: open the address the app printed when it started.";
  }
  if (body && typeof body === 'object') {
    const b = body as { error?: string; problems?: string[] };
    if (b.error) return b.error;
    if (b.problems?.length) return b.problems.join(' ');
  }
  return `The app answered ${status}.`;
}

async function call<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method,
    headers: {
      'X-Setup-Token': token,
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data: unknown = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new ApiError(messageOf(response.status, data), response.status, data);
  }
  return data as T;
}

export const api = {
  prerequisites: () => call<PrerequisiteCheck[]>('GET', '/prerequisites'),
  folders: (path?: string) =>
    call<FolderListing>('GET', `/folders${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  scan: (folders: string[], extraFiles: string[]) => call<ScanResult>('POST', '/scan', { folders, extraFiles }),
  composeProjects: () => call<RunningProject[]>('GET', '/compose/projects'),
  draft: (projects: ProjectSelection[]) => call<DraftResponse>('POST', '/draft', { projects }),
  preview: (spec: SpecDocument, script: LoadScript | null) =>
    call<{ toml: string; script: string | null }>('POST', '/spec/preview', { spec, script }),
  save: (spec: SpecDocument, directory: string, fileName: string, overwrite: boolean, script: LoadScript | null) =>
    call<SaveResult>('POST', '/spec/save', { spec, directory, fileName, overwrite, script }),
  tryRoutes: (script: LoadScript, secret: string) =>
    call<TryResponse>('POST', '/routes/try', { script, secret: secret || null }),
  tryLogin: (baseUrl: string, auth: Auth, secret: string) =>
    call<LoginTry>('POST', '/login/try', { baseUrl, auth, secret: secret || null }),
  appStatus: (projects: AppProject[], baseUrl: string | null) =>
    call<AppStatus>('POST', '/app/status', { projects, baseUrl }),
  startApp: (projects: AppProject[]) => call<StartStatus>('POST', '/app/start', { projects, baseUrl: null }),
  startProgress: (after: number) => call<StartStatus>('GET', `/app/start?after=${after}`),
  monitoring: () => call<MonitoringStatus>('GET', '/monitoring'),
  monitoringPreview: (settings: MonitoringSettings) =>
    call<{ env: string; problems: string[] }>('POST', '/monitoring/preview', settings),
  startMonitoring: (settings: MonitoringSettings) => call<StartStatus>('POST', '/monitoring/start', settings),
  monitoringProgress: (after: number) => call<StartStatus>('GET', `/monitoring/start?after=${after}`),
  preflight: (specPath: string, baseUrl: string | null) =>
    call<PreflightResult>('POST', '/run/check', { specPath, baseUrl }),
  run: (after: number) => call<RunStatus>('GET', `/run?after=${after}`),
  start: (specPath: string, faultInjection: boolean, secrets: Record<string, string>) =>
    call<RunStatus>('POST', '/run/start', { specPath, faultInjection, secrets }),
  stop: () => call<RunStatus>('POST', '/run/stop'),
  findResults: (folders: string[], specPath: string | null) =>
    call<ResultList>('POST', '/results/find', { folders, specPath }),
  result: (folder: string, specPath: string | null) =>
    call<ResultView>(
      'GET',
      `/results?folder=${encodeURIComponent(folder)}${specPath ? `&spec=${encodeURIComponent(specPath)}` : ''}`,
    ),
  /** A chart as an object URL (images can't send the page's key themselves); revoke it when done. */
  chart: async (folder: string, name: string): Promise<string> => {
    const response = await fetch(
      `/api/results/chart?folder=${encodeURIComponent(folder)}&name=${encodeURIComponent(name)}`,
      { headers: { 'X-Setup-Token': token } },
    );
    if (!response.ok) throw new ApiError(messageOf(response.status, null), response.status, null);
    return URL.createObjectURL(await response.blob());
  },
};
