import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import type { PrerequisiteCheck } from './types';

function answerChecks(checks: PrerequisiteCheck[]) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: true, status: 200, text: async () => JSON.stringify(checks) })),
  );
}

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider>
      <QueryClientProvider client={client}>
        <App hasToken />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

const docker = (ok: boolean): PrerequisiteCheck => ({
  id: 'docker',
  label: 'Docker',
  required: true,
  ok,
  detail: ok ? 'Docker Engine 29' : "the engine isn't answering",
  fix: ok ? null : 'Start Docker.',
});

afterEach(() => vi.unstubAllGlobals());

describe('the bar at the bottom', () => {
  it('says when the user can go on, and opens Next', async () => {
    answerChecks([docker(true)]);
    renderApp();
    expect(await screen.findByText('Everything needed is here. Go on to choose your project.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Next: Project/ })).toBeEnabled();
  });

  it('says what is missing, and keeps Next shut', async () => {
    answerChecks([docker(false)]);
    renderApp();
    expect(await screen.findByText(/Fix the items marked in red/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Next: Project/ })).toBeDisabled();
  });
});

describe('the results page', () => {
  it('opens from the header at any time, before any setup, and goes back', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const body = url.startsWith('/api/results/find') ? { results: [], searched: ['/ms'] } : [docker(true)];
        return { ok: true, status: 200, text: async () => JSON.stringify(body) };
      }),
    );
    renderApp();
    await screen.findByText('Everything needed is here. Go on to choose your project.');

    await userEvent.click(screen.getByRole('radio', { name: 'Results' }));
    expect(await screen.findByText('No results yet')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Next: Project/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Back to the setup' }));
    expect(await screen.findByRole('button', { name: /Next: Project/ })).toBeInTheDocument();
  });
});
