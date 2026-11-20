import { describe, expect, it } from 'vitest';
import { STEP, routesBlockers, setKind, settle, stepStatus, toScript, toSpecDocument } from './model';
import {
  estimatedMinutes,
  routeDefaults,
  withExamples,
  featureComponents,
  renameFeature,
  selectedRoutes,
  suggestComponents,
  triedErrors,
  visibleRoutes,
} from './routes';
import type { TryResult } from './types';
import { parseUsers } from './steps/UsersStep';
import { routedState } from './test/fixtures';

describe('the routes found in the code', () => {
  it('start with the reading routes picked, and say why the rest wait', () => {
    const state = routedState();
    expect(state.baseUrl).toBe('http://localhost:8080');
    const byPath = Object.fromEntries(state.routes.map((r) => [`${r.method} ${r.path}`, r]));
    expect(byPath['GET /api/books']).toMatchObject({ selected: true, feature: 'list_books', note: null });
    expect(byPath['GET /api/books/{book_id}']).toMatchObject({ selected: false, note: 'Needs a real value for {book_id}.' });
    expect(byPath['POST /api/orders']).toMatchObject({ selected: false, body: '{}' });
    expect(byPath['POST /api/orders'].note).toMatch(/changes data/);
    expect(byPath['GET /api/health'].note).toBe('A health check, not a user request.');
  });

  it('hide the routes of components left out, until they are placed', () => {
    const state = routedState();
    expect(visibleRoutes(state.routes, state.components).map((r) => r.path)).not.toContain('/billing/invoices');
    const placed = setKind(state.components, 'shop/billing', 'service');
    expect(visibleRoutes(state.routes, placed).map((r) => r.path)).toContain('/billing/invoices');
  });

  it('need real values and a valid body before going on', () => {
    let state = routedState();
    expect(routesBlockers(state)).toEqual([]);
    const pick = (path: string, patch: object) =>
      (state = { ...state, routes: state.routes.map((r) => (r.path === path ? { ...r, ...patch } : r)) });
    pick('/api/books/{book_id}', { selected: true });
    pick('/api/orders', { selected: true, body: '{ "book_id": ' });
    expect(routesBlockers(state)).toEqual(['Fix the 2 routes marked in red.']);
    pick('/api/books/{book_id}', { path: '/api/books/7' });
    pick('/api/orders', { body: '{ "book_id": 7 }' });
    expect(routesBlockers(state)).toEqual([]);
    expect(routesBlockers({ ...state, baseUrl: 'localhost:8080' })).toEqual([
      'Give the address the load test sends requests to (http://…).',
    ]);
    expect(stepStatus(STEP.routes, state, 'ready').message).toBe(
      '3 routes in 3 features. Go on to check what each feature needs.',
    );
  });
});

describe('the example request from the code', () => {
  const request = {
    params: [
      { name: 'blog_id', in: 'path' as const, type: 'int', example: '1', required: true },
      { name: 'notify', in: 'query' as const, type: 'bool', example: 'true', required: true },
      { name: 'page', in: 'query' as const, type: 'int', example: '1', required: false },
    ],
    body: '{\n  "title": "string"\n}',
    bodyType: 'CreateBlogDto',
    bodySource: 'Dtos.py:10',
    notes: ['{blog_id} is 1 as a placeholder: use one that exists in your data.'],
  };

  it('fills the path and the required query parameters, as Swagger does', () => {
    expect(withExamples('/blog/{blog_id}/like', request)).toBe('/blog/1/like?notify=true');
    expect(withExamples('/x?a=b', { ...request, params: [request.params[1]] })).toBe('/x?a=b&notify=true');
    expect(withExamples('/blog/{blog_id}', null)).toBe('/blog/{blog_id}');
  });

  it('starts routes with the example body, and says where it came from', () => {
    const [post, get] = routeDefaults(
      [
        { id: 'a', method: 'POST', path: '/blog/{blog_id}/like', entryPoint: 'http://localhost:8000', service: 's/blog',
          through: [], servicePath: '/{blog_id}/like', handler: 'like', source: 'main.py:3', framework: 'FastAPI', request },
        { id: 'b', method: 'GET', path: '/blog/{blog_id}', entryPoint: 'http://localhost:8000', service: 's/blog',
          through: [], servicePath: '/{blog_id}', handler: 'read', source: 'main.py:9', framework: 'FastAPI',
          request: { ...request, body: null, bodyType: null } },
      ],
      'http://localhost:8000',
    );
    expect(post).toMatchObject({ path: '/blog/1/like?notify=true', body: request.body, selected: false });
    expect(post.hints).toEqual(['Example body from CreateBlogDto (Dtos.py:10).', request.notes[0]]);
    // a read with its id filled in can be tried at once
    expect(get).toMatchObject({ path: '/blog/1?notify=true', selected: true, note: null });
  });
});

