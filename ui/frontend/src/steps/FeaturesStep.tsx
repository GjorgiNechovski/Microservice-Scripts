import { Alert, Badge, Button, Group, MultiSelect, Paper, Stack, Text, TextInput } from '@mantine/core';
import { IconArrowBackUp } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { KINDS } from '../kinds';
import type { WizardState } from '../model';
import {
  componentOf,
  featureComponents,
  featureList,
  isModelled,
  renameFeature,
  selectedRoutes,
  suggestComponents,
} from '../routes';
import type { DraftComponent } from '../types';

type Props = {
  state: WizardState;
  setState: (update: (s: WizardState) => WizardState) => void;
};

export function FeaturesStep({ state, setState }: Props) {
  const services = state.read?.draft?.services ?? {};
  const picked = selectedRoutes(state.routes, state.components);
  const features = featureList(picked);
  const modelled = state.components.filter(isModelled);
  const options = (['service', 'infrastructure'] as const).map((kind) => ({
    group: KINDS[kind].label,
    items: modelled.filter((c) => c.kind === kind).map((c) => ({ value: c.id, label: c.name })),
  }));

  return (
    <Stack gap="lg">
      <Text>
        A feature is up only while <b>every</b> component it needs is up, so this decides every availability figure
        and every single point of failure in the reports. Each one starts with what its requests touch: the gateways
        they pass, the service that handles them, and what that service depends on. Change it where you know better.
      </Text>
      <Text size="sm" c="dimmed">
        Requests that share a feature name in the previous step are one feature here: rename one to merge it into
        another.
      </Text>

      {features.map((f) => {
        const chosen = featureComponents(f.name, picked, state.features, state.components, services);
        const edited = state.features[f.name] !== undefined;
        const reasons = new Map<string, string[]>();
        for (const r of f.routes) {
          for (const s of suggestComponents(r, state.components, services)) {
            reasons.set(s.id, [...(reasons.get(s.id) ?? []), s.reason]);
          }
        }
        return (
          <Paper key={f.name} withBorder p="md" data-feature={f.name}>
            <Group justify="space-between" align="flex-start" wrap="nowrap">
              <FeatureName
                name={f.name}
                taken={features.map((x) => x.name)}
                onRename={(to) =>
                  setState((s) => ({ ...s, ...renameFeature(s.routes, s.features, f.name, to) }))
                }
              />
              <Group gap={6} justify="flex-end" style={{ flex: 1 }}>
                {f.routes.map((r) => (
                  <Badge key={r.id} variant="outline" color="gray" tt="none" ff="monospace">
                    {r.method} {r.path}
                  </Badge>
                ))}
              </Group>
            </Group>
            <MultiSelect
              mt="sm"
              label="It needs"
              aria-label={`components of ${f.name}`}
              data={options}
              value={chosen}
              searchable
              onChange={(ids) => setState((s) => ({ ...s, features: { ...s.features, [f.name]: ids } }))}
              error={chosen.length === 0 ? 'Pick at least one component.' : undefined}
            />
            <Stack gap={2} mt="xs">
              {chosen.map((id) => (
                <Reason key={id} component={componentOf(id, state.components)} reasons={reasons.get(id)} />
              ))}
            </Stack>
            {edited && (
              <Group mt="xs" gap="xs">
                <Text size="xs" c="dimmed">
                  Changed by you.
                </Text>
                <Button
                  size="compact-xs"
                  variant="subtle"
                  leftSection={<IconArrowBackUp size={12} />}
                  onClick={() =>
                    setState((s) => {
                      const { [f.name]: _, ...rest } = s.features;
                      return { ...s, features: rest };
                    })
                  }
                >
                  Back to the suggestion
                </Button>
              </Group>
            )}
          </Paper>
        );
      })}

      {features.length === 0 && (
        <Alert color="yellow">Pick at least one route in the previous step: each one belongs to a feature.</Alert>
      )}
    </Stack>
  );
}

function Reason({ component, reasons }: { component: DraftComponent | undefined; reasons: string[] | undefined }) {
  if (!component) return null;
  const kind = KINDS[component.kind];
  return (
    <Group gap={6} wrap="nowrap">
      <kind.Icon size={14} color={`var(--mantine-color-${kind.color}-6)`} style={{ flexShrink: 0 }} />
      <Text size="xs">
        <b>{component.name}</b>
        <Text span c="dimmed" size="xs">
          : {reasons ? [...new Set(reasons)].join('; ') : 'added by you'}
        </Text>
      </Text>
    </Group>
  );
}

/** A feature's name, renamed when the field is left: typing mustn't merge features letter by letter. */
function FeatureName({ name, taken, onRename }: { name: string; taken: string[]; onRename: (to: string) => void }) {
  const [value, setValue] = useState(name);
  useEffect(() => setValue(name), [name]);
  const valid = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(value);
  const commit = () => {
    if (valid && value !== name) onRename(value);
    else setValue(name);
  };
  return (
    <Stack gap={2}>
      <TextInput
        w={280}
        aria-label={`name of feature ${name}`}
        value={value}
        error={!valid ? "Letters, digits, '-', '_' and '.' only." : undefined}
        onChange={(e) => setValue(e.currentTarget.value)}
        onBlur={commit}
        onKeyDown={(e) => e.key === 'Enter' && commit()}
        styles={{ input: { fontWeight: 600 } }}
      />
      {value !== name && taken.includes(value) && valid && (
        <Text size="xs" c="dimmed">
          Merges it into {value}.
        </Text>
      )}
    </Stack>
  );
}
