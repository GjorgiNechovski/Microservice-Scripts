import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initialState } from '../model';
import type { ResultList, ResultPart, ResultSummary, ResultView } from '../types';
import { ResultsPage } from './ResultsPage';

const at = '2026-11-20T22:38:00Z';

const summary: ResultSummary = {
  folder: '/apps/shop/perf/analysis',
  kind: 'single',
  system: 'shop',
  writtenAt: at,
  levels: [],
  unfinishedAfter: null,
  current: false,
  specSavedAt: null,
};

const text = (name: string, body: string, otherRun = false) => ({ name, text: body, writtenAt: at, otherRun });

const part: ResultPart = {
  folder: '/apps/shop/perf/analysis',
  users: null,
  texts: [
    text(
      'report-explained',
      'PLAIN-LANGUAGE SUMMARY — shop\n=====================\n\nTHE HEADLINE\n--------------------\nWhen someone does "get books", the system works 99.99% of the time.\n',
    ),
    text('report', 'ANALYSIS REPORT -- shop\nLayer 1 -- Performance\n'),
    text('validity', 'Run validity -- single run: INVALID\n----------\n  FAIL  requests succeeded           50% of requests failed\n  PASS  load ran its full duration   60 s of 60 s planned\n'),
    text('validity-explained', 'CAN THESE NUMBERS BE TRUSTED? (single run)\n----------\n  No. Something went wrong with the test itself.\n'),
    text('k6-output', '█ TOTAL RESULTS\n  http_reqs: 10\n'),
  ],
  charts: [
    { name: 'measured_mttr.png', writtenAt: at, otherRun: false },
    { name: 'layer4_invest_get_books.png', writtenAt: at, otherRun: false },
    { name: 'layer3_importance_old_feature.png', writtenAt: '2026-09-01T10:00:00Z', otherRun: true },
  ],
};

function answer(list: ResultList, view: ResultView | null) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? 'GET'} ${url}`);
      if (url.startsWith('/api/results/chart')) return { ok: true, status: 200, blob: async () => new Blob(['png']) };
      const body = url.startsWith('/api/results/find') ? list : view;
      return { ok: true, status: 200, text: async () => JSON.stringify(body) };
    }),
  );
  return calls;
}

function renderStep(onGoToRun = () => {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider>
      <QueryClientProvider client={client}>
        <ResultsPage state={initialState} run="idle" onGoToRun={onGoToRun} />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => 'blob:chart');
  URL.revokeObjectURL = vi.fn();
  localStorage.clear();
});

afterEach(() => vi.unstubAllGlobals());

describe('the results page', () => {
  it("shows an earlier run's results, marked older, trust check first", async () => {
    const calls = answer({ results: [summary], searched: ['/apps/shop'] }, { summary, overview: null, parts: [part] });
    renderStep();

    expect(await screen.findByText(/^Older results: from /)).toBeInTheDocument();
    expect(screen.getByText(/No run in this session has replaced them/)).toBeInTheDocument();
    expect(await screen.findByText('INVALID')).toBeInTheDocument();
    expect(screen.getByText('No. Something went wrong with the test itself.')).toBeInTheDocument();
    expect(screen.getByText('requests succeeded')).toBeInTheDocument();
    expect(screen.getByText('The headline')).toBeInTheDocument();
    expect(screen.getByText('When someone does "get books", the system works 99.99% of the time.')).toBeInTheDocument();
    expect(screen.getByText('How long each part took to come back')).toBeInTheDocument();
    expect(screen.getByText('What would help “get books” most')).toBeInTheDocument();
    // a chart left from an older run is hidden until asked for
    expect(screen.queryByText(/“old feature”/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Show 1 chart left from other runs' }));
    expect(screen.getByRole('radio', { name: 'old feature' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Load test (k6)' })).toBeInTheDocument();

    expect(calls[0]).toBe('POST /api/results/find');
    expect(calls).toContain(`GET /api/results?folder=${encodeURIComponent(summary.folder)}`);
  });

  it('calls the run that just finished current', async () => {
    const current = { ...summary, current: true };
    answer({ results: [current], searched: [] }, { summary: current, overview: null, parts: [part] });
    renderStep();
    expect(await screen.findByText('From the run you just finished')).toBeInTheDocument();
  });

  it('shows a sweep across its levels, and each level', async () => {
    const sweep: ResultSummary = { ...summary, kind: 'sweep', levels: [10, 50] };
    const overview: ResultPart = {
      folder: summary.folder,
      users: null,
      texts: [text('scaling-report-explained', 'HOW MUCH TRAFFIC IT CAN TAKE\n--------------------\nAbout 40 users.\n')],
      charts: [{ name: 'scaling_throughput.png', writtenAt: at, otherRun: false }],
    };
    const level = (users: number): ResultPart => ({ ...part, folder: `${summary.folder}/load-${users}`, users });
    answer({ results: [sweep], searched: [] }, { summary: sweep, overview, parts: [level(10), level(50)] });
    renderStep();

    expect(await screen.findByText('How much traffic it can take')).toBeInTheDocument();
    expect(screen.getByText('Requests handled as users grow')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: '50 users' }));
    expect(await screen.findByText('Can these numbers be trusted?')).toBeInTheDocument();
  });

  it('says where it looked when there are none, and leads to the run', async () => {
    answer({ results: [], searched: ['/apps/shop', '/ms'] }, null);
    const goToRun = vi.fn();
    renderStep(goToRun);

    expect(await screen.findByText('No results yet')).toBeInTheDocument();
    expect(screen.getByText(/Looked in: \/apps\/shop, \/ms/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Go to Run' }));
    expect(goToRun).toHaveBeenCalled();
  });
});
