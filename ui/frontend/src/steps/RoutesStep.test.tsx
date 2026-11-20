import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { WizardState } from '../model';
import { routedState } from '../test/fixtures';
import type { AppStatus, TryResponse } from '../types';
import { RouteErrorsConfirm, RoutesStep } from './RoutesStep';

function Harness({ onState }: { onState: (s: WizardState) => void }) {
  const [state, setState] = useState(routedState);
  onState(state);
  return (
    <MantineProvider>
      <QueryClientProvider client={new QueryClient()}>
        <RoutesStep state={state} setState={(update) => setState(update)} />
      </QueryClientProvider>
    </MantineProvider>
  );
}

afterEach(() => vi.unstubAllGlobals());

describe('the routes step', () => {
  it("says sorry when a service's language can't be read, and what to do instead", () => {
    function Unreadable() {
      const [state, setState] = useState(() => {
        const s = routedState();
        s.read = {
          ...s.read!,
          routes: {
            ...s.read!.routes!,
            code: [
              { service: 'shop/api', language: 'Haskell', supported: false, routes: 0 },
              { service: 'shop/db', language: null, supported: false, routes: 0 },
            ],
            languages: ['C#', 'Go', 'Python'],
          },
        };
        return s;
      });
      return (
        <MantineProvider>
          <QueryClientProvider client={new QueryClient()}>
            <RoutesStep state={state} setState={(update) => setState(update)} />
          </QueryClientProvider>
        </MantineProvider>
      );
    }
    render(<Unreadable />);
    const alert = document.querySelector<HTMLElement>('[data-unread-code]')!;
    expect(within(alert).getByText(/which this app can't read yet/)).toHaveTextContent('Sorry: api is written in Haskell');
    expect(within(alert).getByText(/Use my own k6 script/)).toBeInTheDocument();
    expect(within(alert).getByText('Routes can be read for: C#, Go, Python.')).toBeInTheDocument();
    // infrastructure (the database) isn't expected to have routes
    expect(within(alert).queryByText(/db/)).not.toBeInTheDocument();
  });

  it('asks to start the app before trying, and offers to do it', async () => {
    const down: AppStatus = {
      projects: [{ name: 'shop', state: 'missing', running: 0, notRunning: 0, status: 'not started' }],
      running: false,
      baseUrl: 'http://localhost:8080',
      baseStatus: null,
      commands: ['cd /repos/shop\ndocker compose -f compose.yaml up -d'],
    };
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push(`${init?.method ?? 'GET'} ${url}`);
        const body = url.startsWith('/api/app/status') ? down : { state: 'starting', lines: [], next: 0 };
        return { ok: true, status: 200, text: async () => JSON.stringify(body) };
      }),
    );
    render(<Harness onState={() => {}} />);
    expect(await screen.findByText('Start your app to try the routes')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Try the 1 picked route/ })).toBeDisabled();
    await userEvent.click(screen.getByRole('button', { name: "I'll start it myself" }));
    expect(screen.getByText(/docker compose -f compose.yaml up -d/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Start it for me' }));
    expect(await screen.findByText('Starting your app…')).toBeInTheDocument();
    expect(calls).toContain('POST /api/app/start');
  });

  it('groups the routes by the service that handles them', () => {
    render(<Harness onState={() => {}} />);
    const api = document.querySelector<HTMLElement>('[data-component="api"]')!;
    expect(within(api).getByText('1 of 4 picked')).toBeInTheDocument();
    expect(within(api).getByText(/A health check, not a user request/)).toBeInTheDocument();
    expect(screen.getByText(/route of a component left out of the analysis isn't shown/)).toBeInTheDocument();
  });

  it('asks for a body when a route that sends one is picked', async () => {
    let latest: WizardState | undefined;
    render(<Harness onState={(s) => (latest = s)} />);
    await userEvent.click(screen.getByLabelText('test POST /api/orders'));
    const body = screen.getByLabelText('body of shop/api POST /orders');
    await userEvent.clear(body);
    await userEvent.type(body, '{{"book_id": 7}');
    expect(latest!.routes.find((r) => r.path === '/api/orders')).toMatchObject({ selected: true, body: '{"book_id": 7}' });
  });

  it('shows how each route answered when tried', async () => {
    const tried: TryResponse = {
      login: null,
      tokenFound: null,
      requests: [
        { name: 'GET /api/books', method: 'GET', url: 'http://localhost:8080/api/books', status: 200, milliseconds: 12.4, snippet: '[]', error: null, ok: true },
      ],
    };
    const up: AppStatus = {
      projects: [{ name: 'shop', state: 'running', running: 4, notRunning: 0, status: 'running(4)' }],
      running: true,
      baseUrl: 'http://localhost:8080',
      baseStatus: 200,
      commands: [],
    };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => ({
        ok: true,
        status: 200,
        text: async () => JSON.stringify(url.startsWith('/api/app/status') ? up : tried),
      })),
    );
    render(<Harness onState={() => {}} />);
    await screen.findByText('Your app is running', { exact: false });
    await userEvent.click(screen.getByRole('button', { name: /Try the 1 picked route/ }));
    expect(await screen.findByText('200 · 12 ms')).toBeInTheDocument();
    expect(screen.getByText('1 of 1 answered 2xx/3xx. The results are on each row.')).toBeInTheDocument();
  });
});

describe('leaving the routes step with routes that answered errors', () => {
  it('asks first, says why, and how to fix it', async () => {
    const state = routedState();
    const [books, book] = state.routes;
    const result = (status: number) => ({
      name: 'x', method: 'GET', url: 'http://localhost:8080/x', status, milliseconds: 4, snippet: null, error: null, ok: false,
    });
    const stay = vi.fn();
    const go = vi.fn();
    render(
      <MantineProvider>
        <RouteErrorsConfirm
          errors={[
            { route: books, result: result(401) },
            { route: book, result: result(500) },
          ]}
          opened
          onStay={stay}
          onContinue={go}
        />
      </MantineProvider>,
    );

    expect(await screen.findByText('Some routes answered with errors')).toBeInTheDocument();
    expect(screen.getByText(/these 2 routes didn't work/)).toBeInTheDocument();
    expect(screen.getByText('needs a login: no valid token was sent')).toBeInTheDocument();
    expect(screen.getByText('the app failed while answering')).toBeInTheDocument();
    expect(screen.getByText(/set up the login in Authorization/)).toBeInTheDocument();
    expect(screen.getByText(/unless you expect these errors/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Go back and fix them' }));
    expect(stay).toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Continue anyway' }));
    expect(go).toHaveBeenCalled();
  });
});