describe('what each feature needs', () => {
  it('follows the request: the gateway, the service, and what it depends on', () => {
    const state = routedState();
    const books = state.routes.find((r) => r.path === '/api/books')!;
    expect(suggestComponents(books, state.components, state.read!.draft!.services)).toEqual([
      { id: 'shop/gateway', reason: 'the request passes through it' },
      { id: 'shop/api', reason: 'it handles the request' },
      { id: 'shop/db', reason: 'api needs it (DB_HOST)' },
    ]);
  });

  it("doesn't take a gateway's other upstreams for the feature's needs", () => {
    // nginx in front of two services usually lists both in depends_on
    const state = routedState();
    const components = setKind(state.components, 'shop/billing', 'service').map((c) =>
      c.id === 'shop/gateway' ? { ...c, dependsOn: [{ id: 'shop/billing', via: 'depends_on' }] } : c,
    );
    const books = state.routes.find((r) => r.path === '/api/books')!;
    expect(suggestComponents(books, components, state.read!.draft!.services).map((s) => s.id)).toEqual([
      'shop/gateway',
      'shop/api',
      'shop/db',
    ]);
  });

  it("keeps the user's choice, and carries it over a rename", () => {
    const state = routedState();
    const services = state.read!.draft!.services;
    const edited = { list_books: ['shop/api'] };
    expect(featureComponents('list_books', state.routes, edited, state.components, services)).toEqual(['shop/api']);
    const renamed = renameFeature(state.routes, edited, 'list_books', 'browse');
    expect(renamed.features).toEqual({ browse: ['shop/api'] });
    expect(renamed.routes.find((r) => r.path === '/api/books')?.feature).toBe('browse');
  });

  it('go into the spec as operations, by component name', () => {
    const state = routedState();
    const spec = toSpecDocument(state);
    expect(spec.operations).toEqual({ list_books: ['gateway', 'api', 'db'] });
    expect(spec.load).toEqual({
      script: 'load.js',
      users: [1, 2, 5, 10],
      duration: '2m',
      env: { BASE_URL: 'http://localhost:8080', THINK_TIME: '0' },
      envPassthrough: null,
    });
  });
});

describe('the load test', () => {
  it('logs in with the password left to the environment', () => {
    const state = routedState();
    state.auth = { ...state.auth, kind: 'login', path: '/api/login', tokenFrom: 'body' };
    state.auth.fields = [
      { key: 'email', value: 'test@example.com', secret: false },
      { key: 'password', value: 'ignored', secret: true },
    ];
    expect(toScript(state)?.auth).toEqual({
      type: 'login',
      headerName: 'Authorization',
      headerPrefix: 'Bearer ',
      login: {
        method: 'POST',
        path: '/api/login',
        body: '{\n  "email": "test@example.com",\n  "password": "{{password}}"\n}',
        tokenFrom: 'body',
        tokenField: null,
      },
    });
    expect(toSpecDocument(state).load?.envPassthrough).toEqual(['LOGIN_PASSWORD']);
    expect(toScript({ ...state, load: { ...state.load, script: 'own', ownScript: 'mine.js' } })).toBeNull();
  });

  it('sends a key the user already has, kept out of the files', () => {
    const state = routedState();
    state.auth = { ...state.auth, kind: 'apiKey', headerName: 'X-API-Key', headerPrefix: '' };
    expect(toScript(state)?.auth).toEqual({ type: 'token', headerName: 'X-API-Key', headerPrefix: '', login: null });
    expect(toSpecDocument(state).load?.envPassthrough).toEqual(['AUTH_TOKEN']);
  });

  it('reads the numbers of users smallest first, each once', () => {
    expect(parseUsers(['5', '1', 'x', '5', '0', '2.5', ' 20 '])).toEqual([1, 5, 20]);
    expect(estimatedMinutes({ ...routedState().load, users: [1, 2, 5, 10], duration: '2m' })).toBe(10);
  });

  it('must be saved again after a change, but not for the password', () => {
    const saved = { ...routedState(), savedPath: '/repos/shop/perf/shop.toml' };
    const routes = saved.routes.map((r) => ({ ...r, share: 2 }));
    expect(settle(saved, { ...saved, routes }).savedPath).toBeNull();
    expect(settle(saved, { ...saved, secret: 'x' }).savedPath).toBe(saved.savedPath);
    expect(selectedRoutes(saved.routes, saved.components)).toHaveLength(1);
  });
});

describe('routes that answered errors when tried', () => {
  const answer = (status: number | null): TryResult => ({
    name: 'x',
    method: 'GET',
    url: 'http://localhost:8080/x',
    status,
    milliseconds: 5,
    snippet: null,
    error: status === null ? 'refused' : null,
    ok: status !== null && status < 400,
  });

  it('are the picked ones that got 401, 403 or 5xx', () => {
    const state = routedState();
    state.routes = state.routes.map((r) => ({ ...r, selected: true }));
    const picked = selectedRoutes(state.routes, state.components).slice(0, 3);
    expect(picked).toHaveLength(3);
    const statuses = [401, 404, 503]; // a missing login, a wrong id, the app failing
    const tried = { ids: picked.map((r) => r.id), response: { login: null, tokenFound: null, requests: statuses.map(answer) } };

    expect(triedErrors(tried, picked).map((e) => [e.route.id, e.result.status])).toEqual([
      [picked[0].id, 401],
      [picked[2].id, 503],
    ]);
    // unticked since, or never tried: nothing to ask about
    expect(triedErrors(tried, picked.slice(1)).map((e) => e.result.status)).toEqual([503]);
    expect(triedErrors(null, picked)).toEqual([]);
  });

  it('make the routes step say so', () => {
    const state = routedState();
    const picked = selectedRoutes(state.routes, state.components);
    const ok = stepStatus(STEP.routes, state, 'ready');
    expect(ok.message).toMatch(/Go on to check what each feature needs/);
    const failing = {
      ...state,
      tried: { ids: [picked[0].id], response: { login: null, tokenFound: null, requests: [answer(401)] } },
    };
    expect(stepStatus(STEP.routes, failing, 'ready')).toEqual({
      ready: true,
      message: expect.stringMatching(/but 1 answered with an error when tried/),
    });
  });
});
