import {
  Alert,
  Autocomplete,
  Badge,
  Button,
  Checkbox,
  Code,
  Group,
  List,
  Modal,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
  ThemeIcon,
  Title,
  Tooltip,
} from '@mantine/core';
import { IconPlayerPlay, IconPlus, IconRoute } from '@tabler/icons-react';
import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api';
import { AppStatusPanel } from '../components/AppStatusPanel';
import { AuthPanel } from '../components/AuthPanel';
import { KINDS } from '../kinds';
import { appProjects, type WizardState } from '../model';
import {
  HTTP_METHODS,
  componentOf,
  errorMeaning,
  featureList,
  loginGuess,
  routeProblems,
  selectedRoutes,
  toLoadScript,
  triedResult,
  visibleRoutes,
  type AuthSettings,
  type RouteChoice,
} from '../routes';
import type { DraftComponent, HttpMethod, TryResult } from '../types';

type Props = {
  state: WizardState;
  setState: (update: (s: WizardState) => WizardState) => void;
};

const METHOD_COLOR: Record<HttpMethod, string> = {
  GET: 'blue',
  POST: 'green',
  PUT: 'orange',
  PATCH: 'grape',
  DELETE: 'red',
};

export function RoutesStep({ state, setState }: Props) {
  const tried = state.tried;
  const [appReady, setAppReady] = useState(false);
  const generated = state.load.script === 'generate';
  const visible = visibleRoutes(state.routes, state.components);
  const picked = selectedRoutes(state.routes, state.components);
  const hidden = state.routes.length - visible.length;
  const found = state.read?.routes;
  const features = featureList(picked).map((f) => f.name);

  const change = (id: string, patch: Partial<RouteChoice>) =>
    setState((s) => ({ ...s, routes: s.routes.map((r) => (r.id === id ? { ...r, ...patch } : r)) }));
  const setAuth = (patch: Partial<AuthSettings>) => setState((s) => ({ ...s, auth: { ...s.auth, ...patch } }));

  const attempt = useMutation({
    mutationFn: () => api.tryRoutes(toLoadScript(state.baseUrl, state.auth, picked), state.secret),
    onSuccess: (response) => setState((s) => ({ ...s, tried: { ids: picked.map((r) => r.id), response } })),
  });
  const resultFor = (id: string): TryResult | undefined => triedResult(tried, id);

  // group by the component that handles each route
  const groups = new Map<string, { component: DraftComponent; routes: RouteChoice[] }>();
  for (const r of visible) {
    const c = componentOf(r.service, state.components)!;
    const g = groups.get(c.id) ?? { component: c, routes: [] };
    g.routes.push(r);
    groups.set(c.id, g);
  }

  return (
    <Stack gap="lg">
      <Text>
        Pick the requests the load test sends. Each one belongs to a <b>feature</b>: something a user does, like
        reading a blog. The next step says which components each feature needs.
      </Text>

      <Paper withBorder p="md">
        <Title order={5} mb="xs">
          How the load test is made
        </Title>
        <SegmentedControl
          value={state.load.script}
          onChange={(v) => setState((s) => ({ ...s, load: { ...s.load, script: v as 'generate' | 'own' } }))}
          data={[
            { value: 'generate', label: 'Write it from the routes below' },
            { value: 'own', label: 'Use my own k6 script' },
          ]}
        />
        {generated ? (
          <TextInput
            mt="sm"
            label="Your app's address"
            description="Where the load test sends requests, from this machine: usually your gateway's published port."
            placeholder="http://localhost:8000"
            value={state.baseUrl}
            onChange={(e) => {
              const baseUrl = e.currentTarget.value;
              setState((s) => ({ ...s, baseUrl }));
            }}
          />
        ) : (
          <TextInput
            mt="sm"
            label="Your k6 script"
            description="Its full path, or a path relative to the folder you save the spec in. Its requests are yours; the routes below still say which features there are."
            placeholder="/home/me/shop/perf/load.js"
            value={state.load.ownScript}
            onChange={(e) => {
              const ownScript = e.currentTarget.value;
              setState((s) => ({ ...s, load: { ...s.load, ownScript } }));
            }}
          />
        )}
      </Paper>

      <AppStatusPanel
        projects={appProjects(state)}
        baseUrl={generated && state.baseUrl.trim() ? state.baseUrl.trim() : null}
        onReady={setAppReady}
      />

      {generated && (
        <AuthPanel
          auth={state.auth}
          secret={state.secret}
          baseUrl={state.baseUrl}
          loginGuess={loginGuess(state.routes)}
          onChange={setAuth}
          onSecret={(secret) => setState((s) => ({ ...s, secret }))}
        />
      )}

      <UnreadCode state={state} />
      {(found?.notes.length ?? 0) > 0 && (
        <Alert color="gray" title="Read with care">
          {found!.notes.map((n) => (
            <Text key={n} size="sm">
              {n}
            </Text>
          ))}
        </Alert>
      )}

      {[...groups.values()].map(({ component, routes }) => {
        const kind = KINDS[component.kind];
        return (
          <Paper
            key={component.id}
            withBorder
            p="md"
            data-component={component.name}
            style={{ borderLeft: `4px solid var(--mantine-color-${kind.color}-6)` }}
          >
            <Group gap="xs" mb="xs">
              <ThemeIcon color={kind.color} variant="light">
                <IconRoute size={18} />
              </ThemeIcon>
              <Title order={4}>{component.name}</Title>
              <Text c="dimmed" size="sm">
                {routes.filter((r) => r.selected).length} of {routes.length} picked
              </Text>
            </Group>
            <Table verticalSpacing="xs" layout="fixed">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th w={44} />
                  <Table.Th>Request</Table.Th>
                  <Table.Th w={200}>Feature</Table.Th>
                  {generated && <Table.Th w={90}>Share</Table.Th>}
                  {generated && <Table.Th w={130}>Last try</Table.Th>}
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {routes.map((r) => (
                  <RouteRow
                    key={r.id}
                    route={r}
                    generated={generated}
                    features={features}
                    result={resultFor(r.id)}
                    onChange={(patch) => change(r.id, patch)}
                  />
                ))}
              </Table.Tbody>
            </Table>
          </Paper>
        );
      })}
      {hidden > 0 && (
        <Text size="sm" c="dimmed">
          {hidden} route{hidden === 1 ? ' of a component' : 's of components'} left out of the analysis {hidden === 1 ? "isn't" : "aren't"} shown.
        </Text>
      )}

      <AddRoute
        services={state.components.filter((c) => c.kind === 'service')}
        onAdd={(route) => setState((s) => ({ ...s, routes: [...s.routes, route] }))}
      />

      {generated && (
        <Paper withBorder p="md">
          <Group justify="space-between" wrap="nowrap">
            <div>
              <Title order={5}>Try them</Title>
              <Text size="sm" c="dimmed">
                Sends one request to each picked route{state.auth.kind === 'login' ? ', after logging in' : ''}, to catch a
                wrong path or body before a long run. Routes that change data change it once.
              </Text>
            </div>
            <Button
              leftSection={<IconPlayerPlay size={16} />}
              variant="light"
              loading={attempt.isPending}
              disabled={picked.length === 0 || !appReady}
              onClick={() => attempt.mutate()}
            >
              Try the {picked.length} picked route{picked.length === 1 ? '' : 's'}
            </Button>
          </Group>
          {!appReady && (
            <Text size="sm" c="orange" mt="sm">
              Start your app first: see the top of this page.
            </Text>
          )}
          {attempt.isError && (
            <Alert color="red" mt="sm">
              {attempt.error.message}
            </Alert>
          )}
          {tried?.response.login && !(tried.response.login.ok && tried.response.tokenFound) && (
            <Alert color="red" mt="sm" title="The login didn't work">
              So the routes were sent without a token. Send the login request in Authorization above to see why.
            </Alert>
          )}
          {tried && (
            <Text size="sm" mt="sm">
              {tried.response.requests.filter((r) => r.ok).length} of {tried.response.requests.length} answered
              2xx/3xx. The results are on each row.
            </Text>
          )}
        </Paper>
      )}
    </Stack>
  );
}

