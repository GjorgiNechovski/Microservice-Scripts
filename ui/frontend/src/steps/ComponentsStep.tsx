import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  ThemeIcon,
  Title,
  Tooltip,
} from '@mantine/core';
import { IconArrowBackUp, IconHeartbeat, IconInfoCircle } from '@tabler/icons-react';
import { useState } from 'react';
import { KindLegend } from '../components/KindLegend';
import { FOLDED, KINDS } from '../kinds';
import {
  componentBlockers,
  nameProblems,
  servicesNotInDraft,
  setKind,
  unfold,
  updateComponent,
  type WizardState,
} from '../model';
import type { ComponentKind, DraftComponent } from '../types';

type Props = {
  state: WizardState;
  setState: (update: (s: WizardState) => WizardState) => void;
};

const SECTIONS: { kind: ComponentKind; title: string; help: string }[] = [
  {
    kind: 'unsure',
    title: 'Decide what these are',
    help: "The compose files don't say. Pick service, infrastructure, or not modelled (see the legend above).",
  },
  { kind: 'service', title: 'Services', help: KINDS.service.what },
  { kind: 'infrastructure', title: 'Infrastructure', help: KINDS.infrastructure.what },
  { kind: 'notModelled', title: 'Not modelled', help: KINDS.notModelled.what },
];

const KIND_OPTIONS = [
  { value: 'service', label: 'Service' },
  { value: 'infrastructure', label: 'Infrastructure' },
  { value: 'notModelled', label: 'Not modelled' },
];

export function ComponentsStep({ state, setState }: Props) {
  const [adding, setAdding] = useState<string | null>(null);
  const draft = state.read?.draft;
  if (!draft) {
    return <Text>Read the compose files in the previous step first.</Text>;
  }

  const components = state.components;
  const problems = nameProblems(components);
  const blockers = componentBlockers(components);
  const notInDraft = servicesNotInDraft(components, draft.services);
  const change = (update: (c: DraftComponent[]) => DraftComponent[]) =>
    setState((s) => ({ ...s, components: update(s.components), savedPath: null }));

  return (
    <Stack gap="lg">
      <Text>
        Each compose service has been sorted and its reason is shown. Change anything that's wrong: the analysis is
        only as good as this picture of the system.
      </Text>
      <KindLegend />
      {blockers.length > 0 && (
        <Alert color="yellow" title="Before you go on">
          {blockers.map((b) => (
            <Text key={b} size="sm">
              {b}
            </Text>
          ))}
        </Alert>
      )}

      {SECTIONS.map((section) => {
        const rows = components.filter((c) => c.kind === section.kind);
        if (rows.length === 0) return null;
        const kind = KINDS[section.kind];
        return (
          <Paper
            key={section.kind}
            data-kind={section.kind}
            withBorder
            p="md"
            style={{ borderLeft: `4px solid var(--mantine-color-${kind.color}-6)` }}
          >
            <Group gap="xs">
              <ThemeIcon color={kind.color} variant="light">
                <kind.Icon size={18} />
              </ThemeIcon>
              <Title order={4}>
                {section.title}{' '}
                <Text span c="dimmed" size="sm">
                  ({rows.length})
                </Text>
              </Title>
            </Group>
            <Text size="sm" c="dimmed" mb="sm">
              {section.help}
            </Text>
            <Table verticalSpacing="sm" layout="fixed">
              <Table.Thead>
                <Table.Tr>
                  <Table.Th w="22%">Name</Table.Th>
                  <Table.Th>What it is, and why</Table.Th>
                  <Table.Th w={310}>Is it…</Table.Th>
                  <Table.Th w={150}>Ready when</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {rows.map((c) => (
                  <ComponentRow
                    key={c.id}
                    component={c}
                    problem={problems[c.id]}
                    onChange={(patch) => change((all) => updateComponent(all, c.id, patch))}
                    onKind={(kind) => change((all) => setKind(all, c.id, kind))}
                    onUnfold={(memberId) => change((all) => unfold(all, c.id, memberId, draft.services))}
                  />
                ))}
              </Table.Tbody>
            </Table>
          </Paper>
        );
      })}

      {notInDraft.length > 0 && (
        <Paper withBorder p="md">
          <Title order={5}>Something missing?</Title>
          <Text size="sm" c="dimmed" mb="xs">
            Add a compose service that isn't in the list above. If it's in a compose file that wasn't found, add that
            file in the previous step.
          </Text>
          <Group align="flex-end">
            <Select
              w={360}
              placeholder="Pick a compose service"
              data={notInDraft.map((s) => ({ value: s.id, label: `${s.project} / ${s.composeService}` }))}
              value={adding}
              onChange={setAdding}
              searchable
            />
            <Button
              variant="default"
              disabled={!adding}
              onClick={() => {
                const own = adding ? draft.services[adding] : undefined;
                if (own) change((all) => [...all, { ...own, folded: [], kind: own.kind === 'notModelled' ? 'unsure' : own.kind }]);
                setAdding(null);
              }}
            >
              Add
            </Button>
          </Group>
        </Paper>
      )}
    </Stack>
  );
}

