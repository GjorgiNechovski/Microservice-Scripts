import { useEffect, useRef, useState } from 'react';
import type { StartStatus } from '../types';

/**
 * Follows a `docker compose up` the backend runs: its state and output, a line
 * at a time, until it ends; then calls `onEnd`.
 */
export function useStartJob(progress: (after: number) => Promise<StartStatus>, onEnd: () => void) {
  const [job, setJob] = useState<StartStatus | null>(null);
  const [lines, setLines] = useState<{ n: number; text: string }[]>([]);
  const next = useRef(0);
  const ended = useRef(onEnd);
  ended.current = onEnd;

  useEffect(() => {
    if (job?.state !== 'starting') return;
    const timer = setInterval(async () => {
      try {
        const s = await progress(next.current);
        next.current = s.next;
        if (s.lines.length > 0) setLines((l) => [...l, ...s.lines].slice(-400));
        setJob(s);
        if (s.state !== 'starting') ended.current();
      } catch {
        // keep trying
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [job?.state, progress]);

  /** Shows a job that was just started. */
  const begin = (s: StartStatus) => {
    next.current = s.next;
    setLines(s.lines);
    setJob(s);
  };

  return { job, lines, begin };
}
