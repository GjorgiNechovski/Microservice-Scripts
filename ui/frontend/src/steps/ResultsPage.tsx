import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Code,
  CopyButton,
  Group,
  Loader,
  Modal,
  Paper,
  ScrollArea,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  ThemeIcon,
  Title,
  Tooltip,
  UnstyledButton,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconCheck,
  IconCircleCheck,
  IconCircleDashed,
  IconCircleX,
  IconClockHour4,
  IconCopy,
  IconFolderOpen,
  IconRefresh,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { api } from '../api';
import { FolderPicker } from '../components/FolderPicker';
import type { WizardState } from '../model';
import {
  chartInfo,
  freshness,
  parseExplained,
  parseValidity,
  resultLabel,
  textOf,
  verdictTone,
  when,
  type Block,
  type CheckStatus,
} from '../results';
import type { ResultChart, ResultPart, ResultSummary, ResultText, ResultView, RunState } from '../types';

type Props = {
  state: WizardState;
  /** The app's run, so a run that just ended is picked up. */
  run: RunState;
  onGoToRun: () => void;
};

const OPENED_KEY = 'ms-results-folders';

function loadOpened(): string[] {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(OPENED_KEY) ?? '[]');
    return Array.isArray(saved) ? saved.filter((f): f is string => typeof f === 'string') : [];
  } catch {
    return [];
  }
}

function saveOpened(folders: string[]) {
  try {
    localStorage.setItem(OPENED_KEY, JSON.stringify(folders));
  } catch {
    // storage blocked: the folder is still searched until the page is reloaded
  }
}

/**
 * What the analysis found, in plain words first: whether the numbers can be
 * trusted, the summary, the charts, then the technical reports. Reachable at
 * any time: it finds results an earlier run left (in this setup or not) and
 * says when they are older than what the user has now.
 */
export function ResultsPage({ state, run, onGoToRun }: Props) {
  const [opened, setOpened] = useState<string[]>(loadOpened);
  const [picking, setPicking] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [missing, setMissing] = useState<string | null>(null);

  const folders = useMemo(
    () => [...new Set([...opened, ...state.projects.map((p) => p.directory), ...state.folders])],
    [opened, state.projects, state.folders],
  );
  const find = useQuery({
    queryKey: ['results', folders, state.savedPath, run],
    queryFn: () => api.findResults(folders, state.savedPath),
  });
  const results = find.data?.results ?? [];
  const chosen =
    results.find((r) => r.folder === selected) ?? results.find((r) => r.current) ?? results[0] ?? null;

  const view = useQuery({
    queryKey: ['result', chosen?.folder, chosen?.writtenAt, state.savedPath, run],
    queryFn: () => api.result(chosen!.folder, state.savedPath),
    enabled: chosen !== null,
  });

  // a folder the user opened: pick the newest result in it (or under it)
  useEffect(() => {
    if (!missing || !find.data || find.isFetching) return;
    const inside = results.find((r) => r.folder === missing || r.folder.startsWith(`${missing}/`));
    if (inside) {
      setSelected(inside.folder);
      setMissing(null);
    }
  }, [missing, find.data, find.isFetching, results]);

  const open = (folder: string) => {
    const next = [folder, ...opened.filter((f) => f !== folder)].slice(0, 10);
    setOpened(next);
    saveOpened(next);
    setMissing(folder);
    setPicking(false);
  };

  return (
    <Stack gap="lg">
      <Text>
        What the analysis found: first whether its numbers can be trusted, then the summary in plain words, the charts,
        and the technical reports at the end. Results from earlier runs are found too, and marked when they are older
        than your current setup.
      </Text>

      <Paper withBorder p="md">
        <Group align="flex-end" gap="sm">
          <Select
            label="Results"
            placeholder={find.isLoading ? 'Looking for results…' : 'No results found'}
            data={results.map((r) => ({ value: r.folder, label: resultLabel(r) }))}
            value={chosen?.folder ?? null}
            onChange={(v) => v && setSelected(v)}
            allowDeselect={false}
            style={{ flex: 1, minWidth: 260 }}
            disabled={results.length === 0}
            comboboxProps={{ withinPortal: true }}
          />
          <Button variant="default" leftSection={<IconFolderOpen size={16} />} onClick={() => setPicking(true)}>
            Open a folder…
          </Button>
          <Tooltip label="Look for results again">
            <ActionIcon variant="default" size="lg" onClick={() => find.refetch()} loading={find.isFetching} aria-label="Look again">
              <IconRefresh size={16} />
            </ActionIcon>
          </Tooltip>
        </Group>
        {chosen && (
          <Group gap={4} mt="xs" wrap="nowrap">
            <Text size="xs" c="dimmed" truncate>
              In {chosen.folder}
            </Text>
            <CopyButton value={chosen.folder}>
              {({ copied, copy }) => (
                <ActionIcon variant="subtle" size="xs" color={copied ? 'teal' : 'gray'} onClick={copy} aria-label="Copy the folder">
                  {copied ? <IconCheck size={12} /> : <IconCopy size={12} />}
                </ActionIcon>
              )}
            </CopyButton>
          </Group>
        )}
        {missing && find.data && !find.isFetching && (
          <Alert color="yellow" mt="sm" withCloseButton onClose={() => setMissing(null)}>
            There are no analyzer results in {missing} or the few folders below it. They are in the folder the analyzer
            wrote to (its --out): a report.txt, or a scaling-report.txt for a load sweep.
          </Alert>
        )}
      </Paper>

      {find.isError && <Alert color="red">{find.error.message}</Alert>}

      {find.data && results.length === 0 && (
        <Alert color="blue" title="No results yet">
          <Stack gap="xs">
            <Text size="sm">
              Run the analysis to get some, or open the folder an earlier run wrote to. The setup's own run writes them next
              to the spec, in analysis/; the analyzer run by hand writes them to its --out folder (analysis by default).
            </Text>
            <Text size="xs" c="dimmed">
              Looked in: {find.data.searched.join(', ') || 'nowhere yet'} (and a few folders below each).
            </Text>
            <Group>
              <Button size="xs" onClick={onGoToRun}>
                Go to Run
              </Button>
            </Group>
          </Stack>
        </Alert>
      )}

      {chosen && <FreshnessAlert result={view.data?.summary ?? chosen} />}
      {view.isLoading && <Loader size="sm" />}
      {view.isError && <Alert color="red">{view.error.message}</Alert>}
      {view.data && <ResultBody key={view.data.summary.folder} view={view.data} />}

      <FolderPicker
        opened={picking}
        title="Open the folder with the results"
        start={chosen?.folder ?? state.folders[0]}
        onClose={() => setPicking(false)}
        onPick={open}
      />
    </Stack>
  );
}

