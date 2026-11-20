import {
  ActionIcon,
  Badge,
  Button,
  Collapse,
  Group,
  NumberInput,
  Paper,
  SimpleGrid,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from '@mantine/core';
import { IconArrowBackUp, IconChevronDown, IconChevronUp, IconHeartbeat } from '@tabler/icons-react';
import type { ReactNode } from 'react';
import { useState } from 'react';
import { FOLDED, KINDS } from '../kinds';

/**
 * What service, infrastructure and not modelled mean, and what the markers on
 * each row say: for anyone rearranging what the app sorted.
 */
export function KindLegend() {
  const [open, setOpen] = useState(true);
  return (
    <Paper withBorder p="md">
      <Group justify="space-between">
        <Title order={5}>How to sort them</Title>
        <Button
          variant="subtle"
          size="xs"
          rightSection={open ? <IconChevronUp size={14} /> : <IconChevronDown size={14} />}
          onClick={() => setOpen((o) => !o)}
        >
          {open ? 'Hide' : 'Show'} the legend
        </Button>
      </Group>
      <Collapse expanded={open}>
        <Text size="sm" mt="xs">
          Rule of thumb: follow a user's request. Code your team wrote that it passes through is a <b>service</b>;
          someone else's software it passes through (a gateway, a database) is <b>infrastructure</b>; whatever it never
          passes through is <b>not modelled</b>.
        </Text>
        <SimpleGrid cols={{ base: 1, md: 3 }} mt="sm" spacing="sm">
          {(['service', 'infrastructure', 'notModelled'] as const).map((kind) => {
            const k = KINDS[kind];
            return (
              <Paper key={kind} withBorder p="sm" style={{ borderTop: `3px solid var(--mantine-color-${k.color}-6)` }}>
                <Group gap="xs" mb={6}>
                  <ThemeIcon color={k.color} variant="light" size="sm">
                    <k.Icon size={14} />
                  </ThemeIcon>
                  <Text fw={600}>{k.label}</Text>
                </Group>
                <Stack gap={4}>
                  <Text size="sm">{k.what}</Text>
                  <Text size="sm" c="dimmed">
                    {k.effect}
                  </Text>
                  <Text size="xs" c="dimmed">
                    e.g. {k.examples}
                  </Text>
                </Stack>
              </Paper>
            );
          })}
        </SimpleGrid>
        <Text fw={600} size="sm" mt="md" mb={6}>
          On each row
        </Text>
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg" verticalSpacing={6}>
          {(['replica', 'in front'] as const).map((role) => (
            <Marker
              key={role}
              mark={
                <Badge size="xs" variant="light" color={FOLDED[role].color}>
                  {FOLDED[role].label}
                </Badge>
              }
            >
              {FOLDED[role].help}
            </Marker>
          ))}
          <Marker
            mark={
              <ActionIcon size="xs" variant="subtle" component="span" aria-hidden>
                <IconArrowBackUp size={12} />
              </ActionIcon>
            }
          >
            Takes a folded one back out, to analyse it on its own: a pooler you want counted as a single point of
            failure, say.
          </Marker>
          <Marker
            mark={
              <Badge size="xs" variant="light" color="teal" leftSection={<IconHeartbeat size={10} />}>
                healthy
              </Badge>
            }
          >
            It has a healthcheck: after fault injection the analysis waits until it reports healthy.
          </Marker>
          <Marker mark={<NumberInput size="xs" w={64} value={8000} readOnly aria-hidden hideControls />}>
            No healthcheck: it counts as recovered when this port accepts connections again. The note under it says
            where the number came from; correct it if it's wrong.
          </Marker>
          <Marker mark={<NumberInput size="xs" w={64} value={2} readOnly aria-hidden hideControls />}>
            Instances of infrastructure that take over for each other. Count only real failover: claimed redundancy
            makes the availability figures fiction.
          </Marker>
        </SimpleGrid>
      </Collapse>
    </Paper>
  );
}

function Marker({ mark, children }: { mark: ReactNode; children: ReactNode }) {
  return (
    <Group gap="sm" wrap="nowrap" align="flex-start">
      <div style={{ flex: '0 0 76px', display: 'flex', justifyContent: 'flex-end', paddingTop: 2 }}>{mark}</div>
      <Text size="sm">{children}</Text>
    </Group>
  );
}
