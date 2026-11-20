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
