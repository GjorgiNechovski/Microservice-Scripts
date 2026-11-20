import { Alert, Badge, Button, Group, Loader, Modal, ScrollArea, Stack, Text, TextInput, UnstyledButton } from '@mantine/core';
import { IconArrowUp, IconFolder } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { api } from '../api';

type Props = {
  opened: boolean;
  title: string;
  /** Where to start; the home folder when empty. */
  start?: string;
  onClose: () => void;
  onPick: (path: string) => void;
};

/**
 * Browses folders on this machine through the backend: a browser can't give a
 * page the real path of a folder it picks.
 */
export function FolderPicker({ opened, title, start, onClose, onPick }: Props) {
  const [path, setPath] = useState<string | undefined>(start || undefined);
  const [typed, setTyped] = useState('');
  const listing = useQuery({
    queryKey: ['folders', path],
    queryFn: () => api.folders(path),
    enabled: opened,
  });

  useEffect(() => {
    if (listing.data) setTyped(listing.data.path);
  }, [listing.data]);

  return (
    <Modal opened={opened} onClose={onClose} title={title} size="lg">
      <Stack gap="sm">
        <TextInput
          label="Folder"
          description="Type or paste a path and press Enter, or click your way there."
          value={typed}
          onChange={(e) => setTyped(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') setPath(typed);
          }}
        />
        <Group gap="xs">
          <Button
            size="xs"
            variant="default"
            leftSection={<IconArrowUp size={14} />}
            disabled={!listing.data?.parent}
            onClick={() => listing.data?.parent && setPath(listing.data.parent)}
          >
            Up
          </Button>
          {listing.data?.roots.map((root) => (
            <Button key={root} size="xs" variant="subtle" onClick={() => setPath(root)}>
              {root}
            </Button>
          ))}
        </Group>
        {listing.isError && <Alert color="red">{listing.error.message}</Alert>}
        <ScrollArea h={320} type="auto">
          {listing.isPending && <Loader size="sm" />}
          <Stack gap={2}>
            {listing.data?.folders.map((f) => (
              <UnstyledButton key={f.path} onClick={() => setPath(f.path)} px="xs" py={4}>
                <Group gap="xs" wrap="nowrap">
                  <IconFolder size={16} />
                  <Text size="sm">{f.name}</Text>
                  {f.hasCompose && (
                    <Badge size="xs" variant="light">
                      compose file
                    </Badge>
                  )}
                </Group>
              </UnstyledButton>
            ))}
            {listing.data && listing.data.folders.length === 0 && (
              <Text size="sm" c="dimmed">
                No folders in here.
              </Text>
            )}
          </Stack>
        </ScrollArea>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!listing.data} onClick={() => listing.data && onPick(listing.data.path)}>
            Use this folder
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
