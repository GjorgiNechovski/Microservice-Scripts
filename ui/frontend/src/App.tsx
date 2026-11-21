import {
  ActionIcon,
  Alert,
  AppShell,
  Button,
  Container,
  Group,
  SegmentedControl,
  Stepper,
  Text,
  ThemeIcon,
  Title,
  useComputedColorScheme,
  useMantineColorScheme,
} from '@mantine/core';
import {
  IconArrowLeft,
  IconArrowRight,
  IconChartBar,
  IconHistory,
  IconCircleCheck,
  IconInfoCircle,
  IconMoon,
  IconPlayerPlay,
  IconSun,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';
import { api } from './api';
import { forget, keep, restore, type Restored } from './persist';
import { ago } from './results';
import {
  STEP,
  STEPS,
  initialState,
  settle,
  stepStatus,
  type PrerequisiteStatus,
  type StepStatus,
  type WizardState,
} from './model';
import { selectedRoutes, triedErrors } from './routes';
import type { RunState } from './types';
import { ComponentsStep } from './steps/ComponentsStep';
import { FeaturesStep } from './steps/FeaturesStep';
import { PrerequisitesStep } from './steps/PrerequisitesStep';
import { ProjectStep } from './steps/ProjectStep';
import { ResultsPage } from './steps/ResultsPage';
import { RouteErrorsConfirm, RoutesStep } from './steps/RoutesStep';
import { RunStep } from './steps/RunStep';
import { SaveStep } from './steps/SaveStep';
import { UsersStep } from './steps/UsersStep';

export default function App({ hasToken }: { hasToken: boolean }) {
  // where the user left off, before a reload or a restart of the app
  const [restored, setRestored] = useState<Restored | null>(() => (hasToken ? restore() : null));
  const [active, setActive] = useState(restored?.active ?? 0);
  const [prerequisites, setPrerequisites] = useState<PrerequisiteStatus>('checking');
  const [run, setRun] = useState<RunState>('idle');
  const [state, setStateRaw] = useState<WizardState>(restored?.state ?? initialState);
  useEffect(() => {
    if (!hasToken) return;
    const later = setTimeout(() => keep(state, active), 300);
    return () => clearTimeout(later);
  }, [hasToken, state, active]);
  // the run, wherever the user is: the Run step follows its output, this only its state (a run that ends while
  // the results are open; one still going after the page was reloaded)
  const runState = useQuery({
    queryKey: ['run-state'],
    // the last line number the backend can take: only the state is wanted, not the output
    queryFn: () => api.run(2_147_483_647),
    enabled: hasToken,
    refetchInterval: (q) => (q.state.data && ['running', 'stopping'].includes(q.state.data.state) ? 3000 : 15000),
  });
  useEffect(() => {
    if (runState.data?.state) setRun(runState.data.state);
  }, [runState.data]);
  const running = run === 'running' || run === 'stopping';
  // any change to what was saved means saving again
  const setState = useCallback(
    (update: (s: WizardState) => WizardState) => setStateRaw((s) => settle(s, update(s))),
    [],
  );
  const status = stepStatus(active, state, prerequisites, run);
  // the results are a page of their own, open at any time: earlier runs' results don't need this setup
  const [page, setPage] = useState<Page>('setup');
  const [confirmRoutes, setConfirmRoutes] = useState(false);
  const routeErrors = triedErrors(state.tried, selectedRoutes(state.routes, state.components));

  const go = (step: number) => {
    setPage('setup');
    setActive(step);
    window.scrollTo({ top: 0 });
  };
  const show = (to: Page) => {
    setPage(to);
    window.scrollTo({ top: 0 });
  };
  const next = () => {
    if (active === STEP.routes && routeErrors.length > 0) setConfirmRoutes(true);
    else go(active + 1);
  };
  const startOver = () => {
    forget();
    setRestored(null);
    setStateRaw(initialState);
    go(0);
  };

  return (
    <AppShell header={{ height: 56 }} footer={hasToken ? { height: 72 } : undefined} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <Title order={4}>Microservice-Scripts</Title>
            <Text c="dimmed">setup</Text>
          </Group>
          <Group gap="sm">
            {hasToken && running && (page === 'results' || active !== STEP.run) && (
              <Button color="orange" variant="light" leftSection={<IconPlayerPlay size={16} />} onClick={() => go(STEP.run)}>
                A run is going
              </Button>
            )}
            {hasToken && (
              <SegmentedControl
                value={page}
                onChange={(v) => show(v as Page)}
                data={[
                  { value: 'setup', label: 'Setup' },
                  { value: 'results', label: 'Results' },
                ]}
                aria-label="Page"
              />
            )}
            <ColorSchemeToggle />
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Main>
        <Container size="xl" pb="xl">
          {!hasToken ? (
            <Alert color="red" title="Open the app from its own address">
              This page needs the address the app printed when it started (it ends in ?token=…). Start it with
              start.sh in the repository's root, or start.ps1 on Windows.
            </Alert>
          ) : page === 'results' ? (
            <ResultsPage state={state} run={run} onGoToRun={() => go(STEP.run)} />
          ) : (
            <>
            {restored && (
              <RestoredNotice
                restored={restored}
                needsSecret={state.secret === '' && state.auth.kind !== 'none'}
                onStartOver={startOver}
                onClose={() => setRestored(null)}
              />
            )}
            <Stepper active={active} onStepClick={go} allowNextStepsSelect={false} mb="xl" size="sm">
              <Stepper.Step label={STEPS[0]} description="What's needed">
                <PrerequisitesStep onStatus={setPrerequisites} />
              </Stepper.Step>
              <Stepper.Step label={STEPS[1]} description="Compose files">
                <ProjectStep state={state} setState={setState} />
              </Stepper.Step>
              <Stepper.Step label={STEPS[2]} description="What each is">
                <ComponentsStep state={state} setState={setState} />
              </Stepper.Step>
              <Stepper.Step label={STEPS[3]} description="What to test">
                <RoutesStep state={state} setState={setState} />
              </Stepper.Step>
              <Stepper.Step label={STEPS[4]} description="What each needs">
                <FeaturesStep state={state} setState={setState} />
              </Stepper.Step>
              <Stepper.Step label={STEPS[5]} description="How much load">
                <UsersStep state={state} setState={setState} />
              </Stepper.Step>
              <Stepper.Step label={STEPS[6]} description="Write the files">
                <SaveStep state={state} setState={setState} />
              </Stepper.Step>
              <Stepper.Step label={STEPS[7]} description="Start it">
                <RunStep state={state} setState={setState} onRunState={setRun} onShowResults={() => show('results')} />
              </Stepper.Step>
            </Stepper>
            </>
          )}
        </Container>
      </AppShell.Main>
      {hasToken && (
        <AppShell.Footer>
          {page === 'results' ? (
            <ResultsBar onBack={() => show('setup')} />
          ) : (
            <StepBar
              active={active}
              status={status}
              onBack={() => go(active - 1)}
              onNext={next}
              onResults={() => show('results')}
              finished={run === 'finished'}
            />
          )}
        </AppShell.Footer>
      )}
      <RouteErrorsConfirm
        errors={routeErrors}
        opened={confirmRoutes}
        onStay={() => setConfirmRoutes(false)}
        onContinue={() => {
          setConfirmRoutes(false);
          go(STEP.features);
        }}
      />
    </AppShell>
  );
}

type Page = 'setup' | 'results';

type RestoredProps = {
  restored: Restored;
  /** The login needs a password or key, and those are never kept. */
  needsSecret: boolean;
  onStartOver: () => void;
  onClose: () => void;
};

/** Says the wizard was picked up where it was left, and offers a clean start. */
function RestoredNotice({ restored, needsSecret, onStartOver, onClose }: RestoredProps) {
  return (
    <Alert
      color="blue"
      variant="light"
      icon={<IconHistory size={18} />}
      title={`Picked up where you left off${restored.savedAt ? ` (${ago(new Date(restored.savedAt).toISOString())})` : ''}`}
      withCloseButton
      closeButtonLabel="Hide"
      onClose={onClose}
      mb="md"
    >
      <Group justify="space-between" align="flex-end" gap="sm">
        <Text size="sm" style={{ flex: 1, minWidth: 240 }}>
          Everything you filled in is as you left it. If the compose files or the code changed since, read them again in
          step 2 (Project).
          {needsSecret ? ' The password or key for the login is never kept: enter it again before trying the routes or running.' : ''}
        </Text>
        <Button size="xs" variant="default" onClick={onStartOver}>
          Start over
        </Button>
      </Group>
    </Alert>
  );
}

type StepBarProps = {
  active: number;
  status: StepStatus;
  onBack: () => void;
  onNext: () => void;
  /** On the last step, in Next's place. */
  onResults: () => void;
  /** The run just finished: its results are the next thing to see. */
  finished: boolean;
};

/**
 * Always in view at the bottom: whether this step is done, and if not, what's
 * still missing. Next is named after the step it leads to.
 */
export function StepBar({ active, status, onBack, onNext, onResults, finished }: StepBarProps) {
  const last = active === STEPS.length - 1;
  return (
    <Container size="xl" h="100%">
      <Group h="100%" justify="space-between" wrap="nowrap" gap="md">
        <Button variant="default" leftSection={<IconArrowLeft size={16} />} disabled={active === 0} onClick={onBack}>
          Back
        </Button>
        <Group gap="xs" wrap="nowrap" style={{ flex: 1, minWidth: 0 }} justify="center">
          <ThemeIcon color={status.ready ? 'teal' : 'yellow'} variant="light" radius="xl" size="md">
            {status.ready ? <IconCircleCheck size={18} /> : <IconInfoCircle size={18} />}
          </ThemeIcon>
          <Text size="sm" fw={status.ready ? 600 : 400} lineClamp={2} role="status" aria-live="polite">
            {status.message}
          </Text>
        </Group>
        {last ? (
          <Button
            color={finished ? 'teal' : undefined}
            variant={finished ? 'filled' : 'default'}
            rightSection={<IconChartBar size={16} />}
            onClick={onResults}
          >
            See the results
          </Button>
        ) : (
          <Button
            color={status.ready ? 'teal' : undefined}
            rightSection={<IconArrowRight size={16} />}
            disabled={!status.ready}
            onClick={onNext}
          >
            Next: {STEPS[active + 1]}
          </Button>
        )}
      </Group>
    </Container>
  );
}

/** At the bottom of the results page: back to where the setup was. */
function ResultsBar({ onBack }: { onBack: () => void }) {
  return (
    <Container size="xl" h="100%">
      <Group h="100%" justify="space-between" wrap="nowrap" gap="md">
        <Button variant="default" leftSection={<IconArrowLeft size={16} />} onClick={onBack}>
          Back to the setup
        </Button>
        <Text size="sm" c="dimmed" ta="center" style={{ flex: 1, minWidth: 0 }} lineClamp={2}>
          To refresh the results, run the analysis again: Setup, step 8 (Run).
        </Text>
      </Group>
    </Container>
  );
}

function ColorSchemeToggle() {
  const { setColorScheme } = useMantineColorScheme();
  const computed = useComputedColorScheme('light');
  return (
    <ActionIcon
      variant="default"
      size="lg"
      aria-label="Switch light or dark"
      onClick={() => setColorScheme(computed === 'light' ? 'dark' : 'light')}
    >
      {computed === 'light' ? <IconMoon size={18} /> : <IconSun size={18} />}
    </ActionIcon>
  );
}
