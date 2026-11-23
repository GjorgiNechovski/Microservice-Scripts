import { describe, expect, it } from 'vitest';
import {
  ago,
  chartInfo,
  freshness,
  parseExplained,
  parseValidity,
  sentenceCase,
  toBlocks,
  verdictTone,
} from './results';
import type { ResultSummary } from './types';

// as the analyzer writes them (FINKI, 2026-11-20)
const EXPLAINED = `
PLAIN-LANGUAGE SUMMARY — finki-blogger-2
======================================================================

This is the same information as the technical report, explained without
jargon. It answers two everyday questions about the system:
  1) Is it FAST enough to handle its visitors?
  2) Is it RELIABLE — how often is it working, and what could take it down?

THE HEADLINE
----------------------------------------------------------------------
When someone does "get blogs", the system works 99.9994% of the time —
  that is about 3 minutes a year of downtime.
  Most likely to cause an outage: zookeeper.

IS IT FAST ENOUGH?
----------------------------------------------------------------------
Picture each service as a checkout counter. 'Busy-ness' is how full it
is: 0% is idle; near 100% a queue forms and customers wait.

  - blog-service: about 4% busy — very relaxed, with lots of spare capacity
  - comment-service: about 7% busy — very relaxed, with lots of spare capacity

  In short, at this load (the last two measured by the load test):
    Overloaded?       no, every part kept up with the traffic
    Failed requests?  50.35%: more than the 0.10% that counts as fine
    Speed?            95 of every 100 responses came back within 18 ms
`;

const VALIDITY = `Run validity -- single run: INVALID
--------------------------------------------------------------------------
  PASS  load ran its full duration   60 s of 60 s planned
  FAIL  requests succeeded           50.35% of requests failed -- errors are counted as arrivals
  SKIP  k6 rate matches measured λ   k6 rate or measured λ missing

  INVALID: the test or its measurement broke; do not report this run's numbers
  (CLAUDE.md, 'Before you believe any number').
`;

describe('the plain-language files', () => {
  it('are split into their sections, with readable titles', () => {
    const doc = parseExplained(EXPLAINED);
    expect(doc.title).toBe('Plain-language summary — finki-blogger-2');
    expect(parseExplained('PLAIN-LANGUAGE SCALING SUMMARY -- FINKI blog\n=========================\n').title).toBe(
      'Plain-language scaling summary — FINKI blog',
    );
    expect(doc.sections.map((s) => s.title)).toEqual(['The headline', 'Is it fast enough?']);
    expect(doc.intro[0]).toEqual({
      kind: 'p',
      text: 'This is the same information as the technical report, explained without jargon. It answers two everyday questions about the system:',
      depth: 0,
    });
    expect(doc.intro[1]).toMatchObject({ kind: 'item', marker: '1)', text: 'Is it FAST enough to handle its visitors?' });
  });

  it('join lines wrapped mid-sentence, and keep deliberate line ends, lists and columns', () => {
    const [headline, fast] = parseExplained(EXPLAINED).sections;
    expect(headline.blocks).toEqual([
      {
        kind: 'p',
        text: 'When someone does "get blogs", the system works 99.9994% of the time — that is about 3 minutes a year of downtime.',
        depth: 0,
      },
      { kind: 'p', text: 'Most likely to cause an outage: zookeeper.', depth: 1 },
    ]);
    expect(fast.blocks.map((b) => b.kind)).toEqual(['p', 'item', 'item', 'p', 'table']);
    expect(fast.blocks[1]).toMatchObject({ marker: '•', text: expect.stringMatching(/^blog-service: about 4% busy/) });
    expect(fast.blocks[4]).toEqual({
      kind: 'table',
      rows: [
        ['Overloaded?', 'no, every part kept up with the traffic'],
        ['Failed requests?', '50.35%: more than the 0.10% that counts as fine'],
        ['Speed?', '95 of every 100 responses came back within 18 ms'],
      ],
      depth: 2, // under "In short, …" (1)
    });
  });

  it("don't take one line with a double space for a table", () => {
    expect(toBlocks(['It is fine.  Nothing to do.'])).toEqual([{ kind: 'p', text: 'It is fine.  Nothing to do.', depth: 0 }]);
  });

  it('keep names in titles as they are', () => {
    expect(sentenceCase('DID IT GET BETTER? — 50 users (vs your last run)')).toBe(
      'Did it get better? — 50 users (vs your last run)',
    );
  });
});

describe('the trust check', () => {
  it('is read check by check', () => {
    const v = parseValidity(VALIDITY)!;
    expect(v.label).toBe('single run');
    expect(v.verdict).toBe('INVALID');
    expect(v.checks).toEqual([
      { status: 'PASS', name: 'load ran its full duration', detail: '60 s of 60 s planned' },
      { status: 'FAIL', name: 'requests succeeded', detail: '50.35% of requests failed -- errors are counted as arrivals' },
      { status: 'SKIP', name: 'k6 rate matches measured λ', detail: 'k6 rate or measured λ missing' },
    ]);
    expect(verdictTone(v.verdict).color).toBe('red');
    expect(verdictTone('VALID, with warnings').color).toBe('yellow');
    expect(verdictTone('OVERLOADED').color).toBe('orange');
    expect(verdictTone('VALID').color).toBe('teal');
    expect(parseValidity('something else')).toBeNull();
  });

  it("never shows a verdict it doesn't know as good", () => {
    // a newer analyzer's verdict: neutral, not "these numbers can be used"
    expect(verdictTone('INCONCLUSIVE').color).toBe('gray');
    expect(verdictTone('').color).toBe('gray');
  });
});

describe('charts', () => {
  it('are explained by their file name, per feature where they are about one', () => {
    expect(chartInfo('layer4_invest_get_blogs.png')).toMatchObject({
      title: 'What would help “get blogs” most',
      feature: 'get blogs',
    });
    expect(chartInfo('measured_mttr.png')).toMatchObject({ title: 'How long each part took to come back', feature: null });
    expect(chartInfo('something_new.png')).toMatchObject({ title: 'something new', caption: '' });
  });
});

describe('how current results are', () => {
  const result: ResultSummary = {
    folder: '/apps/shop/perf/analysis',
    kind: 'single',
    system: 'shop',
    writtenAt: '2026-11-20T20:00:00Z',
    levels: [],
    unfinishedAfter: null,
    current: false,
    specSavedAt: null,
  };
  const now = Date.parse('2026-11-20T23:00:00Z');

  it('says results not made by this session are older, and why', () => {
    const f = freshness({ ...result, specSavedAt: '2026-11-20T22:00:00Z', unfinishedAfter: '2026-11-20T22:30:00Z' }, now);
    expect(f.current).toBe(false);
    expect(f.title).toMatch(/^Older results: from .+ \(3 hours ago\)$/);
    expect(f.lines).toHaveLength(3);
    expect(f.lines[1]).toMatch(/saved again .* after these results/);
    expect(f.lines[2]).toMatch(/didn't finish/);
  });

  it("calls the session's own run current", () => {
    expect(freshness({ ...result, current: true }, now)).toMatchObject({ current: true, title: 'From the run you just finished' });
  });

  it('counts time in words', () => {
    expect(ago('2026-11-20T22:59:50Z', now)).toBe('just now');
    expect(ago('2026-11-20T22:59:00Z', now)).toBe('1 minute ago');
    expect(ago('2026-11-18T23:00:00Z', now)).toBe('2 days ago');
    expect(ago('2026-07-14T23:00:00Z', now)).toBe('4 months ago');
  });
});
