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
  IconCircleCheck,
  IconInfoCircle,
  IconMoon,
  IconSun,
} from '@tabler/icons-react';
import { useCallback, useState } from 'react';
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
  const [active, setActive] = useState(0);
  const [prerequisites, setPrerequisites] = useState<PrerequisiteStatus>('checking');
  const [run, setRun] = useState<RunState>('idle');
  const [state, setStateRaw] = useState<WizardState>(initialState);
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

  return (
    <AppShell header={{ height: 56 }} footer={hasToken ? { height: 72 } : undefined} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <Title order={4}>Microservice-Scripts</Title>
            <Text c="dimmed">setup</Text>
          </Group>
          <Group gap="sm">
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
