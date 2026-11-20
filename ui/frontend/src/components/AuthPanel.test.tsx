import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { initialAuth, type AuthSettings } from '../routes';
import type { LoginTry } from '../types';
import { AuthPanel } from './AuthPanel';

function Harness({ onAuth, onSecret }: { onAuth: (a: AuthSettings) => void; onSecret?: (s: string) => void }) {
  const [auth, setAuth] = useState<AuthSettings>({ ...initialAuth, kind: 'login', path: '/auth/login' });
  const [secret, setSecret] = useState('');
  onAuth(auth);
  onSecret?.(secret);
  return (
    <MantineProvider>
      <QueryClientProvider client={new QueryClient()}>
        <AuthPanel
          auth={auth}
          secret={secret}
          baseUrl="http://localhost:8000"
          loginGuess={undefined}
          onChange={(patch) => setAuth((a) => ({ ...a, ...patch }))}
          onSecret={setSecret}
        />
      </QueryClientProvider>
    </MantineProvider>
  );
}

function answer(tried: LoginTry) {
  const sent: unknown[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init?: RequestInit) => {
      sent.push(JSON.parse(String(init?.body)));
      return { ok: true, status: 200, text: async () => JSON.stringify(tried) };
    }),
  );
  return sent;
}

const ok = (body: string, token: LoginTry['token']): LoginTry => ({
  result: { name: 'login', method: 'POST', url: 'http://localhost:8000/auth/login', status: 200, milliseconds: 21, snippet: body, error: null, ok: true },
  body,
  contentType: 'application/json',
  token,
  setsCookie: false,
});

afterEach(() => vi.unstubAllGlobals());

describe('the Authorization panel', () => {
  it('builds the login body from Key/Value rows, the locked one kept apart', async () => {
    let secret = '';
    const sent = answer(ok('{}', null));
    render(<Harness onAuth={() => {}} onSecret={(s) => (secret = s)} />);
    await userEvent.type(screen.getByLabelText('value of username'), 'test@example.com');
    await userEvent.type(screen.getByLabelText('value of password'), 's3cret');
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findByText('Response');
    expect(secret).toBe('s3cret');
    expect(sent[0]).toMatchObject({
      secret: 's3cret',
      auth: { login: { body: '{\n  "username": "test@example.com",\n  "password": "{{password}}"\n}' } },
    });
  });

  it('marks the token it found, and takes another value when clicked', async () => {
    let auth: AuthSettings | undefined;
    answer(ok('{"user": {"id": 7, "name": "ana"}, "access_token": "abc.def.ghi"}', { value: 'abc.def.ghi', field: 'access_token' }));
    render(<Harness onAuth={(a) => (auth = a)} />);
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Logged in: the token is marked above')).toBeInTheDocument();
    expect(screen.getByLabelText('token at access_token')).toBeInTheDocument();
    expect(auth).toMatchObject({ tokenFrom: 'field', tokenField: 'access_token' });
    await userEvent.click(screen.getByLabelText('use user.name as the token'));
    expect(auth).toMatchObject({ tokenFrom: 'field', tokenField: 'user.name' });
  });

  it('says plainly when the response body itself is the token', async () => {
    let auth: AuthSettings | undefined;
    answer(ok('"eyJhbGciOi.payload.sig"', { value: 'eyJhbGciOi.payload.sig', field: null }));
    render(<Harness onAuth={(a) => (auth = a)} />);
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByLabelText('token at the response body')).toBeInTheDocument();
    expect(auth).toMatchObject({ tokenFrom: 'body' });
    expect(screen.getByText('Authorization: Bearer <token>')).toBeInTheDocument();
  });

  it("doesn't carry a password over to another kind of secret", async () => {
    let secret = '';
    render(<Harness onAuth={() => {}} onSecret={(s) => (secret = s)} />);
    await userEvent.type(screen.getByLabelText('value of password'), 's3cret');
    await userEvent.click(screen.getByText('API key', { exact: true }));
    expect(secret).toBe('');
  });

  it('explains a refused login', async () => {
    answer({ ...ok('{"detail": "wrong password"}', null), result: { ...ok('', null).result, status: 401, ok: false } });
    render(<Harness onAuth={() => {}} />);
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('The login was refused (401)')).toBeInTheDocument();
  });
});
