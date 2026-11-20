import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Code,
  Group,
  Mark,
  Paper,
  PasswordInput,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  Textarea,
  TextInput,
  Title,
  Tooltip,
  UnstyledButton,
} from '@mantine/core';
import { IconLock, IconLockOpen, IconPlus, IconSend, IconX } from '@tabler/icons-react';
import { useMutation } from '@tanstack/react-query';
import { Fragment, useState, type ReactNode } from 'react';
import { api } from '../api';
import {
  AUTH_HEADERS,
  HTTP_METHODS,
  PASSWORD,
  loginBody,
  secretVariable,
  toAuth,
  type AuthKind,
  type AuthSettings,
  type BodyField,
  type RouteChoice,
} from '../routes';
import type { HttpMethod, LoginTry } from '../types';

type Props = {
  auth: AuthSettings;
  secret: string;
  baseUrl: string;
  /** Routes that look like a login, to start from. */
  loginGuess: RouteChoice | undefined;
  onChange: (patch: Partial<AuthSettings>) => void;
  onSecret: (secret: string) => void;
};

const KINDS: { value: AuthKind; label: string; help: string }[] = [
  { value: 'none', label: 'No auth', help: 'Requests are sent without credentials.' },
  {
    value: 'login',
    label: 'Log in first',
    help: 'The test sends a login request once at the start, then adds the token from its response to every request, the way your frontend does after a user signs in.',
  },
  { value: 'bearer', label: 'Bearer token', help: 'Every request sends a token you already have, as Authorization: Bearer <token>.' },
  { value: 'apiKey', label: 'API key', help: 'Every request sends a key you already have in a header of its own.' },
];

/** The Authorization settings, laid out like Postman's Authorization tab. */
export function AuthPanel({ auth, secret, baseUrl, loginGuess, onChange, onSecret }: Props) {
  const kind = KINDS.find((k) => k.value === auth.kind)!;
  const choose = (value: AuthKind) => {
    const start = value === 'login' && !auth.path && loginGuess ? { path: loginGuess.path } : {};
    onChange({ kind: value, ...AUTH_HEADERS[value], ...start });
    // a password, a token and a key are different secrets
    if (value !== auth.kind) onSecret('');
  };

  return (
    <Paper withBorder p="md">
      <Title order={5}>Authorization</Title>
      <Text size="sm" c="dimmed" mb="sm">
        How the test's requests say who they are, as on Postman's Authorization tab.
      </Text>
      <SegmentedControl
        value={auth.kind}
        onChange={(v) => choose(v as AuthKind)}
        data={KINDS.map((k) => ({ value: k.value, label: k.label }))}
      />
      <Text size="sm" mt="xs">
        {kind.help}
      </Text>

      {auth.kind === 'login' && (
        <LoginRequestEditor auth={auth} secret={secret} baseUrl={baseUrl} onChange={onChange} onSecret={onSecret} />
      )}

      {auth.kind === 'bearer' && (
        <PasswordInput
          mt="sm"
          maw={560}
          label="Token"
          description={`Not saved in any file: the test reads it from ${secretVariable(auth)}.`}
          value={secret}
          onChange={(e) => onSecret(e.currentTarget.value)}
        />
      )}

      {auth.kind === 'apiKey' && (
        <Group mt="sm" align="flex-start" wrap="nowrap">
          <TextInput
            w={220}
            label="Header"
            value={auth.headerName}
            onChange={(e) => onChange({ headerName: e.currentTarget.value })}
          />
          <PasswordInput
            flex={1}
            maw={420}
            label="Key"
            description={`Not saved in any file: the test reads it from ${secretVariable(auth)}.`}
            value={secret}
            onChange={(e) => onSecret(e.currentTarget.value)}
          />
        </Group>
      )}

      {auth.kind !== 'none' && auth.kind !== 'apiKey' && <HeaderRow auth={auth} onChange={onChange} />}
    </Paper>
  );
}

/** What every request then sends, as a Headers row in Postman. */
function HeaderRow({ auth, onChange }: { auth: AuthSettings; onChange: (patch: Partial<AuthSettings>) => void }) {
  const [editing, setEditing] = useState(false);
  return (
    <Group gap="xs" mt="md" align="center">
      <Text size="sm">Every request then sends the header</Text>
      {editing ? (
        <>
          <TextInput
            size="xs"
            w={150}
            aria-label="header name"
            value={auth.headerName}
            onChange={(e) => onChange({ headerName: e.currentTarget.value })}
          />
          <TextInput
            size="xs"
            w={90}
            aria-label="before the token"
            value={auth.headerPrefix}
            onChange={(e) => onChange({ headerPrefix: e.currentTarget.value })}
          />
          <Code>&lt;token&gt;</Code>
          <Button size="compact-xs" variant="subtle" onClick={() => setEditing(false)}>
            Done
          </Button>
        </>
      ) : (
        <>
          <Code>
            {auth.headerName}: {auth.headerPrefix}&lt;token&gt;
          </Code>
          <Button size="compact-xs" variant="subtle" onClick={() => setEditing(true)}>
            Change
          </Button>
        </>
      )}
    </Group>
  );
}

