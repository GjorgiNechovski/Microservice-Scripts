import {
  Alert,
  Badge,
  Box,
  Button,
  Code,
  CopyButton,
  Group,
  Loader,
  Paper,
  PasswordInput,
  ScrollArea,
  SegmentedControl,
  Select,
  Stack,
  Text,
  TextInput,
  ThemeIcon,
  Title,
  Tooltip,
} from '@mantine/core';
import { IconChartLine, IconCircleCheck, IconPlayerPlay, IconRefresh, IconTerminal2 } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { MonitoringSettings, MonitoringStatus } from '../types';
import { useStartJob } from './useStartJob';

type Props = {
  /** The app's networks, best first, from reading its compose files. */
  appNetworks: string[];
  /** What Beyla reports services under: the system's name. */
  namespace: string;
  onReady: (ready: boolean) => void;
};

/**
 * The monitoring stack the measurements come from (observability/): filled in
 * from what the app already knows and started on the user's say-so, like
 * their app.
 */
export function MonitoringPanel({ appNetworks, namespace, onReady }: Props) {
  const [settings, setSettings] = useState<MonitoringSettings | null>(null);
  const [manual, setManual] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const output = useRef<HTMLDivElement>(null);
  const progress = useCallback((after: number) => api.monitoringProgress(after), []);
  const recheck = useRef<() => void>(() => {});
  const { job, lines, begin } = useStartJob(progress, () => recheck.current());

  const status = useQuery({
    queryKey: ['monitoring'],
    queryFn: api.monitoring,
    refetchInterval: (q) => {
      const settling = job?.state === 'starting' || job?.state === 'started';
      if (settling && !(q.state.data && onAppNetwork(q.state.data, appNetworks))) return 3000;
      // targets Prometheus hasn't read yet: look again until it has
      return q.state.data?.targets.some((t) => t.health === 'unknown') ? 5000 : false;
    },
  });
  recheck.current = () => void status.refetch();
  const s = status.data;
  const ready = !!s && onAppNetwork(s, appNetworks);
  useEffect(() => onReady(ready), [ready, onReady]);
  useEffect(() => {
    output.current?.scrollTo({ top: output.current.scrollHeight });
  }, [lines]);

  // start from the file as it is, with the app's own network
  useEffect(() => {
    if (!s || settings) return;
    const current = s.settings;
    const network = appNetworks.find((n) => s.networks.includes(n)) ?? appNetworks[0] ?? current?.appNetwork ?? '';
    setSettings({
      appNetwork: network,
      traces: (current?.traces ?? false) && s.hasKey,
      honeycombKey: null,
      // the scrape jobs and name in the current .env belong to the app it was written for
      scrapeDir: current?.appNetwork === network ? (current?.scrapeDir ?? null) : null,
      namespace: (current?.appNetwork === network && current?.namespace) || namespace,
    });
  }, [s, settings, appNetworks, namespace]);

  const preview = useQuery({
    queryKey: ['monitoring-preview', settings],
    queryFn: () => api.monitoringPreview(settings!),
    enabled: !!settings && !ready,
  });

  const start = async () => {
    setStartError(null);
    try {
      begin(await api.startMonitoring(settings!));
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e));
    }
  };

  if (!s) {
    return (
      <Paper withBorder p="md">
        <Group gap="xs">
          <Loader size="xs" />
          <Text size="sm">Checking the monitoring stack…</Text>
        </Group>
      </Paper>
    );
  }

  if (ready) {
    const down = s.targets.filter((t) => t.health === 'down');
    return (
      <Paper withBorder p="sm" data-monitoring-state="ready"
        style={{ borderLeft: `4px solid var(--mantine-color-${down.length ? 'yellow' : 'teal'}-6)` }}>
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <Group gap="xs" wrap="nowrap" align="flex-start">
            <ThemeIcon color={down.length ? 'yellow' : 'teal'} variant="light" radius="xl">
              <IconCircleCheck size={18} />
            </ThemeIcon>
            <Stack gap={4}>
              <Text size="sm">
                <b>Monitoring is running</b>: Prometheus at localhost:9090, on your app's network
                {appNetworks.length > 0 ? ` (${s.joined.find((n) => appNetworks.includes(n))})` : ''}.
              </Text>
              <Group gap={6}>
                <Text size="xs" c="dimmed">
                  It reads:
                </Text>
                {s.targets.map((t) => (
                  <Tooltip
                    key={t.url}
                    label={t.health === 'unknown' ? 'Not read yet: Prometheus reads every 15 s' : (t.error ?? t.url)}
                    withArrow
                    multiline
                    maw={420}
                  >
                    <Badge
                      size="sm"
                      variant="light"
                      color={t.health === 'up' ? 'teal' : t.health === 'down' ? 'red' : 'gray'}
                      tt="none"
                    >
                      {t.job} {t.health === 'up' ? 'up' : t.health === 'down' ? 'down' : 'starting'}
                    </Badge>
                  </Tooltip>
                ))}
              </Group>
              {down.length > 0 && (
                <Text size="xs" c="yellow">
                  {down.map((t) => t.job).join(', ')} can't be read: {down[0].error ?? 'no answer'}. Hover a badge for
                  details. Right after starting, wait half a minute and check again.
                </Text>
              )}
            </Stack>
          </Group>
          <Button size="compact-sm" variant="subtle" leftSection={<IconRefresh size={14} />}
            loading={status.isFetching} onClick={() => status.refetch()}>
            Check again
          </Button>
        </Group>
      </Paper>
    );
  }

  const busy = job?.state === 'starting';
  const elsewhere = s.prometheusUp; // up, but not on this app's network
  const networks = [...new Set([...appNetworks, ...s.networks, ...(settings?.appNetwork ? [settings.appNetwork] : [])])];
  return (
    <Paper withBorder p="md" data-monitoring-state={busy ? 'starting' : elsewhere ? 'elsewhere' : 'down'}
      style={{ borderLeft: `4px solid var(--mantine-color-${busy ? 'yellow' : 'orange'}-6)` }}>
      <Group gap="xs">
        {busy ? <Loader size="sm" /> : <IconChartLine size={20} />}
        <Title order={5}>
          {busy
            ? 'Starting the monitoring stack…'
            : elsewhere
              ? `The monitoring stack is running, but for another app (on ${s.joined.join(', ')})`
              : 'Start the monitoring stack'}
        </Title>
      </Group>
      {!busy && (
        <Text size="sm" mt={4}>
          The analysis reads its measurements from Prometheus, in a small stack of its own (Prometheus, an
          OpenTelemetry collector and cAdvisor) that joins your app's network. These settings go into{' '}
          <Code>{s.envPath}</Code>
          {s.envExists ? ' (the file there now is kept as a .bak copy)' : ''}.
        </Text>
      )}

      {!busy && settings && (
        <Stack gap="sm" mt="sm">
          <Select
            maw={420}
            label="Your app's network"
            description="The stack joins it to read your services."
            data={networks.map((n) => ({ value: n, label: appNetworks.includes(n) ? `${n} (your app's)` : n }))}
            value={settings.appNetwork}
            allowDeselect={false}
            searchable
            onChange={(v) => v && setSettings({ ...settings, appNetwork: v })}
          />
          <Stack gap={4}>
            <Text size="sm" fw={500}>
              Traces
            </Text>
            <SegmentedControl
              w="fit-content"
              value={settings.traces ? 'honeycomb' : 'local'}
              onChange={(v) => setSettings({ ...settings, traces: v === 'honeycomb' })}
              data={[
                { value: 'local', label: 'None: metrics only, all on this machine' },
                { value: 'honeycomb', label: 'Also send traces to Honeycomb' },
              ]}
            />
            <Text size="xs" c="dimmed">
              The analysis needs only the metrics. Traces help you look into single slow requests; they need a free
              Honeycomb account.
            </Text>
          </Stack>
          {settings.traces && (
            <PasswordInput
              maw={420}
              label="Honeycomb API key"
              placeholder={s.hasKey ? 'keep the key already set' : 'from ui.honeycomb.io → API Keys'}
              value={settings.honeycombKey ?? ''}
              onChange={(e) => setSettings({ ...settings, honeycombKey: e.currentTarget.value })}
            />
          )}
          <TextInput
            maw={560}
            label="Extra scrape jobs (optional)"
            description="A folder of Prometheus scrape jobs for your system, usually its gateway's metrics. Read from observability/. Leave it empty if you have none."
            placeholder="../systems/my-system/prometheus"
            value={settings.scrapeDir ?? ''}
            onChange={(e) => setSettings({ ...settings, scrapeDir: e.currentTarget.value || null })}
          />
          {preview.data && (
            <>
              <Text size="sm" fw={500}>
                observability/.env
              </Text>
              <Code block>{preview.data.env}</Code>
              {preview.data.problems.map((p) => (
                <Text key={p} size="sm" c="red">
                  {p}
                </Text>
              ))}
            </>
          )}
          <Group gap="sm">
            <Button leftSection={<IconPlayerPlay size={16} />} onClick={start}
              disabled={!preview.data || preview.data.problems.length > 0}>
              {elsewhere ? 'Save and restart it on this network' : 'Save and start'}
            </Button>
            <Button variant="default" leftSection={<IconTerminal2 size={16} />} onClick={() => setManual((m) => !m)}>
              I'll start it myself
            </Button>
            <Button variant="subtle" leftSection={<IconRefresh size={14} />} loading={status.isFetching}
              onClick={() => status.refetch()}>
              Check again
            </Button>
          </Group>
          {manual && (
            <Group gap="xs" align="flex-start" wrap="nowrap">
              <Code block style={{ flex: 1 }}>
                {`# fill in ${s.envPath} first (see .env.example next to it)\n${s.command}`}
              </Code>
              <CopyButton value={s.command}>
                {({ copied, copy }) => (
                  <Button size="compact-xs" variant="light" color={copied ? 'teal' : undefined} onClick={copy}>
                    {copied ? 'Copied' : 'Copy'}
                  </Button>
                )}
              </CopyButton>
            </Group>
          )}
        </Stack>
      )}

      {startError && (
        <Alert color="red" mt="sm">
          {startError}
        </Alert>
      )}
      {job?.state === 'failed' && (
        <Alert color="red" mt="sm" title="Compose couldn't start it">
          Its output is below: the last lines usually say why (a port already taken, a network that doesn't exist).
        </Alert>
      )}
      {lines.length > 0 && (busy || job?.state === 'failed') && (
        <ScrollArea.Autosize mah={220} mt="sm" type="auto" viewportRef={output}>
          <Box component="pre" m={0} fz="xs" ff="monospace" style={{ whiteSpace: 'pre-wrap' }}>
            {lines.map((l) => l.text).join('\n')}
          </Box>
        </ScrollArea.Autosize>
      )}
    </Paper>
  );
}

/** Up, and on the app's network (any network, when the app's aren't known). */
function onAppNetwork(s: MonitoringStatus, appNetworks: string[]): boolean {
  return s.prometheusUp && (appNetworks.length === 0 || s.joined.some((n) => appNetworks.includes(n)));
}
