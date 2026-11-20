// Routes, features and the load test: the rules behind the Routes, Features
// and Users steps, kept free of React so they can be tested on their own.

import type { Auth, DraftComponent, DraftRoute, HttpMethod, LoadScript, RequestShape, TryResponse, TryResult } from './types';

export const HTTP_METHODS: HttpMethod[] = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'];

/** A route as the user has chosen to use it. */
export type RouteChoice = {
  id: string;
  method: HttpMethod;
  /** As the load test sends it: after the base address, or a full address. */
  path: string;
  /** JSON sent with it; empty for none. */
  body: string;
  /** How often it is picked, relative to the others. */
  share: number;
  selected: boolean;
  /** The feature (spec operation) it belongs to. */
  feature: string;
  /** What handles it: a compose service id from the draft, or a component id for one added by hand. */
  service: string;
  /** Gateways on the way, outermost first. */
  through: string[];
  /** Where it was found: file:line and framework; null when added by hand. */
  source: string | null;
  /** Why it isn't picked to begin with. */
  note: string | null;
  /** Where its example came from, and what to know about it (placeholders, values sent every time). */
  hints?: string[];
};

export type AuthKind = 'none' | 'login' | 'bearer' | 'apiKey';

/** One Key/Value row of the login body; a secret row's value is kept apart and never saved. */
export type BodyField = { key: string; value: string; secret: boolean };

/** The Authorization settings, as in Postman's Authorization tab. */
export type AuthSettings = {
  kind: AuthKind;
  /** The login request (kind login). */
  method: HttpMethod;
  path: string;
  bodyMode: 'fields' | 'raw';
  fields: BodyField[];
  raw: string;
  tokenFrom: 'auto' | 'body' | 'field';
  tokenField: string;
  /** The header requests carry the token or key in, and what goes before it. */
  headerName: string;
  headerPrefix: string;
};

export type LoadSettings = {
  /** Virtual users per level: several make a sweep. */
  users: number[];
  /** How long each level is held, in k6's form. */
  duration: string;
  /** Seconds a user pauses between requests; 0 measures capacity. */
  thinkTime: number;
  script: 'generate' | 'own';
  ownScript: string;
};

export const PASSWORD = '{{password}}';

export const initialAuth: AuthSettings = {
  kind: 'none',
  method: 'POST',
  path: '',
  bodyMode: 'fields',
  fields: [
    { key: 'username', value: '', secret: false },
    { key: 'password', value: '', secret: true },
  ],
  raw: '',
  tokenFrom: 'auto',
  tokenField: '',
  headerName: 'Authorization',
  headerPrefix: 'Bearer ',
};

/** The header each kind sends, unless the user changes it. */
export const AUTH_HEADERS: Record<AuthKind, { headerName: string; headerPrefix: string }> = {
  none: { headerName: 'Authorization', headerPrefix: 'Bearer ' },
  login: { headerName: 'Authorization', headerPrefix: 'Bearer ' },
  bearer: { headerName: 'Authorization', headerPrefix: 'Bearer ' },
  apiKey: { headerName: 'X-API-Key', headerPrefix: '' },
};

export const initialLoad: LoadSettings = {
  users: [1, 2, 5, 10],
  duration: '2m',
  thinkTime: 0,
  script: 'generate',
  ownScript: '',
};

const HEALTH = /\/(health|healthz|healthcheck|ready|readiness|readyz|live|liveness|livez|ping|metrics|status|version)\/?$/i;
const LOGIN = /(log-?in|sign-?in|token|authenticate|auth)\/?$/i;
const FEATURE_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
const DURATION = /^(\d+(ms|s|m|h))+$/;

export const isModelled = (c: DraftComponent) => c.kind === 'service' || c.kind === 'infrastructure';

/** The component a compose service is part of: its own, or the one it is folded into. */
export function componentOf(id: string, components: DraftComponent[]): DraftComponent | undefined {
  return components.find((c) => c.id === id || c.folded.some((f) => f.id === id));
}

/** Path parameters still to be filled in: {id} or :id. */
export function placeholders(path: string): string[] {
  return [...path.matchAll(/\{(\w+)\}|(?<=\/):(\w+)/g)].map((m) => m[0]);
}

function featureSlug(text: string): string {
  return (
    text
      .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '') || 'feature'
  );
}

