import {
  Alert,
  Anchor,
  Badge,
  Box,
  Button,
  Code,
  Group,
  List,
  Loader,
  Modal,
  Paper,
  PasswordInput,
  Progress,
  Radio,
  ScrollArea,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconArrowRight,
  IconCircleCheck,
  IconCircleX,
  IconPlayerPlay,
  IconPlayerStop,
  IconRefresh,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { AppStatusPanel } from '../components/AppStatusPanel';
import { MonitoringPanel } from '../components/MonitoringPanel';
import { appProjects, slug, type WizardState } from '../model';
import { SECRET_LABEL, estimatedMinutes, secretVariable, usesSecret } from '../routes';
import type { PreflightCheck, RunLine, RunState, RunStatus } from '../types';

type Props = {
  state: WizardState;
  setState: (update: (s: WizardState) => WizardState) => void;
  onRunState: (state: RunState) => void;
  /** Opens the results page. */
  onShowResults?: () => void;
};

const ACTIVE: RunState[] = ['running', 'stopping'];

/** Follows the backend's run: its state and output, a line at a time. */
function useRun(onRunState: (state: RunState) => void) {
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [lines, setLines] = useState<RunLine[]>([]);
  const next = useRef(0);
  const startedAt = useRef<string | null>(null);

  const poll = useCallback(async () => {
    let s = await api.run(next.current);
    if (s.startedAt !== startedAt.current) {
      // another run than the one shown: read it from its first line
      startedAt.current = s.startedAt;
      s = await api.run(0);
      setLines(s.lines);
    } else if (s.lines.length > 0) {
      setLines((previous) => [...previous, ...s.lines].slice(-5000));
    }
    next.current = s.next;
    setStatus(s);
    onRunState(s.state);
    return s;
  }, [onRunState]);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const loop = async () => {
      try {
        const s = await poll();
        if (!stop) timer = setTimeout(loop, ACTIVE.includes(s.state) ? 1000 : 5000);
      } catch {
        if (!stop) timer = setTimeout(loop, 3000);
      }
    };
    void loop();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [poll]);

  return { status, lines, poll };
}

