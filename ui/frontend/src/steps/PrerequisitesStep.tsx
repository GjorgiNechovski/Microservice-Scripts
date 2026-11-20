import { Alert, Button, Group, Loader, Paper, Stack, Text, ThemeIcon } from '@mantine/core';
import { IconAlertTriangle, IconCheck, IconX } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useEffect } from 'react';
import { api } from '../api';
import type { PrerequisiteStatus } from '../model';

type Props = { onStatus: (status: PrerequisiteStatus) => void };

export function PrerequisitesStep({ onStatus }: Props) {
  const checks = useQuery({ queryKey: ['prerequisites'], queryFn: api.prerequisites });

  useEffect(() => {
    if (checks.isFetching || (checks.isPending && !checks.isError)) onStatus('checking');
    else onStatus(checks.data?.every((c) => c.ok || !c.required) ? 'ready' : 'missing');
  }, [checks.data, checks.isFetching, checks.isPending, checks.isError, onStatus]);

  return (
    <Stack gap="md">
      <Text>The analyzer needs a few things on this machine. Nothing is installed or started here.</Text>
      {checks.isPending && <Loader size="sm" />}
      {checks.isError && <Alert color="red">{checks.error.message}</Alert>}
      <Stack gap="xs">
        {checks.data?.map((check) => (
          <Paper key={check.id} withBorder p="sm">
            <Group align="flex-start" wrap="nowrap">
              <ThemeIcon
                color={check.ok ? 'teal' : check.required ? 'red' : 'yellow'}
                variant="light"
                radius="xl"
              >
                {check.ok ? <IconCheck size={16} /> : check.required ? <IconX size={16} /> : <IconAlertTriangle size={16} />}
              </ThemeIcon>
              <div>
                <Text fw={500}>
                  {check.label}
                  {!check.required && (
                    <Text span size="sm" c="dimmed">
                      {' '}
                      (optional)
                    </Text>
                  )}
                </Text>
                <Text size="sm" c="dimmed">
                  {check.detail}
                </Text>
                {check.fix && (
                  <Text size="sm" mt={4}>
                    {check.fix}
                  </Text>
                )}
              </div>
            </Group>
          </Paper>
        ))}
      </Stack>
      <Group>
        <Button variant="default" onClick={() => checks.refetch()} loading={checks.isFetching}>
          Check again
        </Button>
      </Group>
    </Stack>
  );
}