function FreshnessAlert({ result }: { result: ResultSummary }) {
  const f = freshness(result);
  return (
    <Alert
      color={f.current ? 'teal' : 'yellow'}
      icon={f.current ? <IconCircleCheck size={18} /> : <IconClockHour4 size={18} />}
      title={f.title}
    >
      <Stack gap={4}>
        {f.lines.map((l) => (
          <Text key={l} size="sm">
            {l}
          </Text>
        ))}
      </Stack>
    </Alert>
  );
}

const OVERVIEW = 'overview';

function ResultBody({ view }: { view: ResultView }) {
  const [shown, setShown] = useState<string>(OVERVIEW);
  if (!view.overview) return <PartView part={view.parts[0]} />;

  const part = view.parts.find((p) => String(p.users) === shown);
  return (
    <Stack gap="lg">
      <Stack gap={4}>
        <Text size="sm">
          A load sweep: the test ran at {view.parts.map((p) => p.users).join(', ')} users. See what changed across them,
          or each level on its own.
        </Text>
        <ScrollArea type="auto" offsetScrollbars>
          <SegmentedControl
            value={shown}
            onChange={setShown}
            data={[
              { value: OVERVIEW, label: 'Across all levels' },
              ...view.parts.map((p) => ({ value: String(p.users), label: `${p.users} users` })),
            ]}
          />
        </ScrollArea>
      </Stack>
      {part ? <PartView key={part.folder} part={part} /> : <OverviewView part={view.overview} />}
    </Stack>
  );
}

function OverviewView({ part }: { part: ResultPart }) {
  const explained = textOf(part, 'scaling-report-explained');
  return (
    <Stack gap="lg">
      {explained && <ExplainedView text={explained} />}
      <Charts part={part} />
      <Technical part={part} names={[['scaling-report', 'Scaling report']]} />
    </Stack>
  );
}