type RowProps = {
  route: RouteChoice;
  generated: boolean;
  features: string[];
  result: TryResult | undefined;
  onChange: (patch: Partial<RouteChoice>) => void;
};

function RouteRow({ route: r, generated, features, result, onChange }: RowProps) {
  const problems = r.selected ? routeProblems(r) : [];
  const hasBody = r.method !== 'GET' && r.method !== 'DELETE';
  return (
    <Table.Tr>
      <Table.Td style={{ verticalAlign: 'top' }}>
        <Checkbox
          mt={6}
          aria-label={`test ${r.method} ${r.path}`}
          checked={r.selected}
          onChange={(e) => onChange({ selected: e.currentTarget.checked })}
        />
      </Table.Td>
      <Table.Td style={{ verticalAlign: 'top' }}>
        <Group gap="xs" wrap="nowrap" align="flex-start">
          <Badge color={METHOD_COLOR[r.method]} variant="light" w={64} mt={6} style={{ flexShrink: 0 }}>
            {r.method}
          </Badge>
          <Stack gap={4} style={{ flex: 1, minWidth: 0 }}>
            <TextInput
              size="xs"
              aria-label={`path of ${r.id}`}
              value={r.path}
              disabled={!r.selected}
              error={problems.length > 0 ? problems.join(' ') : undefined}
              onChange={(e) => onChange({ path: e.currentTarget.value })}
              styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)' } }}
            />
            {r.selected && generated && hasBody && (
              <Textarea
                size="xs"
                aria-label={`body of ${r.id}`}
                autosize
                minRows={1}
                maxRows={8}
                placeholder="JSON body"
                value={r.body}
                onChange={(e) => onChange({ body: e.currentTarget.value })}
                styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)' } }}
              />
            )}
            <Text size="xs" c="dimmed">
              {r.source ?? 'added by hand'}
              {!r.selected && r.note ? ` · ${r.note}` : ''}
            </Text>
            {r.selected &&
              (r.hints ?? []).map((h) => (
                <Text key={h} size="xs" c="dimmed">
                  {h}
                </Text>
              ))}
          </Stack>
        </Group>
      </Table.Td>
      <Table.Td style={{ verticalAlign: 'top' }}>
        <Autocomplete
          size="xs"
          aria-label={`feature of ${r.id}`}
          data={features.filter((f) => f !== r.feature)}
          value={r.feature}
          disabled={!r.selected}
          onChange={(feature) => onChange({ feature })}
        />
      </Table.Td>
      {generated && (
        <Table.Td style={{ verticalAlign: 'top' }}>
          <NumberInput
            size="xs"
            aria-label={`share of ${r.id}`}
            min={0}
            value={r.share}
            disabled={!r.selected}
            onChange={(v) => onChange({ share: typeof v === 'number' ? v : Number(v) || 0 })}
          />
        </Table.Td>
      )}
      {generated && (
        <Table.Td style={{ verticalAlign: 'top' }}>{result && <ResultBadge result={result} />}</Table.Td>
      )}
    </Table.Tr>
  );
}

