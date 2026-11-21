import { MantineProvider } from '@mantine/core';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { KINDS } from '../kinds';
import { componentBlockers, type WizardState } from '../model';
import { draftedState } from '../test/fixtures';
import { ComponentsStep } from './ComponentsStep';

function Harness({ onState, initial = draftedState() }: { onState: (s: WizardState) => void; initial?: WizardState }) {
  const [state, setState] = useState(initial);
  onState(state);
  return (
    <MantineProvider>
      <ComponentsStep state={state} setState={(update) => setState(update)} />
    </MantineProvider>
  );
}

describe('ComponentsStep', () => {
  it('shows each component in its group, with the reasons for it', () => {
    render(<Harness onState={() => {}} />);
    expect(screen.getByRole('heading', { name: /Decide what these are/ })).toBeInTheDocument();
    expect(screen.getByText(/another copy of db \(same image, named db-2\)/)).toBeInTheDocument();
    expect(screen.getByText('Folded into this one:')).toBeInTheDocument();
    expect(screen.getByText('1 component still needs you to decide what it is.')).toBeInTheDocument();
  });

  it('lets the user settle an unsure component', async () => {
    let latest: WizardState | undefined;
    render(<Harness onState={(s) => (latest = s)} />);
    const unsure = document.querySelector<HTMLElement>('[data-kind="unsure"]')!;
    await userEvent.click(within(unsure).getByText('Service'));
    expect(latest!.components.find((c) => c.id === 'shop/billing')?.kind).toBe('service');
    expect(componentBlockers(latest!.components)).toEqual([]);
    expect(screen.queryByRole('heading', { name: /Decide what these are/ })).not.toBeInTheDocument();
  });

  it('separates a folded replica on request', async () => {
    let latest: WizardState | undefined;
    render(<Harness onState={(s) => (latest = s)} />);
    await userEvent.click(screen.getByRole('button', { name: 'separate shop/db-2' }));
    expect(latest!.components.map((c) => c.id)).toContain('shop/db-2');
  });

  it('asks whether numbered copies take over for each other before counting them', async () => {
    const base = draftedState();
    const unconfirmed = {
      ...base,
      components: base.components.map((c) =>
        c.id === 'shop/db' ? { ...c, folded: c.folded.map((f) => ({ ...f, confirmed: false })) } : c,
      ),
    };
    let latest: WizardState | undefined;
    const { unmount } = render(<Harness initial={unconfirmed} onState={(s) => (latest = s)} />);

    expect(screen.getByText('Can db-2 take over when db fails?')).toBeInTheDocument();
    expect(componentBlockers(latest!.components)).toContain(
      'Answer the question under db: can its numbered copies take over for each other?',
    );

    await userEvent.click(screen.getByRole('button', { name: 'Yes, count 2 instances' }));
    expect(latest!.components.find((c) => c.id === 'shop/db')?.instances).toBe(2);
    expect(screen.queryByText('Can db-2 take over when db fails?')).not.toBeInTheDocument();
    expect(componentBlockers(latest!.components).some((b) => b.includes('numbered copies'))).toBe(false);
    unmount();

    render(<Harness initial={unconfirmed} onState={(s) => (latest = s)} />);
    await userEvent.click(screen.getByRole('button', { name: "No, they're separate" }));
    const db = latest!.components.find((c) => c.id === 'shop/db')!;
    expect(db.instances).toBe(1);
    expect(db.folded).toEqual([]);
    expect(latest!.components.map((c) => c.id)).toContain('shop/db-2');
  });

  it('forgets an earlier save once anything changes', async () => {
    let latest: WizardState | undefined;
    const saved = { ...draftedState(), savedPath: '/repos/shop/perf/shop.toml' };
    render(<Harness initial={saved} onState={(s) => (latest = s)} />);
    const unsure = document.querySelector<HTMLElement>('[data-kind="unsure"]')!;
    await userEvent.click(within(unsure).getByText('Infrastructure'));
    expect(latest!.savedPath).toBeNull();
  });

  it('explains the groups, and can be folded away', async () => {
    render(<Harness onState={() => {}} />);
    expect(screen.getByText(/Rule of thumb: follow a user's request/)).toBeVisible();
    expect(screen.getByText(KINDS.infrastructure.effect)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Hide the legend' }));
    expect(screen.getByRole('button', { name: 'Show the legend' })).toBeInTheDocument();
  });
});
