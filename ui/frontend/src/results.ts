import type { ResultPart, ResultSummary } from './types';

/**
 * The analyzer's plain-language files are text wrapped at ~70 columns, with
 * UPPERCASE headings underlined by dashes. These turn them back into
 * paragraphs, list items and small tables the page can lay out.
 */

/** A paragraph, a list item (its marker: •, ✓, ✗, !, 1)), or a table of lined-up columns. */
export type Block =
  | { kind: 'p'; text: string; depth: number }
  | { kind: 'item'; marker: string; text: string; depth: number }
  | { kind: 'table'; rows: string[][]; depth: number };

export type Section = { title: string; blocks: Block[] };

/** title: the file's own heading, when it starts with one. */
export type Explained = { title: string | null; intro: Block[]; sections: Section[] };

const RULE = /^\s*(-{10,}|={10,})\s*$/;
const BULLET = /^([-•*]|[✓✗!]|\d+[).])\s+(.*)$/;
/** Where the second column starts, after a gap of two or more spaces. */
const GAP = /\S( {2,})(?=\S)/;
/** A line that ends a sentence (or introduces a list) isn't continued on the next. */
const ENDS = /[.:!?)]$/;

/** "IS IT FAST ENOUGH?" -> "Is it fast enough?"; names in lower or mixed case are kept. */
export function sentenceCase(title: string): string {
  const lowered = title.replace(/\b[A-Z][A-Z'-]+\b/g, (w) => w.toLowerCase());
  return lowered.charAt(0).toUpperCase() + lowered.slice(1);
}

/** "PLAIN-LANGUAGE SUMMARY -- FINKI blog": the heading in sentence case, the system's name as written. */
function docTitle(line: string): string {
  const m = /^(.*?)\s+(?:--|—)\s+(.+)$/.exec(line);
  return m ? `${sentenceCase(m[1])} — ${m[2]}` : sentenceCase(line);
}

export function parseExplained(text: string): Explained {
  const lines = text.replace(/\r/g, '').split('\n');
  const headings = new Set<number>();
  lines.forEach((line, i) => {
    if (i > 0 && RULE.test(line) && lines[i - 1].trim() && !RULE.test(lines[i - 1])) headings.add(i - 1);
  });

  const firstText = lines.findIndex((l) => l.trim() !== '');
  let title: string | null = null;
  const sections: Section[] = [];
  let current: string[] = [];
  let currentTitle: string | null = null;
  let intro: string[] = [];
  const close = () => {
    if (currentTitle === null) intro = current;
    else sections.push({ title: sentenceCase(currentTitle), blocks: toBlocks(current) });
  };
  for (let i = 0; i < lines.length; i++) {
    if (headings.has(i)) {
      if (i === firstText) {
        title = docTitle(lines[i].trim());
      } else {
        close();
        currentTitle = lines[i].trim();
        current = [];
      }
      i++; // the rule under it
      continue;
    }
    current.push(lines[i]);
  }
  close();
  return { title, intro: toBlocks(intro), sections };
}

/** Joins lines wrapped mid-sentence; keeps lists, deliberate line ends and lined-up columns. */
export function toBlocks(lines: string[]): Block[] {
  const clean = lines.map((l) => l.replace(/\s+$/, ''));
  const indents = clean.filter((l) => l.trim()).map((l) => l.length - l.trimStart().length);
  const base = indents.length ? Math.min(...indents) : 0;
  const indentOf = (l: string) => l.length - l.trimStart().length;
  const depthOf = (indent: number) => Math.max(0, Math.round((indent - base) / 2));

  // rows of a table: two or more lines in a row at one indent, their second column starting at one place
  const columnAt = clean.map((l) => {
    if (!l.trim() || BULLET.test(l.trim())) return -1;
    const m = GAP.exec(l);
    return m ? m.index + 1 + m[1].length : -1;
  });
  const isRow = clean.map(
    (l, i) =>
      columnAt[i] >= 0 &&
      [i - 1, i + 1].some((j) => j >= 0 && j < clean.length && columnAt[j] === columnAt[i] && indentOf(clean[j]) === indentOf(l)),
  );

  const blocks: Block[] = [];
  let open: { block: Block; indent: number; last: string } | null = null;
  clean.forEach((line, i) => {
    if (!line.trim()) {
      open = null;
      return;
    }
    const indent = indentOf(line);
    const body = line.trim();
    if (isRow[i]) {
      const row = body.split(/ {2,}/);
      const previous = blocks[blocks.length - 1];
      if (open === null && previous?.kind === 'table' && isRow[i - 1]) previous.rows.push(row);
      else blocks.push({ kind: 'table', rows: [row], depth: depthOf(indent) });
      open = null;
      return;
    }
    const bullet = BULLET.exec(body);
    if (bullet) {
      const marker = /^[-*]$/.test(bullet[1]) ? '•' : bullet[1];
      const block: Block = { kind: 'item', marker, text: bullet[2], depth: depthOf(indent) };
      blocks.push(block);
      open = { block, indent, last: body };
      return;
    }
    const o = open as { block: Block; indent: number; last: string } | null;
    if (o && o.block.kind !== 'table' && !ENDS.test(o.last) && indent >= o.indent) {
      o.block.text += ` ${body}`;
      o.last = body;
      return;
    }
    const block: Block = { kind: 'p', text: body, depth: depthOf(indent) };
    blocks.push(block);
    open = { block, indent, last: body };
  });
  return blocks;
}

export type CheckStatus = 'PASS' | 'WARN' | 'FAIL' | 'SKIP';
export type Check = { status: CheckStatus; name: string; detail: string };
export type Validity = { label: string; verdict: string; checks: Check[] };

/** validity.txt: "Run validity -- <label>: <VERDICT>", then one check a line. */
export function parseValidity(text: string): Validity | null {
  const lines = text.replace(/\r/g, '').split('\n');
  const head = /^Run validity -- (.+?): (.+)$/.exec(lines[0] ?? '');
  if (!head) return null;
  const checks: Check[] = [];
  for (const line of lines.slice(1)) {
    const m = /^\s+(PASS|WARN|FAIL|SKIP)\s+(.+?)(?: {2,}(.*))?$/.exec(line);
    if (m) checks.push({ status: m[1] as CheckStatus, name: m[2].trim(), detail: (m[3] ?? '').trim() });
  }
  return { label: head[1], verdict: head[2].trim(), checks };
}

/** How far to trust a verdict, as a colour and a short sentence. */
export function verdictTone(verdict: string): { color: string; text: string } {
  if (verdict.startsWith('INVALID')) {
    return { color: 'red', text: "Don't use these numbers: the test itself went wrong." };
  }
  if (verdict.startsWith('OVERLOADED')) {
    return { color: 'orange', text: "The test worked, and the app couldn't keep up with this load: that is the result." };
  }
  if (verdict.startsWith('VALID')) {
    return verdict.includes('warning')
      ? { color: 'yellow', text: 'Usable, with the caveats below.' }
      : { color: 'teal', text: 'The test ran as planned: these numbers can be used.' };
  }
  // a verdict this page doesn't know (a newer analyzer): never shown as good
  return { color: 'gray', text: "This page doesn't know this verdict: read the checks below before using the numbers." };
}

export type ChartInfo = {
  title: string;
  caption: string;
  /** The feature (operation) a per-feature chart is about. */
  feature: string | null;
  /** Where it goes among the others. */
  order: number;
};

const CHARTS: { pattern: RegExp; order: number; title: (f: string) => string; caption: string }[] = [
  {
    pattern: /^layer1_utilization$/,
    order: 10,
    title: () => 'How busy each service is, and what that does to its speed',
    caption:
      "Each line is the model's response time as a service gets busier; the dot is what this test measured. " +
      'Past about 70–80% busy, requests start waiting and response time climbs steeply.',
  },
  {
    pattern: /^measured_mttr$/,
    order: 20,
    title: () => 'How long each part took to come back',
    caption:
      'Measured by stopping each part and timing how long it took to work again. Hatched bars were not measured: ' +
      'they are placeholders.',
  },
  {
    pattern: /^layer2_downtime$/,
    order: 30,
    title: () => 'Expected downtime a year, per part',
    caption:
      'From how often each part is assumed to fail and how fast it came back. Parts that run several copies are ' +
      "close to zero: one copy failing doesn't stop them.",
  },
  {
    pattern: /^layer2_availability_vs_mttr$/,
    order: 40,
    title: () => 'How recovery time changes availability (single-copy parts)',
    caption:
      'Each line shows how available a shared part would be if it took shorter or longer to recover; the dot is ' +
      'the recovery time measured here. More nines means less downtime (5 nines is about 5 minutes a year).',
  },
  {
    pattern: /^layer3_importance_(.+)$/,
    order: 50,
    title: (f) => `Which parts “${f}” depends on most`,
    caption:
      'How much an outage of each part takes this feature down (its Birnbaum importance). The longest bar is the ' +
      'part to protect first.',
  },
  {
    pattern: /^layer4_invest_(.+)$/,
    order: 60,
    title: (f) => `What would help “${f}” most`,
    caption:
      'The downtime a year each change would prevent, such as one more copy of a part. The longest bar is the best ' +
      'single investment.',
  },
  {
    pattern: /^layer4_failure_sensitivity_(.+)$/,
    order: 70,
    title: (f) => `How much “${f}” depends on the failure-rate guess`,
    caption:
      'How often parts fail is an estimate. This shows the downtime a year if everything failed from 10× less to ' +
      '10× more often than assumed; the dashed line is the estimate used.',
  },
  {
    pattern: /^scaling_throughput$/,
    order: 10,
    title: () => 'Requests handled as users grow',
    caption: 'Where a line flattens, that service has reached the most it can handle: its ceiling.',
  },
  {
    pattern: /^scaling_utilization$/,
    order: 20,
    title: () => 'How busy each service gets as users grow',
    caption:
      'The dashed line is the target and the dotted one is fully busy. The first service to cross the target is ' +
      'the bottleneck.',
  },
  {
    pattern: /^scaling_response_time$/,
    order: 30,
    title: () => 'Response time as users grow',
    caption: 'Measured at each level. A sharp rise means a service has started queueing requests.',
  },
  {
    pattern: /^scaling_replicas$/,
    order: 40,
    title: () => 'Copies needed as users grow',
    caption:
      'Solid lines: the copies each service needs at each level to stay under the target. Dotted lines: what it ' +
      'runs now.',
  },
  {
    pattern: /^diagnose_quadrant$/,
    order: 50,
    title: () => 'Add copies, or make the code lighter?',
    caption:
      'Each dot is a service. Far right: each request is heavy work, so making the code lighter pays. Near the ' +
      'top: it is very busy, so more copies help.',
  },
];

/** What a chart shows, from its file name; unknown charts keep their name. */
export function chartInfo(file: string): ChartInfo {
  const stem = file.replace(/\.png$/i, '');
  for (const c of CHARTS) {
    const m = c.pattern.exec(stem);
    if (m) {
      const feature = m[1] ? featureName(m[1]) : null;
      return { title: c.title(feature ?? ''), caption: c.caption, feature, order: c.order };
    }
  }
  return { title: stem.replace(/[_-]+/g, ' '), caption: '', feature: null, order: 100 };
}

/** get_blogs -> "get blogs", as the plain-language report names features. */
export function featureName(operation: string): string {
  return operation.replace(/_/g, ' ');
}

/** A part's text by name (report-explained, validity, …), if the run wrote it. */
export function textOf(part: ResultPart, name: string) {
  return part.texts.find((t) => t.name === name) ?? null;
}

const DATE = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
const TIME = new Intl.DateTimeFormat(undefined, { timeStyle: 'short' });

export function when(iso: string): string {
  return DATE.format(new Date(iso));
}

/** "3 hours ago", "2 days ago". */
export function ago(iso: string, now: number = Date.now()): string {
  const minutes = Math.round((now - Date.parse(iso)) / 60000);
  if (minutes < 1) return 'just now';
  const [n, unit] =
    minutes < 60
      ? [minutes, 'minute']
      : minutes < 60 * 24
        ? [Math.round(minutes / 60), 'hour']
        : minutes < 60 * 24 * 60
          ? [Math.round(minutes / (60 * 24)), 'day']
          : [Math.round(minutes / (60 * 24 * 30)), 'month'];
  return `${n} ${unit}${n === 1 ? '' : 's'} ago`;
}

/** What to tell the user about how current a result is. */
export function freshness(r: ResultSummary, now: number = Date.now()): { current: boolean; title: string; lines: string[] } {
  if (r.current) {
    return { current: true, title: 'From the run you just finished', lines: [`Written ${when(r.writtenAt)}.`] };
  }
  const lines = [
    "No run in this session has replaced them, so they can be out of date with your app and your settings as they " +
      'are now. Run the analysis again (step 8) for fresh results.',
  ];
  if (r.specSavedAt) {
    lines.push(
      `Your settings were saved again at ${TIME.format(new Date(r.specSavedAt))}, after these results, so they don't include those changes.`,
    );
  }
  if (r.unfinishedAfter) {
    lines.push(
      `A later run here started ${when(r.unfinishedAfter)} and didn't finish: these are the last complete results.`,
    );
  }
  return { current: false, title: `Older results: from ${when(r.writtenAt)} (${ago(r.writtenAt, now)})`, lines };
}

/** How a result is named in the list. */
export function resultLabel(r: ResultSummary): string {
  const name = r.system ?? r.folder.split(/[\\/]/).filter(Boolean).slice(-2).join('/');
  const kind = r.kind === 'sweep' ? `load sweep, ${r.levels.length} level${r.levels.length === 1 ? '' : 's'}` : 'single run';
  return `${name} · ${kind} · ${when(r.writtenAt)}${r.current ? ' · just now' : ''}`;
}