function PartView({ part }: { part: ResultPart }) {
  const explained = textOf(part, 'report-explained');
  const comparison = textOf(part, 'comparison-explained');
  const hotspots = textOf(part, 'hotspots-explained');
  return (
    <Stack gap="lg">
      <Trust part={part} />
      {explained ? (
        <ExplainedView text={explained} />
      ) : (
        <Alert color="gray">This run has no plain-language summary: see the technical report below.</Alert>
      )}
      {comparison && (
        <Section title="Compared with your previous run at this level" text={comparison}>
          <ExplainedView text={comparison} plain />
        </Section>
      )}
      <Charts part={part} />
      {hotspots && (
        <Section title="Where each service spends its time" text={hotspots}>
          <ExplainedView text={hotspots} plain />
        </Section>
      )}
      <Technical
        part={part}
        names={[
          ['report', 'Technical report'],
          ['validity', 'Trust checks'],
          ['hotspots', 'Time per request'],
          ['comparison', 'Comparison'],
          ['k6-output', 'Load test (k6)'],
        ]}
      />
    </Stack>
  );
}

/** A card with a heading, and a note when the file is from another run than the report. */
function Section({ title, text, children }: { title: string; text?: ResultText; children: ReactNode }) {
  return (
    <Paper withBorder p="md">
      <Group justify="space-between" mb="xs" wrap="nowrap" align="flex-start">
        <Title order={4}>{title}</Title>
        {text?.otherRun && <OtherRun at={text.writtenAt} />}
      </Group>
      {children}
    </Paper>
  );
}

function OtherRun({ at }: { at: string }) {
  return (
    <Tooltip label="Written by a different run than the report next to it: it may not match the rest of the page." multiline w={280}>
      <Badge color="yellow" variant="light" style={{ flexShrink: 0, cursor: 'help' }}>
        from another run · {when(at)}
      </Badge>
    </Tooltip>
  );
}

const STATUS: Record<CheckStatus, { color: string; icon: ReactNode; label: string }> = {
  PASS: { color: 'teal', icon: <IconCircleCheck size={16} />, label: 'passed' },
  WARN: { color: 'yellow', icon: <IconAlertTriangle size={16} />, label: 'partly' },
  FAIL: { color: 'red', icon: <IconCircleX size={16} />, label: 'failed' },
  SKIP: { color: 'gray', icon: <IconCircleDashed size={16} />, label: 'not checked' },
};

/** Whether the run's numbers can be used at all: first, because everything below depends on it. */
function Trust({ part }: { part: ResultPart }) {
  const raw = textOf(part, 'validity');
  const explained = textOf(part, 'validity-explained');
  const validity = raw ? parseValidity(raw.text) : null;
  if (!validity) {
    return (
      <Alert color="gray" title="Can these numbers be trusted?">
        This run has no trust check: it didn't run the load test (or ran before the analyzer had one).
      </Alert>
    );
  }
  const tone = verdictTone(validity.verdict);
  const plain = explained ? parseExplained(explained.text) : null;
  return (
    <Paper withBorder p="md" style={{ borderColor: `var(--mantine-color-${tone.color}-filled)` }}>
      <Group justify="space-between" wrap="nowrap" align="flex-start" mb="xs">
        <Group gap="sm">
          <Title order={4}>Can these numbers be trusted?</Title>
          <Badge color={tone.color} size="lg">
            {validity.verdict}
          </Badge>
        </Group>
        {raw?.otherRun && <OtherRun at={raw.writtenAt} />}
      </Group>
      {/* the analyzer's own words say it better; ours only when it wrote none */}
      {!plain && (
        <Text fw={600} mb="sm">
          {tone.text}
        </Text>
      )}
      {plain && (
        <Box mb="sm">
          <Blocks blocks={[...plain.intro, ...plain.sections.flatMap((s) => s.blocks)]} />
        </Box>
      )}
      <Stack gap={6}>
        {validity.checks.map((c) => (
          <Group key={c.name} gap="xs" wrap="nowrap" align="flex-start">
            <Tooltip label={STATUS[c.status].label}>
              <ThemeIcon color={STATUS[c.status].color} variant="light" size="sm" radius="xl">
                {STATUS[c.status].icon}
              </ThemeIcon>
            </Tooltip>
            <Text size="sm">
              <b>{c.name}</b>
              {c.detail && <Text span c="dimmed" size="sm">: {c.detail}</Text>}
            </Text>
          </Group>
        ))}
      </Stack>
    </Paper>
  );
}