type EditorProps = {
  auth: AuthSettings;
  secret: string;
  baseUrl: string;
  onChange: (patch: Partial<AuthSettings>) => void;
  onSecret: (secret: string) => void;
};

/** Another login request: where its token is gets looked for again. */
const FRESH = { tokenFrom: 'auto', tokenField: '' } as const;

/** The login request, like a request in Postman: method, address, body, Send, response. */
function LoginRequestEditor({ auth, secret, baseUrl, onChange, onSecret }: EditorProps) {
  const send = useMutation({
    mutationFn: () => api.tryLogin(baseUrl, toAuth(auth)!, secret),
    onSuccess: (tried) => {
      // pin where the token was found, so the test reads it from the same place
      if (tried.token) {
        onChange(
          tried.token.field === null
            ? { tokenFrom: 'body', tokenField: '' }
            : { tokenFrom: 'field', tokenField: tried.token.field },
        );
      }
    },
  });
  const setField = (i: number, patch: Partial<BodyField>) =>
    onChange({
      fields: auth.fields.map((f, j) =>
        j === i ? { ...f, ...patch } : patch.secret ? { ...f, secret: false } : f,
      ),
    });

  return (
    <Stack gap="sm" mt="md">
      <Group gap={0} wrap="nowrap" align="flex-start">
        <Select
          w={110}
          aria-label="login method"
          data={HTTP_METHODS}
          value={auth.method}
          allowDeselect={false}
          onChange={(v) => onChange({ method: (v as HttpMethod) ?? 'POST', ...FRESH })}
          styles={{ input: { borderTopRightRadius: 0, borderBottomRightRadius: 0, fontWeight: 600 } }}
        />
        <TextInput
          flex={1}
          aria-label="login path"
          placeholder="/auth/login"
          value={auth.path}
          onChange={(e) => onChange({ path: e.currentTarget.value, ...FRESH })}
          leftSection={
            <Text size="sm" c="dimmed" pl="xs" style={{ whiteSpace: 'nowrap' }}>
              {baseUrl.replace(/\/$/, '')}
            </Text>
          }
          leftSectionWidth={Math.max(60, baseUrl.replace(/\/$/, '').length * 7.6 + 12)}
          styles={{ input: { borderRadius: 0, fontFamily: 'var(--mantine-font-family-monospace)' } }}
        />
        <Button
          leftSection={<IconSend size={16} />}
          loading={send.isPending}
          onClick={() => send.mutate()}
          style={{ borderTopLeftRadius: 0, borderBottomLeftRadius: 0 }}
        >
          Send
        </Button>
      </Group>

      <Group gap="sm">
        <Text size="sm" fw={500}>
          Body
        </Text>
        <SegmentedControl
          size="xs"
          value={auth.bodyMode}
          onChange={(v) =>
            onChange(
              v === 'raw'
                ? { bodyMode: 'raw', raw: auth.raw.trim() ? auth.raw : loginBody({ ...auth, bodyMode: 'fields' }) }
                : { bodyMode: 'fields' },
            )
          }
          data={[
            { value: 'fields', label: 'Key / Value' },
            { value: 'raw', label: 'Raw JSON' },
          ]}
        />
      </Group>

      {auth.bodyMode === 'fields' ? (
        <Table withTableBorder withColumnBorders verticalSpacing={4} layout="fixed">
          <Table.Thead>
            <Table.Tr>
              <Table.Th w="35%">Key</Table.Th>
              <Table.Th>Value</Table.Th>
              <Table.Th w={72} />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {auth.fields.map((f, i) => (
              <Table.Tr key={i}>
                <Table.Td>
                  <TextInput
                    size="xs"
                    variant="unstyled"
                    aria-label={`key ${i + 1}`}
                    placeholder="key"
                    value={f.key}
                    onChange={(e) => setField(i, { key: e.currentTarget.value })}
                  />
                </Table.Td>
                <Table.Td>
                  {f.secret ? (
                    <PasswordInput
                      size="xs"
                      variant="unstyled"
                      aria-label={`value of ${f.key || `key ${i + 1}`}`}
                      placeholder="secret: not saved"
                      value={secret}
                      onChange={(e) => onSecret(e.currentTarget.value)}
                    />
                  ) : (
                    <TextInput
                      size="xs"
                      variant="unstyled"
                      aria-label={`value of ${f.key || `key ${i + 1}`}`}
                      placeholder="value"
                      value={f.value}
                      onChange={(e) => setField(i, { value: e.currentTarget.value })}
                    />
                  )}
                </Table.Td>
                <Table.Td>
                  <Group gap={4} wrap="nowrap">
                    <Tooltip
                      label={f.secret ? 'Secret: never saved in a file' : 'Make it the secret (a password)'}
                      withArrow
                    >
                      <ActionIcon
                        size="sm"
                        variant={f.secret ? 'light' : 'subtle'}
                        color={f.secret ? 'orange' : 'gray'}
                        aria-label={f.secret ? `${f.key} is secret` : `make ${f.key} secret`}
                        onClick={() => setField(i, { secret: !f.secret })}
                      >
                        {f.secret ? <IconLock size={14} /> : <IconLockOpen size={14} />}
                      </ActionIcon>
                    </Tooltip>
                    <ActionIcon
                      size="sm"
                      variant="subtle"
                      color="gray"
                      aria-label={`remove ${f.key || `key ${i + 1}`}`}
                      onClick={() => onChange({ fields: auth.fields.filter((_, j) => j !== i) })}
                    >
                      <IconX size={14} />
                    </ActionIcon>
                  </Group>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
          <Table.Caption>
            <Group justify="space-between">
              <Button
                size="compact-xs"
                variant="subtle"
                leftSection={<IconPlus size={12} />}
                onClick={() => onChange({ fields: [...auth.fields, { key: '', value: '', secret: false }] })}
              >
                Add a key
              </Button>
              <Text size="xs" c="dimmed">
                The locked value is kept out of every file: the test reads it from {secretVariable(auth)}.
              </Text>
            </Group>
          </Table.Caption>
        </Table>
      ) : (
        <Stack gap={4}>
          <Textarea
            aria-label="login body"
            autosize
            minRows={3}
            value={auth.raw}
            onChange={(e) => onChange({ raw: e.currentTarget.value })}
            styles={{ input: { fontFamily: 'var(--mantine-font-family-monospace)' } }}
          />
          <Group gap="xs" align="flex-end">
            <Text size="xs" c="dimmed">
              Write <Code>{PASSWORD}</Code> where the password goes: it is filled in from this box, and never saved.
            </Text>
            <PasswordInput
              size="xs"
              w={240}
              aria-label="password"
              placeholder="password"
              value={secret}
              onChange={(e) => onSecret(e.currentTarget.value)}
            />
          </Group>
        </Stack>
      )}

      {send.isError && <Alert color="red">{send.error.message}</Alert>}
      {send.data && (
        <ResponseView
          tried={send.data}
          auth={auth}
          onPick={(field) =>
            onChange(field === '' ? { tokenFrom: 'body', tokenField: '' } : { tokenFrom: 'field', tokenField: field })
          }
        />
      )}
      {!send.data && auth.tokenFrom !== 'auto' && (
        <Text size="xs" c="dimmed">
          The token is taken from {auth.tokenFrom === 'body' ? 'the response body' : <Code>{auth.tokenField}</Code>}.
          Send the request to see it.
        </Text>
      )}
    </Stack>
  );
}

/** The login's response, as Postman shows it, with the token marked and any value clickable. */
function ResponseView({ tried, auth, onPick }: { tried: LoginTry; auth: AuthSettings; onPick: (field: string) => void }) {
  const r = tried.result;
  let json: unknown;
  let isJson = false;
  if (tried.body) {
    try {
      json = JSON.parse(tried.body);
      isJson = true;
    } catch {
      isJson = false;
    }
  }
  // the token as the test will read it: where the user pointed, else where it was found
  const tokenPath =
    auth.tokenFrom === 'body' ? '' : auth.tokenFrom === 'field' ? auth.tokenField : tried.token ? (tried.token.field ?? '') : null;
  const found = tokenPath !== null && (isJson ? valueAt(json, tokenPath) : tokenPath === '' && !!tried.body);
  const verdict = loginVerdict(tried, found);

  return (
    <Paper withBorder p="sm" bg="var(--mantine-color-default-hover)">
      <Group justify="space-between" mb="xs">
        <Text size="sm" fw={600}>
          Response
        </Text>
        {r.status !== null && (
          <Group gap="xs">
            <Badge color={r.ok ? 'teal' : 'red'} variant="light">
              {r.status}
            </Badge>
            <Text size="xs" c="dimmed">
              {Math.round(r.milliseconds)} ms
            </Text>
          </Group>
        )}
      </Group>
      {r.status === null ? (
        <Text size="sm" c="red">
          No response from {r.url}: {r.error}
        </Text>
      ) : (
        <Box
          component="pre"
          m={0}
          p="xs"
          fz="xs"
          ff="monospace"
          mah={260}
          style={{
            overflow: 'auto',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
            background: 'var(--mantine-color-body)',
            borderRadius: 4,
          }}
          data-testid="login-response"
        >
          {!tried.body ? (
            <Text span c="dimmed" size="xs">
              (empty body)
            </Text>
          ) : isJson ? (
            <JsonView value={json} tokenPath={tokenPath} onPick={r.ok ? onPick : undefined} />
          ) : (
            <Leaf text={tried.body} path="" tokenPath={tokenPath} onPick={r.ok ? onPick : undefined} />
          )}
        </Box>
      )}
      <Alert mt="xs" color={verdict.color} variant="light" title={verdict.title} p="xs">
        <Text size="sm">{verdict.detail}</Text>
      </Alert>
    </Paper>
  );
}

/** Whether a.b.0.c leads to a string or number in the JSON ('' is the whole body). */
function valueAt(json: unknown, path: string): boolean {
  let node: unknown = json;
  for (const part of path.split('.').filter(Boolean)) {
    if (node === null || typeof node !== 'object') return false;
    node = (node as Record<string, unknown>)[part];
  }
  return (typeof node === 'string' && node.length > 0) || typeof node === 'number';
}

function loginVerdict(tried: LoginTry, found: boolean): { color: string; title: string; detail: ReactNode } {
  const r = tried.result;
  if (r.status === null) {
    return { color: 'red', title: 'No response', detail: "Is your app running, and is its address above right?" };
  }
  if (!r.ok) {
    return {
      color: 'red',
      title: `The login was refused (${r.status})`,
      detail: 'Check the keys and values of the body and the account: the response above usually says what is wrong.',
    };
  }
  if (found) {
    return {
      color: 'teal',
      title: 'Logged in: the token is marked above',
      detail: 'Every request of the test will send it. If another value is the token, click that one.',
    };
  }
  if (tried.setsCookie) {
    return {
      color: 'yellow',
      title: 'Logged in, but with a cookie, not a token',
      detail:
        "This API keeps the login in a session cookie. The test written here can't carry cookies yet: use your own k6 script for this app.",
    };
  }
  return {
    color: 'yellow',
    title: 'Logged in, but no token was recognised',
    detail: 'Click the value in the response that is the token.',
  };
}

type PickProps = { tokenPath: string | null; onPick?: (field: string) => void };

/** Pretty JSON whose values can be clicked to say "this is the token". */
function JsonView({ value, tokenPath, onPick }: { value: unknown } & PickProps) {
  const render = (v: unknown, path: string, depth: number, key: string | null, last: boolean): ReactNode => {
    const indent = '  '.repeat(depth);
    const label =
      key === null ? null : (
        <span style={{ color: 'var(--mantine-color-grape-text)' }}>{JSON.stringify(key)}</span>
      );
    const comma = last ? '' : ',';
    if (v !== null && typeof v === 'object') {
      const entries: [string, unknown][] = Array.isArray(v) ? v.map((x, i) => [String(i), x]) : Object.entries(v);
      const [open, close] = Array.isArray(v) ? ['[', ']'] : ['{', '}'];
      if (entries.length === 0) {
        return (
          <div>
            {indent}
            {label}
            {label && ': '}
            {open}
            {close}
            {comma}
          </div>
        );
      }
      return (
        <>
          <div>
            {indent}
            {label}
            {label && ': '}
            {open}
          </div>
          {entries.map(([k, x], i) => (
            <Fragment key={k}>
              {render(x, path ? `${path}.${k}` : k, depth + 1, Array.isArray(v) ? null : k, i === entries.length - 1)}
            </Fragment>
          ))}
          <div>
            {indent}
            {close}
            {comma}
          </div>
        </>
      );
    }
    return (
      <div>
        {indent}
        {label}
        {label && ': '}
        <Leaf text={JSON.stringify(v)} path={path} tokenPath={tokenPath} onPick={onPick} />
        {comma}
      </div>
    );
  };
  return <>{render(value, '', 0, null, true)}</>;
}

function Leaf({ text, path, tokenPath, onPick }: { text: string; path: string } & PickProps) {
  const isToken = path === tokenPath;
  const shown = isToken ? <Mark color="teal">{text}</Mark> : text;
  return (
    <>
      {onPick ? (
        <Tooltip label={isToken ? 'The token' : 'Use this as the token'} withArrow openDelay={300}>
          <UnstyledButton
            component="span"
            onClick={() => onPick(path)}
            aria-label={isToken ? `token at ${path || 'the response body'}` : `use ${path || 'the response body'} as the token`}
            style={{ font: 'inherit', textDecoration: isToken ? undefined : 'underline dotted', cursor: 'pointer' }}
          >
            {shown}
          </UnstyledButton>
        </Tooltip>
      ) : (
        shown
      )}
      {isToken && (
        <Badge size="xs" color="teal" ml={6} style={{ verticalAlign: 'middle' }}>
          token
        </Badge>
      )}
    </>
  );
}
