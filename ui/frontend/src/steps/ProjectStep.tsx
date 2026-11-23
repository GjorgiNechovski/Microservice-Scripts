import {
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Collapse,
  Group,
  Loader,
  Paper,
  Pill,
  Stack,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { IconAlertCircle, IconCheck, IconFile, IconFolderPlus, IconNetwork } from '@tabler/icons-react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { FolderPicker } from '../components/FolderPicker';
import { mergeScan, selections, withRead, type WizardState } from '../model';
import type { KnownProject } from '../types';

type Props = {
  state: WizardState;
  setState: (update: (s: WizardState) => WizardState) => void;
};

const ROLE_LABEL = { base: 'main file', override: 'override', overlay: 'overlay' } as const;

export function ProjectStep({ state, setState }: Props) {
  const [picking, setPicking] = useState(false);
  const [showRunning, setShowRunning] = useState(false);
  const [filePath, setFilePath] = useState('');

  // only the latest search counts: an earlier, slower one finishing last would undo it
  const latestScan = useRef(0);
  const scan = useMutation({
    mutationFn: async (seq: number) => ({ seq, result: await api.scan(state.folders, state.extraFiles, state.knownProjects) }),
    onSuccess: ({ seq, result }) => {
      if (seq !== latestScan.current) return;
      setState((s) => ({ ...s, scan: result, projects: mergeScan(s.projects, result), read: null, components: [] }));
    },
  });
  const running = useQuery({ queryKey: ['compose-projects'], queryFn: api.composeProjects, enabled: showRunning });
  const read = useMutation({
    mutationFn: () => api.draft(selections(state.projects)),
    onSuccess: (response) => setState((s) => withRead(s, response)),
  });

  // search again whenever the folders or pointed-at files change
  const { mutate: rescan } = scan;
  useEffect(() => {
    if (state.folders.length + state.extraFiles.length + state.knownProjects.length > 0) rescan(++latestScan.current);
  }, [state.folders, state.extraFiles, state.knownProjects, rescan]);

  const addFolder = (path: string) =>
    setState((s) => (s.folders.includes(path) ? s : { ...s, folders: [...s.folders, path] }));
  const addFiles = (files: string[]) =>
    setState((s) => ({ ...s, extraFiles: [...new Set([...s.extraFiles, ...files])] }));
  // as Docker started it: under its own name (-p), its files together even when they sit in different folders
  const addKnown = (project: KnownProject) =>
    setState((s) =>
      s.knownProjects.some((k) => k.name === project.name)
        ? s
        : { ...s, knownProjects: [...s.knownProjects, project] },
    );
  const toggleFile = (directory: string, path: string, selected: boolean) =>
    setState((s) => ({
      ...s,
      read: null,
      projects: s.projects.map((p) =>
        p.directory !== directory
          ? p
          : { ...p, files: p.files.map((f) => (f.path === path ? { ...f, selected } : f)) },
      ),
    }));
  const rename = (directory: string, name: string) =>
    setState((s) => ({
      ...s,
      read: null,
      projects: s.projects.map((p) => (p.directory === directory ? { ...p, name } : p)),
    }));

  const chosen = selections(state.projects);
  const outside = (state.scan?.dockerfiles ?? []).filter(
    (d) => !chosen.some((p) => d.startsWith(p.directory)),
  );

  return (
    <Stack gap="lg">
      <Stack gap="xs">
        <Title order={4}>Where is your app?</Title>
        <Text size="sm" c="dimmed">
          Add the folder of each repository your app's services live in, or one folder that holds them all.
          Only these folders are searched for compose files and Dockerfiles.
        </Text>
        <Group gap="xs">
          {state.folders.map((f) => (
            <Pill
              key={f}
              withRemoveButton
              onRemove={() => setState((s) => ({ ...s, folders: s.folders.filter((x) => x !== f), read: null }))}
            >
              {f}
            </Pill>
          ))}
          {state.extraFiles.map((f) => (
            <Pill
              key={f}
              withRemoveButton
              onRemove={() => setState((s) => ({ ...s, extraFiles: s.extraFiles.filter((x) => x !== f), read: null }))}
            >
              {f}
            </Pill>
          ))}
          {state.knownProjects.map((k) => (
            <Pill
              key={k.name}
              withRemoveButton
              onRemove={() =>
                setState((s) => ({ ...s, knownProjects: s.knownProjects.filter((x) => x.name !== k.name), read: null }))
              }
            >
              project {k.name}
            </Pill>
          ))}
        </Group>
        <Group gap="xs">
          <Button leftSection={<IconFolderPlus size={16} />} onClick={() => setPicking(true)}>
            Add a folder
          </Button>
          <Button variant="default" onClick={() => setShowRunning((v) => !v)}>
            {showRunning ? 'Hide' : 'Show'} compose projects Docker knows
          </Button>
        </Group>
        <Collapse expanded={showRunning}>
          <Paper withBorder p="sm">
            <Text size="xs" c="dimmed" mb="xs">
              Only compose projects that have been started on this machine at least once (running or stopped): Docker
              doesn't know an app that has never been started. For one of those, add its folder.
            </Text>
            {running.isPending && <Loader size="sm" />}
            {running.isError && (
              <Text size="sm" c="orange">
                {running.error.message}
              </Text>
            )}
            {running.data?.length === 0 && (
              <Text size="sm">None yet: no compose project has been started on this machine. Add your app's folder.</Text>
            )}
            <Stack gap={6}>
              {running.data?.map((p) => (
                <Group key={p.name} justify="space-between" wrap="nowrap">
                  <div>
                    <Text size="sm" fw={500}>
                      {p.name}{' '}
                      <Text span size="xs" c="dimmed">
                        {p.status}
                      </Text>
                    </Text>
                    <Text size="xs" c="dimmed">
                      {p.files.join(', ')}
                    </Text>
                  </div>
                  <Button size="xs" variant="light" disabled={p.files.length === 0} onClick={() => addKnown({ name: p.name, files: p.files })}>
                    Use its files
                  </Button>
                </Group>
              ))}
            </Stack>
          </Paper>
        </Collapse>
        <Group gap="xs" align="flex-end">
          <TextInput
            flex={1}
            label="Or point at a compose file directly"
            description="For a compose file the search can't recognise by its name, or one outside these folders."
            placeholder="/path/to/stack.yml"
            value={filePath}
            onChange={(e) => setFilePath(e.currentTarget.value)}
          />
          <Button
            variant="default"
            leftSection={<IconFile size={16} />}
            disabled={!filePath.trim()}
            onClick={() => {
              addFiles([filePath.trim()]);
              setFilePath('');
            }}
          >
            Add file
          </Button>
        </Group>
      </Stack>

      {scan.isPending && <Loader size="sm" />}
      {scan.isError && <Alert color="red">{scan.error.message}</Alert>}
      {state.scan?.warnings.map((w) => (
        <Alert key={w} color="yellow" icon={<IconAlertCircle size={16} />}>
          {w}
        </Alert>
      ))}

      {state.scan && (
        <Stack gap="xs">
          <Title order={4}>Compose files found</Title>
          {state.projects.length === 0 && (
            <Text size="sm">No compose files in these folders. Add another folder, or point at the file directly.</Text>
          )}
          {state.projects.map((p) => {
            const result = state.read?.projects.find((r) => r.directory === p.directory);
            return (
              <Paper key={p.directory} withBorder p="sm">
                <Stack gap={6}>
                  <Group justify="space-between" wrap="nowrap">
                    <Text size="sm" fw={500} style={{ wordBreak: 'break-all' }}>
                      {p.directory}
                    </Text>
                    {result?.ok && (
                      <Badge color="teal" variant="light" leftSection={<IconCheck size={12} />}>
                        {result.name}: {result.services} services
                      </Badge>
                    )}
                  </Group>
                  {p.files.map((f) => (
                    <Checkbox
                      key={f.path}
                      checked={f.selected}
                      onChange={(e) => toggleFile(p.directory, f.path, e.currentTarget.checked)}
                      label={
                        <Group gap={6}>
                          <Text size="sm">{f.name}</Text>
                          <Badge size="xs" variant="outline">
                            {ROLE_LABEL[f.role]}
                          </Badge>
                        </Group>
                      }
                    />
                  ))}
                  <TextInput
                    size="xs"
                    w={320}
                    label="Project name"
                    description="Only if you start it with docker compose -p NAME"
                    placeholder="decided by Compose"
                    value={p.name}
                    onChange={(e) => rename(p.directory, e.currentTarget.value)}
                  />
                  {result && !result.ok && (
                    <Alert color="red" title="Compose couldn't read this project">
                      <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>
                        {result.error}
                      </Text>
                    </Alert>
                  )}
                </Stack>
              </Paper>
            );
          })}
          {outside.length > 0 && (
            <Alert color="blue" title="Dockerfiles outside the chosen compose projects">
              <Text size="sm">
                If one of these is a service of your app, its repository's compose file is missing: add that folder
                or file.
              </Text>
              {outside.slice(0, 10).map((d) => (
                <Text key={d} size="xs" c="dimmed">
                  {d}
                </Text>
              ))}
            </Alert>
          )}
          <Group>
            <Button onClick={() => read.mutate()} loading={read.isPending} disabled={chosen.length === 0}>
              Read the chosen compose files
            </Button>
            {state.read?.draft && (
              <Text size="sm" c="dimmed">
                Read {state.read.draft.components.length} components. Next: check what each one is.
              </Text>
            )}
          </Group>
          {read.isError && <Alert color="red">{read.error.message}</Alert>}
          {state.read?.draft && (
            <NetworkNote networks={state.read.draft.networks} notes={state.read.draft.notes} />
          )}
        </Stack>
      )}

      <FolderPicker
        opened={picking}
        title="Add a folder"
        onClose={() => setPicking(false)}
        onPick={(path) => {
          addFolder(path);
          setPicking(false);
        }}
      />
      <Text size="xs" c="dimmed">
        Compose reads the files itself (<Anchor href="https://docs.docker.com/reference/cli/docker/compose/config/" target="_blank">docker compose config</Anchor>), so
        overlays, profiles and .env variables work as they do for docker compose up. Nothing is started.
      </Text>
    </Stack>
  );
}

function NetworkNote({ networks, notes }: { networks: { name: string; projects: string[] }[]; notes: string[] }) {
  return (
    <Stack gap={6}>
      {networks[0] && (
        <Group gap="xs">
          <IconNetwork size={16} />
          <Text size="sm">
            The monitoring stack will join the network <b>{networks[0].name}</b>
            {networks[0].projects.length > 1 && <> (shared by {networks[0].projects.join(', ')})</>}.
          </Text>
        </Group>
      )}
      {notes.map((n) => (
        <Alert key={n} color="yellow">
          {n}
        </Alert>
      ))}
    </Stack>
  );
}
