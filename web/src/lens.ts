// What the dashboard reads (the shapes of /api/fleet and /api/users/{id}) and the plain
// arithmetic it does on them. No model and no keyword rules: every word on screen is
// Hindsight's, every grouping comes from Hindsight's own entities and documents.

export type Version = {
  n: number;
  type: string;
  text: string;
  at: string | null;
  at_is_estimate: boolean;
  reason: string | null;
  new_evidence: string[];
  triggered_by: string[];
};
export type BeliefRecall = { at: string | null; query: string; rank: number; of: number; score: number | null };
export type Belief = {
  id: string;
  text: string;
  status: string;
  flags: string[];
  evidence_count: number;
  created_at: string | null;
  last_changed_at: string | null;
  deleted_at: string | null;
  newest_evidence_at: string | null;
  versions: Version[];
  evidence: string[];
  recalls: BeliefRecall[];
};
export type Fact = {
  text: string | null;
  type: string | null;
  document: string | null;
  chunk: string | null;
  at: string | null;
  missing: boolean;
  entities: string[];
};
export type Hit = { id: string; rank: number; score: number | null; type: string | null };
export type Recall = {
  id: string;
  at: string | null;
  query: string;
  results: number;
  transport: string | null;
  hits: Hit[];
};
export type Summary = {
  id: string;
  flags: string[];
  counts: Record<string, number>;
  first_activity_at: string | null;
  last_activity_at: string | null;
  last_recall_at: string | null;
  last_synced_at: string | null;
};
export type User = Summary & {
  portrait: { text: string; name: string } | null;
  beliefs: Belief[];
  facts: Record<string, Fact>;
  chunks: Record<string, string>;
  recalls: Recall[];
};
export type Collector = {
  last_ping_at: string | null;
  oldest_reasons_check_at: string | null;
  versions: number;
  versions_without_reason: number;
};
export type Fleet = {
  as_of: string;
  time_basis: string;
  collector: Collector;
  fleet: { now: Record<string, number | null>; series: Record<string, number | string | null>[] };
  users: Summary[];
};
/** Only the offline demo has these: the simulated people and their whole conversations. */
export type Persona = {
  display_name: string;
  profile: { full_name: string; about: string };
  sessions: { id: string; started_at: string; messages: { role: string; text: string }[] }[];
};

// -- dates, in the viewer's own time zone ------------------------------------------------------

const dDay = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' });
const dTime = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });
export const day = (s: string | null | undefined) => (s ? dDay.format(new Date(s)) : 'unknown');
export const stamp = (s: string | null | undefined) => (s ? `${day(s)}, ${dTime.format(new Date(s))}` : 'unknown');

const WEEK = 7 * 864e5;
export const withinWeek = (s: string | null | undefined, asOf: string) =>
  !!s && Date.parse(asOf) - Date.parse(s) <= WEEK;

/** "This week memory learned 3 beliefs, rewrote 2." */
export function weekSentence(u: User, asOf: string): string {
  const learned = u.beliefs.filter((b) => withinWeek(b.created_at, asOf)).length;
  const rewrote = u.beliefs.filter((b) =>
    b.versions.some((v) => v.type === 'updated' && withinWeek(v.at, asOf)),
  ).length;
  const forgot = u.beliefs.filter((b) => withinWeek(b.deleted_at, asOf)).length;
  const parts: string[] = [];
  if (learned) parts.push(`learned ${learned} ${learned === 1 ? 'belief' : 'beliefs'}`);
  if (rewrote) parts.push(`rewrote ${rewrote}`);
  if (forgot) parts.push(`forgot ${forgot}`);
  return parts.length ? `This week memory ${parts.join(', ')}.` : 'Nothing changed this week.';
}

// -- text as Hindsight wrote it ------------------------------------------------------------