/** A plain-language file: one card per section, or (plain) its blocks as they are. */
function ExplainedView({ text, plain = false }: { text: ResultText; plain?: boolean }) {
  const doc = parseExplained(text.text);
  if (plain) {
    return (
      <Stack gap="md">
        {doc.intro.length > 0 && <Blocks blocks={doc.intro} />}
        {doc.sections.map((s) => (
          <Box key={s.title}>
            <Text fw={600} mb={4}>
              {s.title}
            </Text>
            <Blocks blocks={s.blocks} />
          </Box>
        ))}
      </Stack>
    );
  }
  return (
    <Stack gap="md">
      {(doc.title || doc.intro.length > 0) && (
        <Section title={doc.title ?? 'Summary'} text={text}>
          <Blocks blocks={doc.intro} />
        </Section>
      )}
      {doc.sections.map((s) => (
        <Section key={s.title} title={s.title}>
          <Blocks blocks={s.blocks} />
        </Section>
      ))}
    </Stack>
  );
}

function Blocks({ blocks }: { blocks: Block[] }) {
  return (
    <Stack gap={6}>
      {blocks.map((b, i) => {
        const pl = b.depth * 16;
        if (b.kind === 'table') {
          return (
            <Table key={i} withRowBorders={false} verticalSpacing={2} horizontalSpacing="sm" ml={pl} w="auto">
              <Table.Tbody>
                {b.rows.map((row, j) => (
                  <Table.Tr key={j}>
                    {row.map((cell, k) => (
                      <Table.Td key={k} fw={k === 0 ? 600 : undefined} style={{ verticalAlign: 'top' }}>
                        {cell}
                      </Table.Td>
                    ))}
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          );
        }
        if (b.kind === 'item') {
          const color = b.marker === '✗' ? 'red' : b.marker === '✓' ? 'teal' : b.marker === '!' ? 'yellow' : undefined;
          return (
            <Group key={i} gap={8} wrap="nowrap" align="flex-start" pl={pl}>
              <Text size="sm" c={color} fw={color ? 700 : undefined} style={{ flexShrink: 0, minWidth: 12 }}>
                {b.marker}
              </Text>
              <Text size="sm">{b.text}</Text>
            </Group>
          );
        }
        return (
          <Text key={i} size="sm" pl={pl}>
            {b.text}
          </Text>
        );
      })}
    </Stack>
  );
}

/** The charts: the ones about the whole system, then those per feature, a feature at a time. */
function Charts({ part }: { part: ResultPart }) {
  const [showOld, setShowOld] = useState(false);
  const charts = part.charts
    .filter((c) => showOld || !c.otherRun)
    .map((c) => ({ chart: c, info: chartInfo(c.name) }))
    .sort((a, b) => a.info.order - b.info.order || a.chart.name.localeCompare(b.chart.name));
  const hiddenOld = part.charts.filter((c) => c.otherRun).length;
  const features = [...new Set(charts.map((c) => c.info.feature).filter((f): f is string => f !== null))];
  const [feature, setFeature] = useState<string | null>(null);
  const shownFeature = feature && features.includes(feature) ? feature : (features[0] ?? null);
  const general = charts.filter((c) => c.info.feature === null);
  const perFeature = charts.filter((c) => c.info.feature === shownFeature);
  if (part.charts.length === 0) return null;

  return (
    <Paper withBorder p="md">
      <Group justify="space-between" mb="xs">
        <Title order={4}>Charts</Title>
        {hiddenOld > 0 && (
          <Button size="compact-xs" variant="subtle" onClick={() => setShowOld((s) => !s)}>
            {showOld ? 'Hide' : 'Show'} {hiddenOld} chart{hiddenOld === 1 ? '' : 's'} left from other runs
          </Button>
        )}
      </Group>
      <Text size="sm" c="dimmed" mb="md">
        Click a chart to see it larger.
      </Text>
      <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
        {general.map((c) => (
          <ChartCard key={c.chart.name} folder={part.folder} chart={c.chart} />
        ))}
      </SimpleGrid>
      {features.length > 0 && (
        <Stack gap="sm" mt="xl">
          <Text fw={600}>Per feature</Text>
          <ScrollArea type="auto" offsetScrollbars>
            <SegmentedControl value={shownFeature ?? ''} onChange={setFeature} data={features} />
          </ScrollArea>
          <SimpleGrid cols={{ base: 1, md: 2 }} spacing="lg">
            {perFeature.map((c) => (
              <ChartCard key={c.chart.name} folder={part.folder} chart={c.chart} />
            ))}
          </SimpleGrid>
        </Stack>
      )}
    </Paper>
  );
}

/** Loads a chart with the page's key (an image tag can't send it) and frees it when done. */
function useChart(folder: string, chart: ResultChart): { url: string | null; failed: boolean } {
  const [state, setState] = useState<{ url: string | null; failed: boolean }>({ url: null, failed: false });
  useEffect(() => {
    let alive = true;
    let made: string | null = null;
    setState({ url: null, failed: false });
    api
      .chart(folder, chart.name)
      .then((url) => {
        if (alive) {
          made = url;
          setState({ url, failed: false });
        } else {
          URL.revokeObjectURL(url);
        }
      })
      .catch(() => alive && setState({ url: null, failed: true }));
    return () => {
      alive = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [folder, chart.name, chart.writtenAt]);
  return state;
}

function ChartCard({ folder, chart }: { folder: string; chart: ResultChart }) {
  const info = chartInfo(chart.name);
  const { url, failed } = useChart(folder, chart);
  const [large, setLarge] = useState(false);
  return (
    <Stack gap={6}>
      <Group justify="space-between" wrap="nowrap" align="flex-start">
        <Text fw={600} size="sm">
          {info.title}
        </Text>
        {chart.otherRun && <OtherRun at={chart.writtenAt} />}
      </Group>
      {info.caption && (
        <Text size="xs" c="dimmed">
          {info.caption}
        </Text>
      )}
      <UnstyledButton onClick={() => url && setLarge(true)} aria-label={`See larger: ${info.title}`} disabled={!url}>
        <Box bg="white" p={4} style={{ borderRadius: 'var(--mantine-radius-sm)', border: '1px solid var(--mantine-color-default-border)' }}>
          {url ? (
            <img src={url} alt={info.title} style={{ display: 'block', width: '100%', height: 'auto' }} />
          ) : (
            <Group h={160} justify="center">
              {failed ? <Text size="sm" c="red">This chart couldn't be loaded.</Text> : <Loader size="sm" />}
            </Group>
          )}
        </Box>
      </UnstyledButton>
      <Modal opened={large} onClose={() => setLarge(false)} title={info.title} size="90%">
        {url && <img src={url} alt={info.title} style={{ display: 'block', width: '100%', height: 'auto', background: 'white' }} />}
        {info.caption && (
          <Text size="sm" c="dimmed" mt="sm">
            {info.caption}
          </Text>
        )}
      </Modal>
    </Stack>
  );
}

/** The analyzer's own reports, as written, for whoever wants the numbers behind the summary. */
function Technical({ part, names }: { part: ResultPart; names: [string, string][] }) {
  const present = names.map(([name, label]) => ({ text: textOf(part, name), label })).filter((t) => t.text !== null);
  const [tab, setTab] = useState<string | null>(present[0]?.text?.name ?? null);
  if (present.length === 0) return null;
  return (
    <Paper withBorder p="md">
      <Title order={4} mb={4}>
        Technical details
      </Title>
      <Text size="sm" c="dimmed" mb="sm">
        The analyzer's own reports, as it wrote them: the numbers behind the summary above.
      </Text>
      <Tabs value={tab} onChange={setTab}>
        <Tabs.List>
          {present.map(({ text, label }) => (
            <Tabs.Tab key={text!.name} value={text!.name}>
              {label}
            </Tabs.Tab>
          ))}
        </Tabs.List>
        {present.map(({ text }) => (
          <Tabs.Panel key={text!.name} value={text!.name} pt="sm">
            {text!.otherRun && (
              <Box mb="xs">
                <OtherRun at={text!.writtenAt} />
              </Box>
            )}
            <ScrollArea.Autosize mah={520} type="auto">
              <Code block style={{ whiteSpace: 'pre', fontSize: 12 }}>
                {text!.text}
              </Code>
            </ScrollArea.Autosize>
          </Tabs.Panel>
        ))}
      </Tabs>
    </Paper>
  );
}