export function RunStep({ state, setState, onRunState, onShowResults }: Props) {
  // everything the method covers is the default: load, fault injection, all four analysis layers
  const [faultInjection, setFaultInjection] = useState(true);
  const [confirmFull, setConfirmFull] = useState(false);
  const [showPlan, setShowPlan] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [appReady, setAppReady] = useState<boolean | null>(null);
  const [monitoringReady, setMonitoringReady] = useState<boolean | null>(null);
  const { status, lines, poll } = useRun(onRunState);
  const specPath = state.savedPath;
  const generated = state.load.script === 'generate';
  // a sweep measures recovery once, before its load levels; a single run after its load test
  const sweep = state.load.users.length > 1;
  const needsSecret = generated && usesSecret(state.auth);

  const preflight = useQuery({
    queryKey: ['preflight', specPath, generated ? state.baseUrl : null],
    queryFn: () => api.preflight(specPath!, generated ? state.baseUrl : null),
    enabled: !!specPath,
    retry: false,
    // something still starting (cAdvisor's first readings): look again shortly
    refetchInterval: (q) => (q.state.data?.checks.some((c) => c.status === 'wait') ? 10_000 : false),
  });

  // once the app or the monitoring comes up, check again
  const { refetch: recheck } = preflight;
  useEffect(() => {
    if (appReady || monitoringReady) void recheck();
  }, [appReady, monitoringReady, recheck]);

  const components = state.components.filter((c) => c.kind === 'service' || c.kind === 'infrastructure').length;
  const loadMinutes = estimatedMinutes(state.load);
  // three stops per component, each back in seconds to a minute
  const faultMinutes = Math.ceil((components * 3 * 20) / 60);
  const totalMinutes = loadMinutes + (faultInjection ? faultMinutes : 0);

  const active = status !== null && ACTIVE.includes(status.state);
  const canStart =
    !!specPath &&
    !active &&
    preflight.data?.canStart === true &&
    (!needsSecret || state.secret.length > 0);

  const start = async () => {
    setConfirmFull(false);
    setStarting(true);
    setStartError(null);
    try {
      await api.start(specPath!, faultInjection, needsSecret ? { [secretVariable(state.auth)]: state.secret } : {});
      await poll();
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };
  const stop = async () => {
    setConfirmStop(false);
    await api.stop();
    await poll();
  };

  const stopButton = active && (
    <Button
      size="md"
      color="red"
      variant="light"
      leftSection={<IconPlayerStop size={18} />}
      disabled={status?.state === 'stopping'}
      onClick={() => setConfirmStop(true)}
    >
      Stop
    </Button>
  );
  const output = status && status.state !== 'idle' && (
    <RunOutput status={status} lines={lines} estimate={totalMinutes * 60} sweep={sweep} onShowResults={onShowResults} />
  );
  const stopModal = (
    <Modal opened={confirmStop} onClose={() => setConfirmStop(false)} title="Stop the run?">
      <Stack>
        <Text size="sm">
          The analyzer stops at once, starts again anything it had stopped, and removes its load generator. What it
          measured so far isn't analysed.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={() => setConfirmStop(false)}>
            Keep running
          </Button>
          <Button color="red" onClick={stop}>
            Stop
          </Button>
        </Group>
      </Stack>
    </Modal>
  );

  if (!specPath) {
    // a run that's going stays in view and can be stopped, whatever happened to the setup since
    return (
      <Stack gap="lg">
        <Alert color="yellow">
          {active
            ? "A run is going (below). To start another one, save the spec in the previous step: saving into this run's folder waits until it ends."
            : 'Save the spec in the previous step first.'}
        </Alert>
        {stopButton && <Group>{stopButton}</Group>}
        {output}
        {stopModal}
      </Stack>
    );
  }

  return (
    <Stack gap="lg">
      <AppStatusPanel
        projects={appProjects(state)}
        baseUrl={generated && state.baseUrl.trim() ? state.baseUrl.trim() : null}
        onReady={setAppReady}
      />
      <MonitoringPanel
        appNetworks={(state.read?.draft?.networks ?? []).map((n) => n.name)}
        namespace={slug(state.specName)}
        onReady={setMonitoringReady}
      />
      <Paper withBorder p="md">
        <Group justify="space-between">
          <Title order={5}>Before you start</Title>
          <Button
            size="xs"
            variant="subtle"
            leftSection={<IconRefresh size={14} />}
            loading={preflight.isFetching}
            onClick={() => preflight.refetch()}
          >
            Check again
          </Button>
        </Group>
        {preflight.isPending && (
          <Group gap="xs" mt="xs">
            <Loader size="xs" />
            <Text size="sm">Checking the app, the monitoring stack and the load generator…</Text>
          </Group>
        )}
        {preflight.isError && (
          <Alert color="red" mt="xs">
            {preflight.error.message}
          </Alert>
        )}
        <Stack gap={6} mt="xs">
          {preflight.data?.checks.map((c) => <CheckRow key={c.key} check={c} />)}
        </Stack>
      </Paper>

      <Paper withBorder p="md">
        <Title order={5} mb="xs">
          What to run
        </Title>
        <Radio.Group value={faultInjection ? 'full' : 'load'} onChange={(v) => setFaultInjection(v === 'full')}>
          <Stack gap="sm">
            <Radio
              value="full"
              label={
                <Group gap="xs">
                  <span>The full run</span>
                  <Badge size="sm" color="teal" variant="light">
                    recommended
                  </Badge>
                </Group>
              }
              description="Everything the method covers: the load test (speed, capacity, the bottleneck, how many copies each service needs), fault injection (how long each component takes to come back), then all four analysis layers: queueing, availability, single points of failure per feature, and what to improve first."
            />
            <Radio
              value="load"
              label="The load test only"
              description="Speed and capacity only. Nothing is stopped, but recovery isn't measured, so the availability figures are placeholders marked NOT MEASURED."
            />
          </Stack>
        </Radio.Group>
        {faultInjection && (
          <Stack gap={6} mt="md">
            <Text size="sm">
              Fault injection stops each component of your app a few times and starts it again, so your app has short,
              real outages. Run it only where that is fine, never on production.{' '}
              <Anchor component="button" size="sm" onClick={() => setShowPlan((v) => !v)}>
                {showPlan ? 'Hide' : 'Show'} what it stops
              </Anchor>
            </Text>
            {showPlan && (
              <ScrollArea.Autosize mah={260} type="auto">
                <Code block>{preflight.data?.faultPlan ?? '…'}</Code>
              </ScrollArea.Autosize>
            )}
          </Stack>
        )}
      </Paper>

      {needsSecret && (
        <PasswordInput
          w={360}
          label={state.auth.kind === 'login' ? 'Login password' : SECRET_LABEL[state.auth.kind]}
          description="Passed to the load test only: not saved anywhere."
          value={state.secret}
          onChange={(e) => {
            const secret = e.currentTarget.value;
            setState((s) => ({ ...s, secret }));
          }}
        />
      )}

      <Group>
        <Button
          size="md"
          color="teal"
          leftSection={<IconPlayerPlay size={18} />}
          disabled={!canStart}
          loading={starting}
          onClick={() => (faultInjection ? setConfirmFull(true) : start())}
        >
          Start
        </Button>
        {stopButton}
        <Text size="sm" c="dimmed">
          About {totalMinutes} minutes
          {faultInjection ? `: ${loadMinutes} for the load test, about ${faultMinutes} for fault injection` : ''}.
        </Text>
      </Group>
      {startError && <Alert color="red">{startError}</Alert>}

      {output}

      <Modal opened={confirmFull} onClose={() => setConfirmFull(false)} title="Start the full run?" size="lg">
        <Stack>
          <Text size="sm">
            {sweep
              ? 'First, before the load levels, these components are stopped and started again, a few times each:'
              : 'After the load test, these components are stopped and started again, a few times each:'}
          </Text>
          <ScrollArea.Autosize mah={260} type="auto">
            <Code block>{preflight.data?.faultPlan ?? '…'}</Code>
          </ScrollArea.Autosize>
          <Alert color="orange" icon={<IconAlertTriangle size={18} />}>
            <List size="sm">
              <List.Item>A component without persistence loses its data when stopped (a cache comes back empty).</List.Item>
              <List.Item>Stopping the run part-way still starts again whatever it had stopped.</List.Item>
            </List>
          </Alert>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmFull(false)}>
              Cancel
            </Button>
            <Button color="teal" leftSection={<IconPlayerPlay size={16} />} onClick={start}>
              Start the full run
            </Button>
          </Group>
        </Stack>
      </Modal>

      {stopModal}
    </Stack>
  );
}

