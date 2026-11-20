import { IconCode, IconDatabase, IconEyeOff, IconQuestionMark } from '@tabler/icons-react';
import type { ComponentKind } from './types';

/** How each kind of component is shown and explained, everywhere it appears. */
export const KINDS: Record<
  ComponentKind,
  { label: string; color: string; Icon: typeof IconCode; what: string; effect: string; examples: string }
> = {
  service: {
    label: 'Service',
    color: 'blue',
    Icon: IconCode,
    what: 'Code your team wrote that handles requests: an API, a backend, a worker.',
    effect:
      'Gets the performance analysis: how busy it is, how fast it answers, how many copies it needs. So it needs request metrics.',
    examples: 'catalog, orders, user-service',
  },
  infrastructure: {
    label: 'Infrastructure',
    color: 'violet',
    Icon: IconDatabase,
    what: "Software your services rely on that your team didn't write: a gateway, database, cache, message broker or connection pooler.",
    effect: "Counts towards availability: while it's down, every feature that needs it is down too.",
    examples: 'postgres, redis, kong, rabbitmq',
  },
  notModelled: {
    label: 'Not modelled',
    color: 'gray',
    Icon: IconEyeOff,
    what: "Anything a user's request never passes through: frontends (the load test calls the API behind them), admin UIs, monitoring, load generators, one-off jobs such as migrations.",
    effect: 'Left out of the analysis. It still runs: nothing is stopped or changed.',
    examples: 'frontend, pgadmin, prometheus, db-migrate',
  },
  unsure: {
    label: 'Unsure',
    color: 'yellow',
    Icon: IconQuestionMark,
    what: "The compose files didn't say what it is, so the app didn't guess.",
    effect: 'Pick one of the three before going on.',
    examples: 'an image pulled from your own registry',
  },
};

/**
 * What the markers on a folded compose service mean: shown on hover in each
 * row, and in the legend.
 */
export const FOLDED: Record<'replica' | 'in front', { label: string; color: string; help: string }> = {
  replica: {
    label: 'replica',
    color: 'grape',
    help:
      'Another copy of this component, running as a compose service of its own (db and db-2). ' +
      'It is folded into this component as one more instance: while one copy is up, the component is up.',
  },
  'in front': {
    label: 'in front',
    color: 'cyan',
    help:
      'A connection pooler or load balancer whose only job is passing requests to this component. ' +
      'Every request to the component goes through it, so it counts as part of the component, not as ' +
      'a single point of failure of its own. The undo arrow takes it back out if you want it counted.',
  },
};

