import { Alert, Button, Code, Group, List, Modal, ScrollArea, Stack, Tabs, Text, TextInput } from '@mantine/core';
import { IconCheck, IconFolder } from '@tabler/icons-react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { ApiError, api } from '../api';
import { FolderPicker } from '../components/FolderPicker';
import { toScript, toSpecDocument, type WizardState } from '../model';
import { secretVariable, usesSecret } from '../routes';
import type { SaveResult } from '../types';

type Props = {
  state: WizardState;
  setState: (update: (s: WizardState) => WizardState) => void;
};

export function SaveStep({ state, setState }: Props) {
  const [picking, setPicking] = useState(false);
  const [confirmReplace, setConfirmReplace] = useState<string[] | null>(null);
  const [saved, setSaved] = useState<SaveResult | null>(null);
  const spec = toSpecDocument(state);
  const script = toScript(state);

  const preview = useQuery({
    queryKey: ['preview', spec, script],
    queryFn: () => api.preview(spec, script),
    retry: false,
  });
  const save = useMutation({
    mutationFn: (overwrite: boolean) => api.save(spec, state.saveDirectory, state.fileName, overwrite, script),
    onSuccess: (result) => {
      setSaved(result);
      setConfirmReplace(null);
      setState((s) => ({ ...s, savedPath: result.path }));
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        const body = error.body as { path: string; paths?: string[] };
        setConfirmReplace(body.paths ?? [body.path]);
      }
    },
  });
  const set = (patch: Partial<WizardState>) => {
    setSaved(null);
    setState((s) => ({ ...s, ...patch }));
  };

  return (
    <Stack gap="md">
      <TextInput
        label="Name of the system"
        description="Shown at the top of every report."
        value={state.specName}
        onChange={(e) => set({ specName: e.currentTarget.value })}
      />
      <Group align="flex-end" wrap="nowrap">
        <TextInput
          flex={1}
          label="Save in"
          description={
            script
              ? 'Your project is a good place: the load test is written next to the spec, and the results land there too.'
              : 'Your project is a good place: the measured values and the results are written next to the spec.'
          }
          value={state.saveDirectory}
          onChange={(e) => set({ saveDirectory: e.currentTarget.value })}
        />
        <Button variant="default" leftSection={<IconFolder size={16} />} onClick={() => setPicking(true)}>
          Browse
        </Button>
      </Group>
      <TextInput
        label="File name"
        value={state.fileName}
        onChange={(e) => set({ fileName: e.currentTarget.value })}
      />

      {preview.isError ? (
        <Alert color="yellow" title="Not ready to save">
          {preview.error.message}
        </Alert>
      ) : (
        <Tabs defaultValue="spec" keepMounted={false}>
          <Tabs.List>
            <Tabs.Tab value="spec">The spec ({state.fileName || 'spec.toml'})</Tabs.Tab>
            {script && <Tabs.Tab value="script">The load test (load.js)</Tabs.Tab>}
          </Tabs.List>
          <Tabs.Panel value="spec" pt="xs">
            <ScrollArea.Autosize mah={420} type="auto">
              <Code block>{preview.data?.toml ?? '…'}</Code>
            </ScrollArea.Autosize>
          </Tabs.Panel>
          {script && (
            <Tabs.Panel value="script" pt="xs">
              <ScrollArea.Autosize mah={420} type="auto">
                <Code block>{preview.data?.script ?? '…'}</Code>
              </ScrollArea.Autosize>
            </Tabs.Panel>
          )}
        </Tabs>
      )}

      <Group>
        <Button
          leftSection={<IconCheck size={16} />}
          onClick={() => save.mutate(false)}
          loading={save.isPending}
          disabled={preview.isError || !state.saveDirectory.trim() || !state.fileName.trim()}
        >
          {script ? 'Save the spec and the load test' : 'Save the spec'}
        </Button>
      </Group>
      {save.isError && !(save.error instanceof ApiError && save.error.status === 409) && (
        <Alert color="red">{save.error.message}</Alert>
      )}
      {saved && state.savedPath && (
        <Alert color="teal" title="Saved">
          <Text size="sm">{saved.path}</Text>
          {saved.scriptPath && <Text size="sm">{saved.scriptPath}</Text>}
          {[saved.backup, saved.scriptBackup].filter(Boolean).map((b) => (
            <Text key={b} size="sm" c="dimmed">
              The file it replaced is kept as {b}.
            </Text>
          ))}
          <CommandLine path={saved.path} secret={state.load.script === 'generate' && usesSecret(state.auth) ? secretVariable(state.auth) : null} />
        </Alert>
      )}

      <Modal opened={confirmReplace !== null} onClose={() => setConfirmReplace(null)} title="Replace the existing files?">
        <Stack>
          <Text size="sm">These already exist. Each is kept as a dated .bak copy next to it.</Text>
          <List size="sm">
            {confirmReplace?.map((p) => (
              <List.Item key={p}>{p}</List.Item>
            ))}
          </List>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmReplace(null)}>
              Keep them
            </Button>
            <Button color="orange" onClick={() => save.mutate(true)} loading={save.isPending}>
              Replace
            </Button>
          </Group>
        </Stack>
      </Modal>
      <FolderPicker
        opened={picking}
        title="Save the spec in"
        start={state.saveDirectory}
        onClose={() => setPicking(false)}
        onPick={(path) => {
          set({ saveDirectory: path });
          setPicking(false);
        }}
      />
      <Text size="xs" c="dimmed">
        A folder that doesn't exist yet is created when you save.
      </Text>
    </Stack>
  );
}

/** The same run from a terminal: from the analyzer's folder, results next to the spec, the secret set. */
function CommandLine({ path, secret }: { path: string; secret: string | null }) {
  const folder = path.replace(/[\\/][^\\/]*$/, '');
  const separator = path.includes('\\') ? '\\' : '/';
  return (
    <>
      <Text size="sm" mt="sm">
        Also usable from a terminal, in the Microservice-Scripts folder:
      </Text>
      <Code block>
        {`${secret ? `${secret}=… ` : ''}python3 -m analyzer ${path} --out ${folder}${separator}analysis`}
      </Code>
      <Text size="xs" c="dimmed">
        That is the full run; add --skip-chaos for the load test only.
        {secret ? ` ${secret} is the secret the load test logs in with (never written to a file).` : ''}
      </Text>
    </>
  );
}