/** The address most routes are reached at: the app's front door. */
export function suggestBaseUrl(routes: DraftRoute[]): string {
  const counts = new Map<string, number>();
  for (const r of routes) {
    if (r.entryPoint) counts.set(r.entryPoint, (counts.get(r.entryPoint) ?? 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? '';
}

/**
 * The routes as found, ready to choose from. Reading routes is safe, so only
 * those are picked to begin with; anything that changes data, checks health
 * or needs a value filled in waits for the user.
 */
export function routeDefaults(found: DraftRoute[], baseUrl: string): RouteChoice[] {
  const byHandler = new Map<string, number>();
  for (const r of found) {
    if (r.handler) byHandler.set(featureSlug(r.handler), (byHandler.get(featureSlug(r.handler)) ?? 0) + 1);
  }
  return found.map((r) => {
    const handler = r.handler ? featureSlug(r.handler) : null;
    const feature =
      handler && byHandler.get(handler) === 1 ? handler : featureSlug(`${r.method} ${r.path.replace(/[{}:]/g, '')}`);
    const path = withExamples(
      !r.entryPoint ? r.servicePath : r.entryPoint === baseUrl ? r.path : `${r.entryPoint}${r.path}`,
      r.request,
    );
    const note = HEALTH.test(r.servicePath)
      ? 'A health check, not a user request.'
      : r.method !== 'GET'
        ? 'It changes data: tick it if the test may do that, and give it a body.'
        : placeholders(path).length > 0
          ? `Needs a real value for ${placeholders(path).join(', ')}.`
          : !r.entryPoint
            ? 'No published port leads to it: write its full address.'
            : null;
    const hints = [
      ...(r.request?.bodyType
        ? [`Example body from ${r.request.bodyType}${r.request.bodySource ? ` (${r.request.bodySource})` : ''}.`]
        : []),
      ...(r.request?.notes ?? []),
    ];
    return {
      id: r.id,
      method: r.method,
      path,
      body: r.request?.body ?? (r.method === 'GET' || r.method === 'DELETE' ? '' : '{}'),
      hints,
      share: 1,
      selected: note === null,
      feature,
      service: r.service,
      through: r.through,
      source: `${r.source} · ${r.framework}`,
      note,
    };
  });
}

/**
 * A path with the code's example values filled in, as Swagger's "Try it out"
 * starts: path parameters replaced, required query parameters added.
 */
export function withExamples(path: string, request: RequestShape | null | undefined): string {
  if (!request) return path;
  let p = path;
  for (const param of request.params.filter((x) => x.in === 'path')) {
    p = p.split(`{${param.name}}`).join(encodeURIComponent(param.example));
  }
  const query = request.params
    .filter((x) => x.in === 'query' && x.required)
    .map((x) => `${encodeURIComponent(x.name)}=${encodeURIComponent(x.example)}`);
  return query.length > 0 ? `${p}${p.includes('?') ? '&' : '?'}${query.join('&')}` : p;
}

/** A login route found among the routes, to start the login settings from. */
export function loginGuess(routes: RouteChoice[]): RouteChoice | undefined {
  return routes.find((r) => r.method === 'POST' && LOGIN.test(r.path));
}

/** Routes handled by a component that is part of the analysis (the rest are hidden). */
export function visibleRoutes(routes: RouteChoice[], components: DraftComponent[]): RouteChoice[] {
  return routes.filter((r) => {
    const c = componentOf(r.service, components);
    return c !== undefined && isModelled(c);
  });
}

export function selectedRoutes(routes: RouteChoice[], components: DraftComponent[]): RouteChoice[] {
  return visibleRoutes(routes, components).filter((r) => r.selected);
}

/** What is wrong with a picked route, in words for the user. */
export function routeProblems(r: RouteChoice): string[] {
  const problems: string[] = [];
  if (!r.path.startsWith('/') && !/^https?:\/\//i.test(r.path)) {
    problems.push('Start the path with / (or write a full http:// address).');
  }
  for (const p of placeholders(r.path)) problems.push(`Put a real value in place of ${p}.`);
  if (r.body.trim()) {
    try {
      JSON.parse(r.body.split(PASSWORD).join('x'));
    } catch {
      problems.push("The body isn't valid JSON.");
    }
  }
  if (!(r.share > 0)) problems.push('Its share must be more than 0.');
  if (!FEATURE_NAME.test(r.feature)) problems.push("Name its feature with letters, digits, '-', '_' and '.'.");
  return problems;
}

/** The features of the picked routes, in the order they first appear. */
export function featureList(routes: RouteChoice[]): { name: string; routes: RouteChoice[] }[] {
  const features: { name: string; routes: RouteChoice[] }[] = [];
  for (const r of routes) {
    const f = features.find((x) => x.name === r.feature);
    if (f) f.routes.push(r);
    else features.push({ name: r.feature, routes: [r] });
  }
  return features;
}

export type Suggestion = { id: string; reason: string };

/**
 * The components a route needs: the gateways it passes, the component that
 * handles it, and everything those depend on (depends_on, or an environment
 * variable naming it), each with the reason.
 */
export function suggestComponents(
  route: RouteChoice,
  components: DraftComponent[],
  services: Record<string, DraftComponent>,
): Suggestion[] {
  const found = new Map<string, string>();
  const queue: DraftComponent[] = [];
  const add = (c: DraftComponent | undefined, reason: string) => {
    if (!c || !isModelled(c) || found.has(c.id)) return;
    found.set(c.id, reason);
    queue.push(c);
  };
  const handler = componentOf(route.service, components);
  // a load balancer in front of the handler alone is part of it, not a gateway of its own
  const inFront = route.through.filter((id) => handler && componentOf(id, components)?.id === handler.id);
  for (const id of route.through.filter((x) => !inFront.includes(x))) {
    const c = componentOf(id, components);
    add(c, c && c.id !== id ? `the request passes through ${id.split('/')[1]}, part of ${c.name}` : `the request passes through it`);
  }
  add(
    handler,
    inFront.length > 0
      ? `it handles the request, behind ${inFront.map((id) => id.split('/')[1]).join(', ')}`
      : 'it handles the request',
  );
  const gateways = new Set(found.keys());
  gateways.delete(handler?.id ?? '');
  while (queue.length > 0) {
    const c = queue.shift()!;
    const deps = [c, ...c.folded.map((f) => services[f.id]).filter(Boolean)].flatMap((x) => x.dependsOn);
    for (const d of deps) {
      const needed = componentOf(d.id, components);
      // a gateway "depends on" the services it routes to; this request goes to one of them only
      if (gateways.has(c.id) && needed?.kind === 'service') continue;
      add(needed, `${c.name} needs it (${d.via})`);
    }
  }
  return [...found.entries()].map(([id, reason]) => ({ id, reason }));
}

/** The components a feature needs: what the user chose, or what its routes suggest. */
export function featureComponents(
  name: string,
  routes: RouteChoice[],
  edited: Record<string, string[]>,
  components: DraftComponent[],
  services: Record<string, DraftComponent>,
): string[] {
  const chosen = edited[name];
  if (chosen) return chosen.filter((id) => components.some((c) => c.id === id && isModelled(c)));
  const ids: string[] = [];
  for (const r of routes.filter((x) => x.feature === name)) {
    for (const s of suggestComponents(r, components, services)) if (!ids.includes(s.id)) ids.push(s.id);
  }
  return ids;
}

/** Renames a feature on all its routes, keeping what the user chose for it. */
export function renameFeature(
  routes: RouteChoice[],
  edited: Record<string, string[]>,
  from: string,
  to: string,
): { routes: RouteChoice[]; features: Record<string, string[]> } {
  const features = { ...edited };
  if (features[from] && from !== to) {
    features[to] = features[from];
    delete features[from];
  }
  return { routes: routes.map((r) => (r.feature === from ? { ...r, feature: to } : r)), features };
}

/** The login body: the Key/Value rows as JSON, the secret row as {{password}}; or the raw JSON. */
export function loginBody(auth: AuthSettings): string {
  if (auth.bodyMode === 'raw') return auth.raw;
  const rows = auth.fields.filter((f) => f.key.trim());
  if (rows.length === 0) return '';
  return JSON.stringify(Object.fromEntries(rows.map((f) => [f.key.trim(), f.secret ? PASSWORD : f.value])), null, 2);
}

export function authProblems(auth: AuthSettings): string[] {
  const problems: string[] = [];
  if (auth.kind === 'login') {
    if (!auth.path.startsWith('/') && !/^https?:\/\//i.test(auth.path)) {
      problems.push('Give the path of the login request, starting with /.');
    }
    const body = loginBody(auth);
    if (body.trim()) {
      try {
        JSON.parse(body.split(PASSWORD).join('x'));
      } catch {
        problems.push("The login request's body isn't valid JSON.");
      }
    }
    if (auth.tokenFrom === 'field' && !auth.tokenField.trim()) problems.push('Click the token in the login response.');
  }
  if (auth.kind !== 'none' && !auth.headerName.trim()) problems.push('Name the header the token goes in.');
  return problems;
}

/** Whether the requests need a secret the app doesn't save: a password, a token or a key. */
export function usesSecret(auth: AuthSettings): boolean {
  if (auth.kind === 'bearer' || auth.kind === 'apiKey') return true;
  return auth.kind === 'login' && loginBody(auth).includes(PASSWORD);
}

/** The environment variable the load test reads the secret from. */
export const secretVariable = (auth: AuthSettings) => (auth.kind === 'login' ? 'LOGIN_PASSWORD' : 'AUTH_TOKEN');

/** What the user calls the secret, for labels. */
export const SECRET_LABEL: Record<AuthKind, string> = {
  none: '',
  login: 'Password',
  bearer: 'Token',
  apiKey: 'API key',
};

/** The Authorization settings as the backend writes them into the script. */
export function toAuth(auth: AuthSettings): Auth | null {
  if (auth.kind === 'none') return null;
  const header = { headerName: auth.headerName.trim(), headerPrefix: auth.headerPrefix };
  if (auth.kind !== 'login') return { type: 'token', ...header, login: null };
  return {
    type: 'login',
    ...header,
    login: {
      method: auth.method,
      path: auth.path.trim(),
      body: loginBody(auth).trim() || null,
      tokenFrom: auth.tokenFrom,
      tokenField: auth.tokenFrom === 'field' ? auth.tokenField.trim() : null,
    },
  };
}

export function validBaseUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === 'http:' || u.protocol === 'https:';
  } catch {
    return false;
  }
}

export function loadProblems(load: LoadSettings): string[] {
  const problems: string[] = [];
  if (load.users.length === 0) problems.push('Give at least one number of users.');
  if (load.users.some((u) => !Number.isInteger(u) || u < 1 || u > 100000)) {
    problems.push('Each number of users is a whole number from 1 to 100000.');
  }
  if (!DURATION.test(load.duration)) problems.push('Write how long each level lasts like 2m, 90s or 1m30s.');
  if (!(load.thinkTime >= 0)) problems.push("The pause can't be negative.");
  return problems;
}

/** Seconds in a k6 duration ("1m30s" is 90). */
export function seconds(duration: string): number {
  const unit: Record<string, number> = { ms: 0.001, s: 1, m: 60, h: 3600 };
  return [...duration.matchAll(/(\d+)(ms|s|m|h)/g)].reduce((sum, m) => sum + Number(m[1]) * unit[m[2]], 0);
}

/** About how long the load test runs: each level, plus the measuring and reporting after it. */
export function estimatedMinutes(load: LoadSettings): number {
  return Math.ceil((load.users.length * (seconds(load.duration) + 30)) / 60);
}

/** The script the backend writes, from the picked routes. */
export function toLoadScript(baseUrl: string, auth: AuthSettings, routes: RouteChoice[]): LoadScript {
  return {
    baseUrl: baseUrl.trim(),
    auth: toAuth(auth),
    requests: routes.map((r) => ({
      name: `${r.method} ${r.path}`,
      method: r.method,
      path: r.path.trim(),
      body: r.body.trim() || null,
      share: r.share,
    })),
  };
}

/** The last Try: which routes were sent, in order, and how each answered. */
export type Tried = { ids: string[]; response: TryResponse };

/** How the last Try answered a route; undefined when it wasn't sent. */
export function triedResult(tried: Tried | null, id: string): TryResult | undefined {
  const i = tried?.ids.indexOf(id) ?? -1;
  return i >= 0 ? tried?.response.requests[i] : undefined;
}

/**
 * The picked routes whose last Try answered 401, 403 or 5xx: sent like that in
 * the load test, every one of them fails, and a run where many requests fail
 * can't be trusted.
 */
export function triedErrors(tried: Tried | null, picked: RouteChoice[]): { route: RouteChoice; result: TryResult }[] {
  return picked
    .map((route) => ({ route, result: triedResult(tried, route.id) }))
    .filter((e): e is { route: RouteChoice; result: TryResult } => {
      const status = e.result?.status;
      return status === 401 || status === 403 || (status !== null && status !== undefined && status >= 500);
    });
}

/** What an error status most likely means here, in a few words. */
export function errorMeaning(status: number): string {
  if (status === 401) return 'needs a login: no valid token was sent';
  if (status === 403) return "the user (or key) isn't allowed to do this";
  return 'the app failed while answering';
}