/**
 * Services whose routes couldn't be found: in a language this can't read, or
 * one it reads but where no route was recognised. Says so, and what to do.
 */
function UnreadCode({ state }: { state: WizardState }) {
  const found = state.read?.routes;
  const unread = (found?.code ?? []).filter(
    (c) => c.routes === 0 && componentOf(c.service, state.components)?.kind === 'service',
  );
  if (!found || unread.length === 0) return null;
  const languages = found.languages ?? [];
  return (
    <Alert color="orange" title="Some services' routes couldn't be found" data-unread-code>
      <Stack gap={6}>
        {unread.map((c) => {
          const name = componentOf(c.service, state.components)?.name ?? c.service;
          return (
            <Text key={c.service} size="sm">
              {!c.language ? (
                <>
                  <b>{name}</b>: we couldn't tell what it is written in, so its code couldn't be read.
                </>
              ) : !c.supported ? (
                <>
                  Sorry: <b>{name}</b> is written in <b>{c.language}</b>, which this app can't read yet, so its routes
                  couldn't be found.
                </>
              ) : (
                <>
                  <b>{name}</b> is written in {c.language}, but no HTTP route was recognised in its code. It may serve
                  none (a queue consumer, say: then there's nothing to add), or use a framework this app doesn't know
                  yet.
                </>
              )}
            </Text>
          );
        })}
        <Text size="sm">
          Please fill in what's missing yourself: add {unread.length === 1 ? 'its' : 'their'} routes under{' '}
          <b>Something missing?</b> below, or write your own k6 script and choose{' '}
          <b>Use my own k6 script</b> above.
        </Text>
        {languages.length > 0 && (
          <Text size="xs" c="dimmed">
            Routes can be read for: {languages.join(', ')}.
          </Text>
        )}
      </Stack>
    </Alert>
  );
}

function ResultBadge({ result }: { result: TryResult }) {
  const label = result.status === null ? 'no answer' : `${result.status} · ${Math.round(result.milliseconds)} ms`;
  const detail = result.error ?? result.snippet ?? 'empty answer';
  return (
    <Tooltip label={detail} multiline w={360} withArrow>
      <Badge color={result.ok ? 'teal' : 'red'} variant="light" mt={6} style={{ cursor: 'help' }}>
        {label}
      </Badge>
    </Tooltip>
  );
}

function AddRoute({ services, onAdd }: { services: DraftComponent[]; onAdd: (r: RouteChoice) => void }) {
  const [method, setMethod] = useState<HttpMethod>('GET');
  const [path, setPath] = useState('');
  const [service, setService] = useState<string | null>(services[0]?.id ?? null);
  const add = () => {
    if (!service || !path.trim()) return;
    onAdd({
      id: `manual:${method} ${path.trim()}:${Date.now()}`,
      method,
      path: path.trim(),
      body: method === 'GET' || method === 'DELETE' ? '' : '{}',
      share: 1,
      selected: true,
      feature:
        `${method.toLowerCase()}_${path.trim().replace(/[{}:]/g, '').replace(/[^A-Za-z0-9]+/g, '_')}`.replace(
          /_+$/,
          '',
        ),
      service,
      through: [],
      source: null,
      note: null,
    });
    setPath('');
  };
  return (
    <Paper withBorder p="md">
      <Title order={5}>Something missing?</Title>
      <Text size="sm" c="dimmed" mb="xs">
        Add a route the code search didn't find. Name the service that handles it: its feature then starts with what
        that service needs.
      </Text>
      <Group align="flex-end" wrap="nowrap">
        <Select w={110} label="Method" data={HTTP_METHODS} value={method} allowDeselect={false}
          onChange={(v) => setMethod((v as HttpMethod) ?? 'GET')} />
        <TextInput flex={1} label="Path" placeholder="/api/orders" value={path}
          onChange={(e) => setPath(e.currentTarget.value)}
          onKeyDown={(e) => e.key === 'Enter' && add()} />
        <Select w={220} label="Handled by" data={services.map((s) => ({ value: s.id, label: s.name }))}
          value={service} onChange={setService} />
        <Button variant="default" leftSection={<IconPlus size={16} />} disabled={!service || !path.trim()} onClick={add}>
          Add
        </Button>
      </Group>
    </Paper>
  );
}

type ConfirmProps = {
  errors: { route: RouteChoice; result: TryResult }[];
  opened: boolean;
  onStay: () => void;
  onContinue: () => void;
};

/** Asked on Next when picked routes answered errors when tried: the run would count them as failures. */
export function RouteErrorsConfirm({ errors, opened, onStay, onContinue }: ConfirmProps) {
  const login = errors.some((e) => e.result.status === 401 || e.result.status === 403);
  const failed = errors.some((e) => (e.result.status ?? 0) >= 500);
  return (
    <Modal opened={opened} onClose={onStay} title="Some routes answered with errors" size="lg" centered>
      <Stack gap="sm">
        <Text size="sm">
          When you tried them, {errors.length === 1 ? 'this route' : `these ${errors.length} routes`} didn&apos;t work.
          In the load test {errors.length === 1 ? 'it' : 'they'} would fail every time too, and that makes the results
          wrong: failed requests are cheap, so the app looks less busy than it is, and a run where many requests fail
          is marked as not to be trusted.
        </Text>
        <Stack gap={6}>
          {errors.map(({ route, result }) => (
            <Group key={route.id} gap="xs" wrap="nowrap" align="flex-start">
              <Badge color={METHOD_COLOR[route.method]} variant="light" style={{ flexShrink: 0 }}>
                {route.method}
              </Badge>
              <Code style={{ wordBreak: 'break-all' }}>{route.path}</Code>
              <Badge color="red" variant="light" style={{ flexShrink: 0 }}>
                {result.status}
              </Badge>
              <Text size="sm" c="dimmed">
                {errorMeaning(result.status ?? 500)}
              </Text>
            </Group>
          ))}
        </Stack>
        <Text size="sm" fw={600}>
          To fix it:
        </Text>
        <List size="sm" spacing={4}>
          {login && <List.Item>set up the login in Authorization on this page, then press Try again;</List.Item>}
          {failed && (
            <List.Item>
              for a 5xx, the app&apos;s logs say why. A missing login can show up as a 5xx too, and so can an example
              value the app doesn&apos;t accept (a body or an id that doesn&apos;t exist);
            </List.Item>
          )}
          <List.Item>or untick the routes that can&apos;t work as they are;</List.Item>
          <List.Item>
            unless you expect these errors (you&apos;re testing how the app handles bad requests, for example): then
            press Continue anyway.
          </List.Item>
        </List>
        <Group justify="flex-end" mt="xs">
          <Button variant="default" onClick={onContinue}>
            Continue anyway
          </Button>
          <Button onClick={onStay} data-autofocus>
            Go back and fix them
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
