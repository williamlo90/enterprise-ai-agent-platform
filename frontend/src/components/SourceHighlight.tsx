export interface TextRange { start: number; end: number }

// Offsets are UTF-16 string offsets from a verified source consumer, not HTML.
export function SourceHighlight({ text, ranges = [] }: { text: string; ranges?: readonly TextRange[] }) {
  const valid = ranges.filter(({ start, end }) => Number.isInteger(start) && Number.isInteger(end) &&
    start >= 0 && end > start && end <= text.length).sort((a, b) => a.start - b.start);
  const merged: TextRange[] = [];
  for (const range of valid) {
    const last = merged.at(-1);
    if (last && range.start <= last.end) last.end = Math.max(last.end, range.end);
    else merged.push({ ...range });
  }
  let cursor = 0;
  const parts = merged.map(({ start, end }) => {
    const before = text.slice(cursor, start);
    cursor = end;
    return <span key={start}>{before}<mark>{text.slice(start, end)}</mark></span>;
  });
  return <div className="source-text">{parts}{text.slice(cursor)}</div>;
}
