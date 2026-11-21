import { Alert, Button, Group, NumberInput, Paper, SegmentedControl, Stack, TagsInput, Text, Title } from '@mantine/core';
import { IconClock } from '@tabler/icons-react';
import { useState } from 'react';
import type { WizardState } from '../model';
import { estimatedMinutes, loadProblems, type LoadSettings } from '../routes';

type Props = {
  state: WizardState;
  setState: (update: (s: WizardState) => WizardState) => void;
};

const PRESETS: { label: string; users: number[]; help: string }[] = [
  { label: '1, 2, 5, 10', users: [1, 2, 5, 10], help: 'a first look, or a small app with no pause' },
  { label: '5, 10, 20, 50', users: [5, 10, 20, 50], help: 'a medium app, or a short pause' },
  { label: '50, 100, 200, 500', users: [50, 100, 200, 500], help: 'a big app, or people-like pauses' },
];

const DURATIONS = ['1m', '2m', '3m', '5m'];

/** Whole numbers from what was typed, smallest first, each once. */
export function parseUsers(values: string[]): number[] {
  return [...new Set(values.map((v) => Number(v.trim())).filter((n) => Number.isInteger(n) && n > 0))].sort(
    (a, b) => a - b,
  );
}

export function UsersStep({ state, setState }: Props) {
  const load = state.load;
  const [leftOut, setLeftOut] = useState<string[]>([]);
  const set = (patch: Partial<LoadSettings>) => setState((s) => ({ ...s, load: { ...s.load, ...patch } }));
  const problems = loadProblems(load);
  const sweep = load.users.length > 1;

  return (
    <Stack gap="lg">
      <Text>
        The load test runs <b>virtual users</b>: each sends a request, waits for the answer, pauses, and sends the
        next. Holding several numbers of users in turn (a <b>sweep</b>) shows where the requests per second stop
        rising: that is the bottleneck, and from it how many copies each service needs.
      </Text>

      <Paper withBorder p="md">
        <Title order={5}>How many users</Title>
        <Text size="sm" c="dimmed" mb="xs">
          One number per level, held one after the other. One number alone is a single run.
        </Text>
        <TagsInput
          aria-label="numbers of users"
          value={load.users.map(String)}
          onChange={(values) => {
            const users = parseUsers(values);
            setLeftOut(values.filter((v) => !users.includes(Number(v.trim()))));
            set({ users });
          }}
          splitChars={[',', ' ']}
          placeholder="Type a number, then Enter"
          clearable
        />
        {leftOut.length > 0 && (
          <Text size="sm" c="orange" mt={4}>
            Left out {leftOut.map((v) => `“${v}”`).join(', ')}: each level is a whole number of users, 1 or more.
          </Text>
        )}
        <Group gap="xs" mt="xs">
          <Text size="sm" c="dimmed">
            Start from:
          </Text>
          {PRESETS.map((p) => (
            <Button key={p.label} size="compact-sm" variant="default" onClick={() => set({ users: p.users })} title={p.help}>
              {p.label}
            </Button>
          ))}
        </Group>
      </Paper>

      <Paper withBorder p="md">
        <Title order={5}>How long each level lasts</Title>
        <Text size="sm" c="dimmed" mb="xs">
          At least 2 minutes for numbers you can trust: a rate needs several readings, and the first seconds of a level
          are warm-up.
        </Text>
        <SegmentedControl
          value={DURATIONS.includes(load.duration) ? load.duration : '2m'}
          onChange={(duration) => set({ duration })}
          data={DURATIONS.map((d) => ({ value: d, label: d.replace('m', ' min') }))}
        />
      </Paper>

      <Paper withBorder p="md">
        <Title order={5}>Pause between a user's requests</Title>
        <NumberInput
          mt="xs"
          w={200}
          aria-label="pause in seconds"
          suffix=" s"
          min={0}
          step={0.5}
          decimalScale={1}
          value={load.thinkTime}
          onChange={(v) => set({ thinkTime: typeof v === 'number' ? v : Number(v) || 0 })}
        />
        <Text size="sm" c="dimmed" mt="xs">
          <b>0</b> measures capacity: users send as fast as the app answers, so even a few load it fully. A pause of 2
          to 5 seconds is like people using it; the numbers of users then need to be much higher. Compare only runs
          made with the same pause.
        </Text>
      </Paper>

      <Alert color="blue" variant="light" title="Not sure where to start?">
        With no pause, a small app often stops speeding up below 5 users, so start with 1, 2, 5, 10 and look at where
        the requests per second stop rising. Then sweep again around that point.
      </Alert>

      {problems.length === 0 && (
        <Group gap="xs">
          <IconClock size={18} />
          <Text size="sm">
            The load test takes about <b>{estimatedMinutes(load)} minutes</b>
            {sweep ? ` (${load.users.length} levels of ${load.duration})` : ''}. Fault injection, if you choose it,
            adds a few more.
          </Text>
        </Group>
      )}
    </Stack>
  );
}
