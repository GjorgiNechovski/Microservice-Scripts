import { initialState, withRead, type WizardState } from '../model';
import type { DraftComponent, DraftResponse, DraftRoute, HttpMethod } from '../types';

export function component(patch: Partial<DraftComponent> & Pick<DraftComponent, 'id'>): DraftComponent {
  const [project, service] = patch.id.split('/');
  return {
    name: service,
    project,
    composeService: service,
    kind: 'service',
    reasons: [],
    image: null,
    builtFromSource: true,
    language: null,
    hasHealthcheck: false,
    healthcheckSource: null,
    restart: null,
    replicas: null,
    readinessPort: 8000,
    readinessSource: 'Dockerfile: uvicorn --port 8000',
    instances: 1,
    instancesReason: null,
    dependsOn: [],
    folded: [],
    networks: [],
    ...patch,
  };
}

/** A small app: a service, a database with a replica folded in, and one image nobody knows. */
export function draftedState(): WizardState {
  const db2 = component({ id: 'shop/db-2', kind: 'infrastructure', image: 'postgres:16', readinessPort: 5432 });
  const components = [
    component({ id: 'shop/api' }),
    component({
      id: 'shop/db',
      kind: 'infrastructure',
      image: 'postgres:16',
      hasHealthcheck: true,
      instances: 2,
      instancesReason: '2 copies: db, db-2',
      folded: [
        {
          id: 'shop/db-2',
          role: 'replica',
          reason: 'another copy of db (same image, named db-2): counted as one more instance',
          confirmed: true,
        },
      ],
    }),
    component({ id: 'shop/billing', kind: 'unsure', image: 'myorg/billing:1.4', builtFromSource: false }),
  ];
  const read: DraftResponse = {
    projects: [{ directory: '/repos/shop', files: ['/repos/shop/compose.yaml'], name: 'shop', ok: true, error: null, services: 4 }],
    draft: {
      components,
      services: Object.fromEntries([...components, db2].map((c) => [c.id, { ...c, folded: [], instances: 1 }])),
      networks: [{ name: 'shop_default', projects: ['shop'], components: 3 }],
      projectServices: { shop: ['api', 'db', 'db-2', 'billing'] },
      notes: [],
    },
  };
  return { ...initialState, read, components, specName: 'Shop', saveDirectory: '/repos/shop/perf', fileName: 'shop.toml' };
}

function route(method: HttpMethod, path: string, service: string, handler: string, servicePath = path.replace(/^\/\w+/, '')): DraftRoute {
  return {
    id: `${service} ${method} ${servicePath}`,
    method,
    path,
    entryPoint: 'http://localhost:8080',
    service,
    through: ['shop/gateway'],
    servicePath,
    handler,
    source: `app.py:${path.length}`,
    framework: 'FastAPI / Flask',
  };
}

/**
 * The same app read with its routes: a gateway in front, the api needing the
 * database through its replica's address, and a route of the unsure billing.
 */
export function routedState(): WizardState {
  const base = draftedState();
  const gateway = component({ id: 'shop/gateway', kind: 'infrastructure', image: 'nginx:1.27', builtFromSource: false });
  const components = [
    ...base.components.map((c) => (c.id === 'shop/api' ? { ...c, dependsOn: [{ id: 'shop/db-2', via: 'DB_HOST' }] } : c)),
    gateway,
  ];
  const read: DraftResponse = {
    ...base.read!,
    draft: { ...base.read!.draft!, components },
    routes: {
      routes: [
        route('GET', '/api/books', 'shop/api', 'list_books'),
        route('GET', '/api/books/{book_id}', 'shop/api', 'get_book'),
        route('POST', '/api/orders', 'shop/api', 'place_order'),
        route('GET', '/api/health', 'shop/api', 'health'),
        route('GET', '/billing/invoices', 'shop/billing', 'invoices'),
      ],
      notes: [],
      frameworks: ['FastAPI / Flask'],
    },
  };
  return withRead(initialState, read);
}