type RowProps = {
  component: DraftComponent;
  problem?: string;
  onChange: (patch: Partial<DraftComponent>) => void;
  onKind: (kind: ComponentKind) => void;
  onUnfold: (memberId: string) => void;
};

function ComponentRow({ component: c, problem, onChange, onKind, onUnfold }: RowProps) {
  const modelled = c.kind === 'service' || c.kind === 'infrastructure';
  const what = c.image ?? (c.builtFromSource ? `built from source${c.language ? ` (${c.language})` : ''}` : '');
  return (
    <Table.Tr>
      <Table.Td>
        <TextInput
          size="xs"
          aria-label={`name of ${c.composeService}`}
          value={c.name}
          error={problem}
          disabled={!modelled && c.kind !== 'unsure'}
          onChange={(e) => onChange({ name: e.currentTarget.value })}
        />
        <Text size="xs" c="dimmed" mt={4}>
          {c.project} / {c.composeService}
        </Text>
      </Table.Td>
      <Table.Td>
        <Text size="sm">{what}</Text>
        {c.reasons.filter((r) => r !== what).map((r) => (
          <Text key={r} size="xs" c="dimmed">
            {r}
          </Text>
        ))}
        {c.folded.length > 0 && (
          <Text size="xs" fw={500} mt={6}>
            Folded into this one:
          </Text>
        )}
        {c.folded.map((f) => (
          <Group key={f.id} gap={6} wrap="nowrap" align="flex-start">
            <Tooltip multiline w={320} label={FOLDED[f.role].help}>
              <Badge size="xs" variant="light" color={FOLDED[f.role].color} style={{ flexShrink: 0, cursor: 'help' }}>
                {FOLDED[f.role].label}
              </Badge>
            </Tooltip>
            <Text size="xs" c="dimmed">
              <b>{f.id.split('/').pop()}</b>: {f.reason}
            </Text>
            <Tooltip label="Model it as a component of its own">
              <ActionIcon size="xs" variant="subtle" aria-label={`separate ${f.id}`} onClick={() => onUnfold(f.id)}>
                <IconArrowBackUp size={12} />
              </ActionIcon>
            </Tooltip>
          </Group>
        ))}
        {c.dependsOn.length > 0 && (
          <Text size="xs" c="dimmed">
            depends on {c.dependsOn.map((d) => d.id.split('/').pop()).join(', ')}
          </Text>
        )}
      </Table.Td>
      <Table.Td>
        <SegmentedControl
          size="xs"
          fullWidth
          color={c.kind === 'unsure' ? undefined : KINDS[c.kind].color}
          data={KIND_OPTIONS}
          value={c.kind === 'unsure' ? '' : c.kind}
          onChange={(v) => onKind(v as ComponentKind)}
        />
        {c.kind === 'infrastructure' && (
          <Group gap={6} mt={8} wrap="nowrap">
            <NumberInput
              size="xs"
              w={70}
              min={1}
              aria-label={`instances of ${c.name}`}
              value={c.instances}
              onChange={(v) => onChange({ instances: typeof v === 'number' && v >= 1 ? v : 1 })}
            />
            <Text size="xs" c="dimmed">
              instance{c.instances === 1 ? '' : 's'}
            </Text>
            <Tooltip
              multiline
              w={280}
              label={`${c.instancesReason ? `${c.instancesReason}. ` : ''}Count instances only if they really fail over for each other: declaring redundancy that doesn't exist makes every availability figure fiction.`}
            >
              <IconInfoCircle size={14} />
            </Tooltip>
          </Group>
        )}
      </Table.Td>
      <Table.Td>
        {c.hasHealthcheck ? (
          <Tooltip label={c.healthcheckSource ?? 'healthcheck'}>
            <Badge variant="light" color="teal" leftSection={<IconHeartbeat size={12} />}>
              healthy
            </Badge>
          </Tooltip>
        ) : modelled || c.kind === 'unsure' ? (
          <>
            <NumberInput
              size="xs"
              min={1}
              max={65535}
              placeholder="port"
              aria-label={`readiness port of ${c.name}`}
              value={c.readinessPort ?? ''}
              onChange={(v) => onChange({ readinessPort: typeof v === 'number' ? v : null, readinessSource: 'set by hand' })}
            />
            <Text size="xs" c="dimmed" mt={2}>
              {c.readinessSource ?? 'port unknown: enter it'}
            </Text>
          </>
        ) : null}
      </Table.Td>
    </Table.Tr>
  );
}
