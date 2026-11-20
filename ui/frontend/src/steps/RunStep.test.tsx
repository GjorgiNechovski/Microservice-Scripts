import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { routedState } from '../test/fixtures';
import type { AppStatus, MonitoringStatus, PreflightResult, RunStatus } from '../types';
import { RunStep } from './RunStep';

const idle: RunStatus = {
  state: 'idle',
  specPath: null,
  faultInjection: false,
  outDir: null,
  startedAt: null,
  endedAt: null,
  exitCode: null,
  lines: [],
  next: 0,
  results: [],
};

const up: AppStatus = {
  projects: [{ name: 'shop', state: 'running', running: 4, notRunning: 0, status: 'running(4)' }],
  running: true,
  baseUrl: 'http://localhost:8080',
  baseStatus: 200,
  commands: ['cd /repos/shop\ndocker compose -f compose.yaml up -d'],
};

const monitoringUp: MonitoringStatus = {
  envExists: true,
  settings: { appNetwork: 'shop_default', traces: false, honeycombKey: null, scrapeDir: null, namespace: 'shop' },
  hasKey: false,
  prometheusUp: true,
  joined: ['shop_default', 'microservice-observability_default'],
  targets: [{ job: 'cadvisor', url: 'http://cadvisor:8080/metrics', health: 'up', error: null }],
  networks: ['shop_default'],
  envPath: '/ms/observability/.env',
  command: 'cd /ms/observability\ndocker compose -f docker-compose.observability.yaml up -d',
};

const plan = 'blog-service  OUTAGE on kill';

function answer(preflight: PreflightResult, monitoring: MonitoringStatus = monitoringUp) {
  const calls: { call: string; body: unknown }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ call: `${init?.method ?? 'GET'} ${url}`, body: init?.body ? JSON.parse(String(init.body)) : null });
      const body = url.startsWith('/api/run/check')
        ? preflight
        : url.startsWith('/api/app/status')
          ? up
          : url.startsWith('/api/monitoring/preview')
            ? { env: 'APP_NETWORK=shop_default\nOTEL_COLLECTOR_CONFIG=config.local.yaml\n', problems: [] }
            : url.startsWith('/api/monitoring/start')
              ? { state: 'starting', lines: [], next: 0 }
              : url.startsWith('/api/monitoring')
                ? monitoring
                : idle;
      return { ok: true, status: 200, text: async () => JSON.stringify(body) };
    }),
  );
  return calls;
}

function renderStep() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const state = { ...routedState(), savedPath: '/repos/shop/perf/shop.toml' };
  return render(
    <MantineProvider>
      <QueryClientProvider client={client}>
        <RunStep state={state} setState={() => {}} onRunState={() => {}} />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('the run step', () => {
  it('runs everything by default, asking once with what would be stopped', async () => {
    const calls = answer({ checks: [], faultPlan: plan, canStart: true });
    renderStep();
    expect(screen.getByLabelText(/The full run/)).toBeChecked();
    await userEvent.click(await screen.findByRole('button', { name: 'Start' }));
    expect(await screen.findByText('Start the full run?')).toBeInTheDocument();
    expect(screen.getAllByText(/OUTAGE on kill/).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole('button', { name: 'Start the full run' }));
    const start = calls.find((c) => c.call === 'POST /api/run/start');
    expect(start?.body).toMatchObject({ faultInjection: true });
  });

  it('starts a load test alone without asking', async () => {
    const calls = answer({ checks: [], faultPlan: plan, canStart: true });
    renderStep();
    await userEvent.click(screen.getByLabelText('The load test only'));
    await userEvent.click(await screen.findByRole('button', { name: 'Start' }));
    expect(screen.queryByText('Start the full run?')).not.toBeInTheDocument();
    expect(calls.find((c) => c.call === 'POST /api/run/start')?.body).toMatchObject({ faultInjection: false });
  });

  it('lists what was checked, and won\'t start while something is missing', async () => {
    answer({
      checks: [{ key: 'app:shop', label: 'Your app (shop)', status: 'fail', detail: "It isn't running." }],
      faultPlan: null,
      canStart: false,
    });
    renderStep();
    expect(await screen.findByText(/It isn't running/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled();
  });

  it("says a freshly started cAdvisor is starting, not missing", async () => {
    answer(
      {
        checks: [
          { key: 'cadvisor', label: 'Container metrics (cAdvisor)', status: 'wait', detail: 'Starting (up for 12 s)' },
        ],
        faultPlan: null,
        canStart: true,
      },
      { ...monitoringUp, targets: [{ job: 'cadvisor', url: 'http://cadvisor:8080/metrics', health: 'unknown', error: null }] },
    );
    renderStep();
    expect(await screen.findByText(/Starting \(up for 12 s\)/)).toBeInTheDocument();
    expect(document.querySelector('[data-check="cadvisor"]')?.getAttribute('data-status')).toBe('wait');
    expect(await screen.findByText('cadvisor starting')).toBeInTheDocument();
    expect(screen.queryByText(/can't be read/)).not.toBeInTheDocument();
  });

  it('fills in the monitoring settings and starts the stack', async () => {
    const calls = answer(
      { checks: [], faultPlan: null, canStart: false },
      { ...monitoringUp, prometheusUp: false, joined: [], targets: [], envExists: false, settings: null },
    );
    renderStep();
    expect(await screen.findByText('Start the monitoring stack')).toBeInTheDocument();
    expect(await screen.findByText(/OTEL_COLLECTOR_CONFIG=config.local.yaml/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Save and start' }));
    expect(await screen.findByText('Starting the monitoring stack…')).toBeInTheDocument();
    expect(calls.find((c) => c.call === 'POST /api/monitoring/start')?.body).toMatchObject({
      appNetwork: 'shop_default',
      traces: false,
    });
  });
});