/** Portraits arrive as markdown; read them as plain sentences. */
export const plainText = (md: string) =>
  md
    .replace(/^#+\s.*$/gm, '')
    .replace(/\*\*([^*]+?):?\*\*:?/g, '$1:')
    .replace(/^\s*[-*]\s+/gm, '')
    .replace(/\s+/g, ' ')
    .trim();

/** Facts end with Hindsight's own annotations ("| When: …", "| Involving: …"). */
export const factText = (t: string | null | undefined) =>
  t ? t.replace(/\s*\|\s*(Involving|When|Where):.*$/s, '') : '';

/** The conversation a change came from: the document of the facts that caused it. */
export const documentOf = (u: User, v: Version) =>
  [...v.new_evidence, ...v.triggered_by].map((id) => u.facts[id]?.document).find(Boolean) ?? null;

/** The last part of a document id, which agents often write as "<user>:<session>". */
export const documentLabel = (doc: string | null | undefined) => (doc ? (String(doc).split(':').pop() ?? doc) : null);

// -- what a belief is about: Hindsight's entities ------------------------------------------------

const MONTHS = 'january february march april may june july august september october november december'.split(' ');
const NOISE = new Set(['user', 'assistant', ...MONTHS]);

/** Entities of the facts behind these beliefs, most mentioned first, minus the ones that say nothing. */
export function entitiesOf(u: User, beliefs: Belief[], ownName: string[] = []): string[] {
  const facts = Object.values(u.facts);
  const mentions = new Map<string, number>();
  for (const f of facts)
    for (const e of new Set(f.entities.map((x) => x.toLowerCase()))) mentions.set(e, (mentions.get(e) ?? 0) + 1);
  // Named in most facts: in practice the person themselves, and as a filter it matches everything.
  const everywhere = (key: string) => facts.length >= 10 && (mentions.get(key) ?? 0) > facts.length / 2;
  const seen = new Map<string, string>();
  for (const b of beliefs)
    for (const id of b.evidence)
      for (const e of u.facts[id]?.entities ?? []) {
        const key = e.toLowerCase();
        if (!NOISE.has(key) && !ownName.includes(key) && !everywhere(key)) seen.set(key, e);
      }
  return [...seen.entries()]
    .sort((a, b) => (mentions.get(b[0]) ?? 0) - (mentions.get(a[0]) ?? 0) || a[0].localeCompare(b[0]))
    .map(([, e]) => e);
}

/** The entities most beliefs mention, for filtering; a belief can match several. */
export function topEntities(
  u: User,
  groups: Belief[][],
  ownName: string[] = [],
  most = 8,
): { name: string; count: number }[] {
  const count = new Map<string, number>();
  for (const g of groups) for (const n of entitiesOf(u, g, ownName)) count.set(n, (count.get(n) ?? 0) + 1);
  return [...count.entries()]
    .filter(([, c]) => c > 1)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, most)
    .map(([name, c]) => ({ name, count: c }));
}

// -- near-duplicates: shared words, nothing cleverer -------------------------------------------------

