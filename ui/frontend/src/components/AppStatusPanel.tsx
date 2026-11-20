import {
  Alert,
  Box,
  Button,
  Code,
  CopyButton,
  Group,
  List,
  Loader,
  Paper,
  ScrollArea,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from '@mantine/core';
import { IconCircleCheck, IconPlayerPlay, IconRefresh, IconTerminal2 } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { AppProject, AppStatus } from '../types';
import { useStartJob } from './useStartJob';

type Props = {
  projects: AppProject[];
  /** The app's address, to check something answers there; null when the user brings their own script. */
  baseUrl: string | null;
  /** Told whenever it is known whether the app is up (and answers, when there is an address). */
  onReady: (ready: boolean) => void;
};

/**
 * Whether the user's app is up, and if it isn't: start it for them (the same
 * `docker compose up -d` with the files they picked) or show them the command.
 * Trying routes, and the run itself, need it up, so better now than later.
 */
export function AppStatusPanel({ projects, baseUrl, onReady }: Props) {
  const [startError, setStartError] = useState<string | null>(null);
  const [manual, setManual] = useState(false);
  const output = useRef<HTMLDivElement>(null);
  const progress = useCallback((after: number) => api.startProgress(after), []);
  const recheck = useRef<() => void>(() => {});
  const { job, lines, begin } = useStartJob(progress, () => recheck.current());
  const status = useQuery({
    queryKey: ['app-status', projects, baseUrl],
    queryFn: () => api.appStatus(projects, baseUrl),
    enabled: projects.length > 0,
    // after a start, keep looking until the app answers
    refetchInterval: (q) => {
      const s = q.state.data;
      const settling = job?.state === 'starting' || job?.state === 'started';
      return settling && !(s && ready(s, baseUrl)) ? 3000 : false;
    },
  });
  recheck.current = () => void status.refetch();
  const starting = job;
  const isReady = status.data ? ready(status.data, baseUrl) : false;
  useEffect(() => onReady(isReady), [isReady, onReady]);
  // follow the end, like a terminal
  useEffect(() => {
    output.current?.scrollTo({ top: output.current.scrollHeight });
  }, [lines]);

  const start = async () => {
    setStartError(null);
    try {
      begin(await api.startApp(projects));
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e));
    }
  };

  if (projects.length === 0) return null;
  const s = status.data;

  if (!s) {
    return (
      <Paper withBorder p="md">
        <Group gap="xs">
          <Loader size="xs" />
          <Text size="sm">Checking whether your app is running…</Text>
        </Group>
      </Paper>
    );
  }

  if (isReady) {
    const containers = s.projects.reduce((n, p) => n + p.running, 0);
    const resting = s.projects.reduce((n, p) => n + p.notRunning, 0);
    return (
      <Paper withBorder p="sm" data-app-state="ready" style={{ borderLeft: '4px solid var(--mantine-color-teal-6)' }}>
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <ThemeIcon color="teal" variant="light" radius="xl">
              <IconCircleCheck size={18} />
            </ThemeIcon>
            <Text size="sm">
              <b>Your app is running</b>: {containers} container{containers === 1 ? '' : 's'}
              {resting > 0 ? ` (${resting} more not running)` : ''}
              {s.baseUrl && s.baseStatus !== null ? `; ${s.baseUrl} answers.` : '.'}
            </Text>
          </Group>
          <Button size="compact-sm" variant="subtle" leftSection={<IconRefresh size={14} />}
            loading={status.isFetching} onClick={() => status.refetch()}>
            Check again
          </Button>
        </Group>
      </Paper>
    );
  }

  const busy = starting?.state === 'starting';
  const upButSilent = s.running && !!baseUrl && s.baseStatus === null;
  return (
    <Paper withBorder p="md" data-app-state={busy ? 'starting' : upButSilent ? 'silent' : 'down'}
      style={{ borderLeft: `4px solid var(--mantine-color-${upButSilent || busy ? 'yellow' : 'orange'}-6)` }}>
      {busy ? (
        <Group gap="xs">
          <Loader size="sm" />
          <Title order={5}>Starting your app…</Title>
          <Text size="sm" c="dimmed">
            The first time, Compose may build images: that can take a few minutes.
          </Text>
        </Group>
      ) : upButSilent ? (
        <>
          <Title order={5}>Your app is running, but nothing answers at {baseUrl} yet</Title>
          <Text size="sm" mt={4}>
            It may still be starting{starting ? ': this looks again every few seconds' : ''}. If it stays like this,
            check the address under "How the load test is made".
          </Text>
        </>
      ) : (
        <>
          <Title order={5}>Start your app to try the routes</Title>
          <Text size="sm" mt={4}>
            Trying the routes, and the load test itself, need your app running. Start it yourself, or let this app
            start it with the compose files you picked.
          </Text>
          <List size="sm" mt="xs">
            {s.projects.map((p) => (
              <List.Item key={p.name}>
                <b>{p.name}</b>: {p.state === 'missing' ? 'not started' : p.status}
              </List.Item>
            ))}
          </List>
        </>
      )}

      {!busy && !upButSilent && (
        <Group mt="md" gap="sm">
          <Button leftSection={<IconPlayerPlay size={16} />} onClick={start}>
            Start it for me
          </Button>
          <Button variant="default" leftSection={<IconTerminal2 size={16} />} onClick={() => setManual((m) => !m)}>
            I'll start it myself
          </Button>
          <Button variant="subtle" leftSection={<IconRefresh size={14} />} loading={status.isFetching}
            onClick={() => status.refetch()}>
            Check again
          </Button>
        </Group>
      )}
      {upButSilent && !starting && (
        <Button mt="sm" size="compact-sm" variant="subtle" leftSection={<IconRefresh size={14} />}
          loading={status.isFetching} onClick={() => status.refetch()}>
          Check again
        </Button>
      )}

      {manual && !busy && (
        <Stack gap="xs" mt="sm">
          <Text size="sm">Run this in a terminal, then press Check again:</Text>
          {s.commands.map((c) => (
            <Group key={c} gap="xs" align="flex-start" wrap="nowrap">
              <Code block style={{ flex: 1 }}>
                {c}
              </Code>
              <CopyButton value={c}>
                {({ copied, copy }) => (
                  <Button size="compact-xs" variant="light" color={copied ? 'teal' : undefined} onClick={copy}>
                    {copied ? 'Copied' : 'Copy'}
                  </Button>
                )}
              </CopyButton>
            </Group>
          ))}
        </Stack>
      )}

      {startError && (
        <Alert color="red" mt="sm">
          {startError}
        </Alert>
      )}
      {starting?.state === 'failed' && (
        <Alert color="red" mt="sm" title="Compose couldn't start it">
          Its output is below: the last lines usually say why (a port already taken, an image that doesn't build).
        </Alert>
      )}
      {lines.length > 0 && (busy || starting?.state === 'failed' || upButSilent) && (
        <ScrollArea.Autosize mah={220} mt="sm" type="auto" viewportRef={output}>
          <Box component="pre" m={0} fz="xs" ff="monospace" style={{ whiteSpace: 'pre-wrap' }} data-testid="start-output">
            {lines.map((l) => l.text).join('\n')}
          </Box>
        </ScrollArea.Autosize>
      )}
    </Paper>
  );
}

/** Up: every project has containers running, and the address answers when there is one. */
function ready(s: AppStatus, baseUrl: string | null): boolean {
  return s.running && (!baseUrl || s.baseStatus !== null);
}