function CheckRow({ check }: { check: PreflightCheck }) {
  const look = {
    ok: { color: 'teal', Icon: IconCircleCheck },
    wait: { color: 'blue', Icon: IconCircleCheck },
    warn: { color: 'yellow', Icon: IconAlertTriangle },
    fail: { color: 'red', Icon: IconCircleX },
  }[check.status];
  return (
    <Group gap="sm" wrap="nowrap" align="flex-start" data-check={check.key} data-status={check.status}>
      <ThemeIcon color={look.color} variant="light" size="sm" radius="xl" mt={2}>
        {check.status === 'wait' ? <Loader size={12} color="blue" /> : <look.Icon size={14} />}
      </ThemeIcon>
      <Text size="sm">
        <b>{check.label}</b>
        <Text span c="dimmed" size="sm">
          : {check.detail}
        </Text>
      </Text>
    </Group>
  );
}

type OutputProps = { status: RunStatus; lines: RunLine[]; estimate: number; sweep: boolean; onShowResults?: () => void };

function RunOutput({ status, lines, estimate, sweep, onShowResults }: OutputProps) {
  const viewport = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  useEffect(() => {
    if (follow) viewport.current?.scrollTo({ top: viewport.current.scrollHeight });
  }, [lines, follow]);
  const { text: elapsed, seconds } = useElapsed(status);
  const title = {
    idle: '',
    running: `Running for ${elapsed}`,
    stopping: 'Stopping…',
    finished: `Finished after ${elapsed}`,
    failed: `Ended with an error after ${elapsed}`,
    stopped: `Stopped after ${elapsed}`,
  }[status.state];

  return (
    <Paper withBorder p="md">
      <Group justify="space-between" mb="xs">
        <Group gap="xs">
          {(status.state === 'running' || status.state === 'stopping') && <Loader size="xs" />}
          <Title order={5}>{title}</Title>
        </Group>
        <Text size="xs" c="dimmed">
          {status.faultInjection ? 'the full run' : 'load test only'}
        </Text>
      </Group>
      {status.state === 'running' && (
        <Stack gap={4} mb="sm">
          <Progress value={Math.min(97, (seconds / estimate) * 100)} animated size="sm" />
          <Text size="xs" c="dimmed">
            About {Math.max(1, Math.ceil((estimate - seconds) / 60))} min left
            {status.faultInjection ? (sweep ? ' (fault injection, then the load levels)' : ' (load test, then fault injection)') : ''}. The output stays quiet while k6
            holds a level: that's normal.
          </Text>
        </Stack>
      )}
      <ScrollArea
        h={380}
        type="auto"
        viewportRef={viewport}
        onScrollPositionChange={({ y }) => {
          const v = viewport.current;
          if (v) setFollow(y + v.clientHeight >= v.scrollHeight - 24);
        }}
      >
        <Box
          component="pre"
          m={0}
          fz="xs"
          ff="monospace"
          style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}
          data-testid="run-output"
        >
          {lines.map((l) => (
            <div key={l.n} style={l.error ? { color: 'var(--mantine-color-orange-text)' } : undefined}>
              {l.text || ' '}
            </div>
          ))}
        </Box>
      </ScrollArea>
      {status.state === 'finished' && (
        <Alert color="teal" mt="sm" title="The results are ready">
          <Group justify="space-between" gap="sm">
            <Text size="sm">Saved in {status.outDir}.</Text>
            {onShowResults && (
              <Button size="xs" color="teal" rightSection={<IconArrowRight size={14} />} onClick={onShowResults}>
                See the results
              </Button>
            )}
          </Group>
        </Alert>
      )}
    </Paper>
  );
}

function useElapsed(status: RunStatus): { text: string; seconds: number } {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (status.state !== 'running' && status.state !== 'stopping') return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [status.state]);
  if (!status.startedAt) return { text: '', seconds: 0 };
  const end = status.endedAt ? Date.parse(status.endedAt) : now;
  const total = Math.max(0, Math.round((end - Date.parse(status.startedAt)) / 1000));
  return { text: `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`, seconds: total };
}