const STOP = new Set(
  'that this with from have they their will would about been were what when which into than then them your also only more most some such very just over after before because'.split(
    ' ',
  ),
);
function words(text: string): Set<string> {
  // Any script: Hindsight writes some beliefs in Hindi.
  return new Set((text.toLowerCase().match(/[\p{L}\p{N}']{3,}/gu) ?? []).filter((w) => !STOP.has(w)));
}
function jaccard(a: Set<string>, b: Set<string>) {
  let shared = 0;
  for (const w of a) if (b.has(w)) shared++;
  return shared / (a.size + b.size - shared || 1);
}

/** Live beliefs that say nearly the same thing, grouped; the best supported leads each group. */
export function nearDuplicates(u: User, threshold = 0.5): Belief[][] {
  const live = u.beliefs.filter((b) => b.status === 'live');
  const sets = live.map((b) => words(b.text));
  const parent = live.map((_, i) => i);
  const find = (i: number): number => (parent[i] === i ? i : (parent[i] = find(parent[i])));
  for (let i = 0; i < live.length; i++)
    for (let j = i + 1; j < live.length; j++)
      // Too few words to compare is not similarity.
      if (sets[i].size >= 5 && sets[j].size >= 5 && jaccard(sets[i], sets[j]) >= threshold) parent[find(j)] = find(i);
  const groups = new Map<number, Belief[]>();
  live.forEach((b, i) => groups.set(find(i), [...(groups.get(find(i)) ?? []), b]));
  return [...groups.values()].map((g) =>
    g.sort(
      (a, b) => b.evidence_count - a.evidence_count || (b.last_changed_at ?? '').localeCompare(a.last_changed_at ?? ''),
    ),
  );
}

// -- a rewrite, word by word ------------------------------------------------------------------------

export type Piece = { op: 'same' | 'del' | 'add'; text: string };
export function redline(before: string, after: string): Piece[] {
  const a = before.split(/(\s+)/),
    b = after.split(/(\s+)/);
  const L = Array.from({ length: a.length + 1 }, () => new Array<number>(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i--)
    for (let j = b.length - 1; j >= 0; j--)
      L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out: Piece[] = [];
  const push = (op: Piece['op'], text: string) => {
    const last = out[out.length - 1];
    if (last?.op === op) last.text += text;
    else out.push({ op, text });
  };
  let i = 0,
    j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) (push('same', a[i]), i++, j++);
    else if (L[i + 1][j] >= L[i][j + 1]) push('del', a[i++]);
    else push('add', b[j++]);
  }
  while (i < a.length) push('del', a[i++]);
  while (j < b.length) push('add', b[j++]);
  return out;
}

/** Most of the wording changed: show was, then now, rather than a wall of marks. */
export function isRewrite(before: string, after: string): boolean {
  const changed = redline(before, after)
    .filter((p) => p.op !== 'same')
    .reduce((n, p) => n + p.text.length, 0);
  return changed / (before.length + after.length || 1) > 0.4;
}

// -- what a recall returned -------------------------------------------------------------------------

const ms = (s: string | null | undefined) => (s ? Date.parse(s) : NaN);

export type HitRow = {
  rank: number;
  id: string;
  kind: 'belief' | 'fact' | 'unknown';
  /** As Hindsight reported it, even when Lens no longer holds the item. */
  isBelief: boolean;
  text: string | null;
  /** What became of a belief after this recall returned it. */
  since: 'rewritten' | 'forgotten' | null;
};

/** Every result in rank order, each belief worded as it stood when the recall ran. */
export function hitRows(u: User, r: Recall): HitRow[] {
  const beliefs = new Map(u.beliefs.map((b) => [b.id, b]));
  const at = ms(r.at);
  return r.hits.map((h) => {
    const isBelief = h.type === 'observation';
    const b = beliefs.get(h.id);
    if (b) {
      const before = b.versions.filter((v) => !(ms(v.at) > at) && !isGone(v.type));
      const after = b.versions.filter((v) => ms(v.at) > at);
      const since = after.some((v) => isGone(v.type))
        ? 'forgotten'
        : after.some((v) => v.type === 'updated')
          ? 'rewritten'
          : null;
      return {
        rank: h.rank,
        id: h.id,
        kind: 'belief',
        isBelief: true,
        text: (before[before.length - 1] ?? b.versions[0])?.text ?? b.text,
        since,
      };
    }
    const f = u.facts[h.id];
    if (f?.text) return { rank: h.rank, id: h.id, kind: 'fact', isBelief, text: factText(f.text), since: null };
    return { rank: h.rank, id: h.id, kind: 'unknown', isBelief, text: null, since: null };
  });
}

export const isGone = (type: string) => type === 'deleted' || type === 'invalidated';

// -- sessions: the documents facts came from ------------------------------------------------------------

export type Session = {
  id: string;
  document: string;
  at: string | null;
  changes: { b: Belief; v: Version }[];
  replies: Recall[];
  /** What Hindsight kept of the conversation: the text of each chunk facts came from. */
  fragments: string[];
  /** The whole conversation, only in the offline demo. */
  messages: { role: string; text: string }[];
};

/** Conversations newest first: what memory took from each, and the replies until the next one. */
export function buildSessions(u: User, persona?: Persona): Session[] {
  const byDoc = new Map<string, Session>();
  const session = (doc: string) => {
    let s = byDoc.get(doc);
    if (!s)
      byDoc.set(
        doc,
        (s = {
          id: documentLabel(doc) ?? doc,
          document: doc,
          at: null,
          changes: [],
          replies: [],
          fragments: [],
          messages: [],
        }),
      );
    return s;
  };
  for (const f of Object.values(u.facts)) {
    if (!f.document) continue;
    const s = session(f.document);
    if (f.at && (!s.at || ms(f.at) < ms(s.at))) s.at = f.at;
    const chunk = f.chunk ? u.chunks[f.chunk] : null;
    if (chunk && !s.fragments.includes(chunk)) s.fragments.push(chunk);
  }
  for (const b of u.beliefs)
    for (const v of b.versions) {
      const doc = documentOf(u, v);
      if (doc) session(doc).changes.push({ b, v });
    }
  for (const p of persona?.sessions ?? []) {
    const s = [...byDoc.values()].find((x) => x.id === p.id);
    if (s) {
      s.messages = p.messages;
      s.at = p.started_at;
    }
  }
  // Times are compared as instants: Hindsight and the demo write different offsets.
  const ordered = [...byDoc.values()].sort((a, b) => (ms(a.at) || 0) - (ms(b.at) || 0));
  for (const r of u.recalls)
    [...ordered]
      .reverse()
      .find((s) => ms(s.at) <= ms(r.at))
      ?.replies.push(r);
  return ordered.reverse();
}

// -- flags --------------------------------------------------------------------------------------------

export const FLAG_LABEL: Record<string, string> = {
  new: 'New',
  changed: 'Rewritten',
  deleted: 'Forgotten',
  thin: 'One source',
  stale: 'Stale',
  never_recalled: 'Never used',
  high_churn: 'Many changes',
  empty_recalls: 'Empty recalls',
};
/** Flags that describe normal memory; the rest ask for a look. One source is normal: Hindsight
 *  writes most beliefs from a single fact (88% on the spike), so it would flag every user. */
export const ROUTINE = ['changed', 'new', 'deleted', 'thin'];
export const needsLook = (flags: string[]) => flags.some((f) => !ROUTINE.includes(f));
