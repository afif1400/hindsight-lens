// One user: a list on the left (beliefs, sessions or recalls), the selected item read in
// full on the right; on a phone the reading pane is a drawer. The selection lives in the
// URL (#/u/<bank>/<kind>/<id>) so a link reopens the same thing.
import * as React from 'react';
import { RiArrowDownSLine, RiSearchLine } from '@remixicon/react';
import * as Drawer from '@/components/ui/drawer';
import * as Input from '@/components/ui/input';
import * as SegmentedControl from '@/components/ui/segmented-control';
import { aboutOf, nameOf, ownName, personas } from './data';
import { type Mode, fleetHref, replace, userHref } from './routes';
import {
  type Belief,
  type Recall,
  type Session,
  type User,
  buildSessions,
  day,
  entitiesOf,
  nearDuplicates,
  plainText,
  topEntities,
  weekSentence,
  withinWeek,
} from './lens';
import { BeliefDetail, Bone, Flags, Muted, RecallResults, VERB, VersionText } from './shared';

/** One row of the list. A question asked several times is one row holding every recall of it. */
type Item =
  | { key: string; id: string; kind: 'belief'; b: Belief; similar: Belief[] }
  | { key: string; id: string; kind: 'session'; s: Session }
  | { key: string; id: string; kind: 'recall'; recalls: Recall[] };
type Group = { name: string; items: Item[] };

const MODES: [Mode, string][] = [
  ['belief', 'Beliefs'],
  ['session', 'Sessions'],
  ['recall', 'Recalls'],
];

export function UserPage({ u, asOf, mode, selected }: { u: User; asOf: string; mode: Mode; selected: string | null }) {
  const [q, setQ] = React.useState('');
  const [entity, setEntity] = React.useState<string | null>(null);
  const [closed, setClosed] = React.useState<Record<string, boolean>>({});
  // On a phone, a link to one thing opens straight onto it.
  const [drawer, setDrawer] = React.useState(selected !== null);
  const wide = useWide();
  const own = ownName(u.id);
  const dupes = React.useMemo(() => nearDuplicates(u), [u]);
  const entities = React.useMemo(() => topEntities(u, dupes, own), [u, dupes]); // `own` follows from `u`
  const sessions = React.useMemo(() => buildSessions(u, personas[u.id]), [u]);
  const groups = buildGroups(u, mode, q, asOf, dupes, sessions, entity, own);
  const visible = groups.flatMap((g) => (closed[g.name] && !q ? [] : g.items));
  const matches = (i: Item) => i.id === selected || (i.kind === 'recall' && i.recalls.some((r) => r.id === selected));
  const sel = visible.find(matches) ?? (wide ? visible[0] : undefined) ?? null;

  const select = (item: Item, openDrawer = true) => {
    replace(userHref(u.id, item.kind, item.id));
    if (!wide && openDrawer) setDrawer(true);
  };
  const switchMode = (m: Mode) => {
    setQ('');
    replace(userHref(u.id, m));
  };

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.closest('input, textarea, [role=dialog]') || !visible.length) return;
      const at = Math.max(
        0,
        visible.findIndex((i) => i.key === sel?.key),
      );
      if (e.key === 'j') select(visible[Math.min(visible.length - 1, at + 1)], false);
      if (e.key === 'k') select(visible[Math.max(0, at - 1)], false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
  React.useEffect(() => {
    if (sel) document.querySelector(`[data-key="${CSS.escape(sel.key)}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [sel?.key]);

  const detail = sel ? (
    <Detail u={u} item={sel} own={own} selected={selected} />
  ) : (
    <Muted>Nothing matches. Clear the search or the filter.</Muted>
  );
  const c = u.counts;

  return (
    <main className='mx-auto max-w-6xl px-4 py-8 sm:px-6'>
      <div className='space-y-2'>
        <a href={fleetHref()} className='text-paragraph-xs text-text-soft-400 hover:text-text-sub-600'>
          Fleet
        </a>
        <h1 className='break-words text-title-h5'>{nameOf(u.id)}</h1>
        {aboutOf(u.id) ? <p className='text-paragraph-sm text-text-sub-600'>{aboutOf(u.id)}</p> : null}
        <p className='text-paragraph-sm'>{weekSentence(u, asOf)}</p>
        <p className='text-paragraph-xs text-text-soft-400'>
          {c.live} live {c.live === 1 ? 'belief' : 'beliefs'}, {c.deleted} forgotten, {c.facts} facts from {c.sessions}{' '}
          {c.sessions === 1 ? 'conversation' : 'conversations'}, {c.recalls} {c.recalls === 1 ? 'recall' : 'recalls'}.
        </p>
        <Flags flags={u.flags} only='attention' />
        {u.portrait ? <Portrait text={plainText(u.portrait.text)} /> : null}
      </div>

      <div className='mt-8 grid gap-8 lg:grid-cols-[23rem_1fr]'>
        <aside className='flex min-w-0 flex-col gap-3 lg:sticky lg:top-20 lg:h-[calc(100dvh-6rem)]'>
          <SegmentedControl.Root value={mode} onValueChange={(v) => switchMode(v as Mode)}>
            <SegmentedControl.List>
              {MODES.map(([m, label]) => (
                <SegmentedControl.Trigger key={m} value={m}>
                  {label}
                </SegmentedControl.Trigger>
              ))}
            </SegmentedControl.List>
          </SegmentedControl.Root>
          <Input.Root size='small'>
            <Input.Wrapper>
              <Input.Icon as={RiSearchLine} />
              <Input.Input
                aria-label='Search'
                placeholder={{ belief: 'Search beliefs', session: 'Search sessions', recall: 'Search questions' }[mode]}
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
            </Input.Wrapper>
          </Input.Root>
          {mode === 'belief' && entities.length ? (
            <div className='flex flex-wrap gap-1.5' role='group' aria-label='Show beliefs that mention'>
              {entities.map((e) => (
                <button
                  key={e.name}
                  type='button'
                  aria-pressed={entity === e.name}
                  onClick={() => setEntity(entity === e.name ? null : e.name)}
                  className={`rounded-10 px-2.5 py-1 text-paragraph-xs ring-1 ring-inset ${
                    entity === e.name
                      ? 'bg-bg-strong-950 text-text-white-0 ring-transparent'
                      : 'text-text-sub-600 ring-stroke-soft-200 hover:bg-bg-weak-50'
                  }`}
                >
                  {e.name} <span className={entity === e.name ? 'opacity-70' : 'text-text-soft-400'}>{e.count}</span>
                </button>
              ))}
            </div>
          ) : null}

          <nav
            className='-mx-1 min-h-0 flex-1 overflow-y-auto px-1 pb-8'
            aria-label={MODES.find(([m]) => m === mode)?.[1]}
          >
            {groups.map((g) => (
              <section key={g.name || 'all'} className='pb-2'>
                {g.name ? (
                  <button
                    type='button'
                    aria-expanded={!(closed[g.name] && !q)}
                    onClick={() => setClosed({ ...closed, [g.name]: !closed[g.name] })}
                    className='sticky top-0 z-[1] flex w-full items-center gap-2 bg-bg-white-0 py-2 text-left'
                  >
                    <span className='text-label-xs text-text-strong-950'>{g.name}</span>
                    <span className='text-paragraph-xs text-text-soft-400'>{g.items.length}</span>
                    <RiArrowDownSLine
                      className={`ml-auto size-4 text-text-soft-400 ${closed[g.name] && !q ? '-rotate-90' : ''}`}
                    />
                  </button>
                ) : null}
                {closed[g.name] && !q ? null : (
                  <ol>
                    {g.items.map((item) => (
                      <li key={item.key} data-key={item.key}>
                        <button
                          type='button'
                          onClick={() => select(item)}
                          aria-current={item.key === sel?.key && wide ? 'true' : undefined}
                          className={`w-full rounded-10 px-3 py-2 text-left text-paragraph-sm ${
                            item.key === sel?.key && wide
                              ? 'bg-bg-weak-50 text-text-strong-950'
                              : 'text-text-sub-600 hover:bg-bg-weak-50'
                          }`}
                        >
                          <RowText item={item} />
                        </button>
                      </li>
                    ))}
                  </ol>
                )}
              </section>
            ))}
            {!groups.length ? (
              <Muted className='px-3'>
                {q || entity ? 'Nothing matches. Clear the search or the filter.' : EMPTY[mode]}
              </Muted>
            ) : null}
          </nav>
        </aside>

        {wide ? <article className='min-w-0 border-l border-stroke-soft-200 pl-8'>{detail}</article> : null}
      </div>

      {!wide ? (
        <Drawer.Root open={drawer && !!sel} onOpenChange={setDrawer}>
          <Drawer.Content className='max-w-full'>
            <Drawer.Header>
              <Drawer.Title>
                {{ belief: 'A belief', session: 'A conversation', recall: 'A recall' }[sel?.kind ?? 'belief']}
              </Drawer.Title>
            </Drawer.Header>
            <Drawer.Body className='p-5'>{detail}</Drawer.Body>
          </Drawer.Content>
        </Drawer.Root>
      ) : null}
    </main>
  );
}

const EMPTY: Record<Mode, string> = {
  belief: 'Memory holds no beliefs about this user yet.',
  session: 'No conversation has reached memory yet.',
  recall: 'Nothing has been recalled for this user. Recalls need Hindsight’s audit log switched on.',
};

// -- the list ----------------------------------------------------------------------------------------

function RowText({ item }: { item: Item }) {
  if (item.kind === 'belief')
    return (
      <>
        <span
          className={`line-clamp-2 break-words ${item.b.status !== 'live' ? 'line-through decoration-text-soft-400' : ''}`}
        >
          {item.b.text}
        </span>
        {item.similar.length ? (
          <span className='mt-0.5 block text-paragraph-xs text-text-soft-400'>{item.similar.length} similar</span>
        ) : null}
      </>
    );
  if (item.kind === 'session')
    return (
      <span className='flex items-baseline justify-between gap-3'>
        <span className='text-text-strong-950'>{day(item.s.at)}</span>
        <span className='truncate text-paragraph-xs text-text-soft-400'>
          {item.s.changes.length
            ? `${item.s.changes.length} ${item.s.changes.length === 1 ? 'change' : 'changes'}`
            : 'no change'}
          {item.s.replies.length ? `, ${item.s.replies.length} recalled` : ''}
        </span>
      </span>
    );
  return (
    <>
      <span className='line-clamp-2 break-words'>{item.recalls[0].query}</span>
      <span className='mt-0.5 block text-paragraph-xs text-text-soft-400'>
        {day(item.recalls[0].at)}
        {item.recalls.length > 1 ? `, asked ${item.recalls.length} times` : ''}
      </span>
    </>
  );
}

function buildGroups(
  u: User,
  mode: Mode,
  q: string,
  asOf: string,
  dupes: Belief[][],
  sessions: Session[],
  entity: string | null,
  own: string[],
): Group[] {
  const match = (t: string) => !q || t.toLowerCase().includes(q.toLowerCase());
  if (mode === 'recall') {
    const byQuery = new Map<string, Item & { kind: 'recall' }>();
    for (const r of u.recalls) {
      if (!match(r.query)) continue;
      const row = byQuery.get(r.query);
      if (row) row.recalls.push(r);
      else byQuery.set(r.query, { key: r.id, id: r.id, kind: 'recall', recalls: [r] });
    }
    return byQuery.size ? [{ name: '', items: [...byQuery.values()] }] : [];
  }
  if (mode === 'session') {
    const items: Item[] = sessions
      .filter((s) =>
        match([s.id, ...s.fragments, ...s.messages.map((m) => m.text), ...s.changes.map((c) => c.v.text)].join(' ')),
      )
      .map((s) => ({ key: s.document, id: s.document, kind: 'session', s }));
    return items.length ? [{ name: '', items }] : [];
  }

  // Beliefs: what was rewritten this week, everything newest first, what memory forgot.
  // The entity filter narrows all three; keys carry the group since a belief can sit in two.
  const mentions = (g: Belief[]) => !entity || entitiesOf(u, g, own).includes(entity);
  const asItem = (g: Belief[], group: string): Item => ({
    key: `${group}:${g[0].id}`,
    id: g[0].id,
    kind: 'belief',
    b: g[0],
    similar: g.slice(1),
  });
  const groups: Group[] = [];
  const recent = u.beliefs.filter(
    (b) =>
      b.status === 'live' &&
      b.versions.some((v) => v.type === 'updated' && withinWeek(v.at, asOf)) &&
      match(b.text) &&
      mentions([b]),
  );
  if (recent.length) groups.push({ name: 'Rewritten this week', items: recent.map((b) => asItem([b], 'week')) });
  const all = dupes
    .filter((g) => g.some((b) => match(b.text)) && mentions(g))
    .sort((a, b) => (b[0].last_changed_at ?? '').localeCompare(a[0].last_changed_at ?? ''));
  if (all.length)
    groups.push({ name: entity ? `Mentions ${entity}` : 'All beliefs', items: all.map((g) => asItem(g, 'all')) });
  const gone = u.beliefs.filter((b) => b.status !== 'live' && match(b.text) && mentions([b]));
  if (gone.length) groups.push({ name: 'Forgotten', items: gone.map((b) => asItem([b], 'gone')) });
  return groups;
}

// -- the reading pane ----------------------------------------------------------------------------------

function Detail({ u, item, own, selected }: { u: User; item: Item; own: string[]; selected: string | null }) {
  if (item.kind === 'recall') {
    // A link to a later asking of the same question opens that one.
    const r = item.recalls.find((x) => x.id === selected) ?? item.recalls[0];
    return <RecallResults key={r.id} u={u} r={r} />;
  }
  if (item.kind === 'session') return <SessionStory s={item.s} />;
  const about = entitiesOf(u, [item.b, ...item.similar], own);
  return (
    <div className='space-y-8'>
      <BeliefDetail u={u} b={item.b} />
      {item.similar.length ? (
        <section className='space-y-2'>
          <h3 className='text-label-sm'>Says nearly the same</h3>
          <ul className='space-y-1.5 border-l-2 border-stroke-soft-200 pl-3'>
            {item.similar.map((b) => (
              <li key={b.id} className='text-paragraph-sm text-text-sub-600'>
                {b.text}
              </li>
            ))}
          </ul>
          <p className='text-paragraph-xs text-text-soft-400'>Lens groups beliefs that share most of their words.</p>
        </section>
      ) : null}
      {about.length ? (
        <section className='space-y-2'>
          <h3 className='text-label-sm'>About</h3>
          <p className='text-paragraph-sm text-text-sub-600'>{about.join(', ')}</p>
          <p className='text-paragraph-xs text-text-soft-400'>Entities Hindsight extracted from its sources.</p>
        </section>
      ) : null}
    </div>
  );
}

function SessionStory({ s }: { s: Session }) {
  return (
    <div className='space-y-8'>
      <div className='space-y-1'>
        <p className='text-paragraph-md'>{day(s.at)}</p>
        <p className='break-words text-paragraph-xs text-text-soft-400'>Conversation {s.id}</p>
      </div>
      {s.replies.length ? (
        <section className='space-y-2'>
          <h3 className='text-label-sm'>Asked memory</h3>
          <ul className='space-y-1.5'>
            {[...new Set(s.replies.map((r) => r.query))].map((query) => (
              <li key={query} className='text-paragraph-sm'>
                {query}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <section className='space-y-3'>
        <h3 className='text-label-sm'>
          {s.changes.length ? `What memory took: ${s.changes.length}` : 'Memory took nothing from this conversation'}
        </h3>
        <ul className='space-y-3'>
          {s.changes.map(({ b, v }) => (
            <li key={`${b.id}${v.n}`} className='space-y-0.5'>
              <VersionText b={b} v={v} />
              <p className='text-paragraph-xs text-text-soft-400'>
                {VERB[v.type] ?? v.type}. Why: {v.reason ?? 'not captured'}
              </p>
            </li>
          ))}
        </ul>
      </section>
      {s.messages.length ? (
        <details>
          <summary className='cursor-pointer text-label-sm'>The conversation, {s.messages.length} messages</summary>
          <div className='mt-3 space-y-2 text-paragraph-sm'>
            {s.messages.map((m, i) => (
              <p
                key={i}
                className={m.role === 'user' ? '' : 'border-l-2 border-stroke-soft-200 pl-3 text-text-sub-600'}
              >
                {m.text}
              </p>
            ))}
          </div>
        </details>
      ) : s.fragments.length ? (
        <details>
          <summary className='cursor-pointer text-label-sm'>What Hindsight kept of the conversation</summary>
          <div className='mt-3 space-y-3 text-paragraph-sm text-text-sub-600'>
            {s.fragments.map((f, i) => (
              <p key={i} className='whitespace-pre-wrap border-l-2 border-stroke-soft-200 pl-3'>
                {f}
              </p>
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

function Portrait({ text }: { text: string }) {
  const [open, setOpen] = React.useState(false);
  return (
    <button
      type='button'
      onClick={() => setOpen(!open)}
      aria-expanded={open}
      className='block max-w-[65ch] text-left text-paragraph-sm text-text-sub-600'
    >
      <span className={open ? '' : 'line-clamp-2'}>{text}</span>
    </button>
  );
}

export function UserSkeleton() {
  return (
    <main className='mx-auto max-w-6xl px-4 py-8 sm:px-6' aria-busy='true'>
      <div className='space-y-3'>
        <Bone className='h-3 w-10' />
        <Bone className='h-7 w-48' />
        <Bone className='h-4 w-72 max-w-full' />
      </div>
      <div className='mt-8 grid gap-8 lg:grid-cols-[23rem_1fr]'>
        <div className='space-y-3'>
          <Bone className='h-9 w-full' />
          <Bone className='h-9 w-full' />
          {Array.from({ length: 7 }, (_, i) => (
            <Bone key={i} className='h-10 w-full' />
          ))}
        </div>
        <div className='hidden space-y-3 lg:block'>
          <Bone className='h-6 w-4/5' />
          <Bone className='h-4 w-2/5' />
          <Bone className='mt-6 h-24 w-full' />
        </div>
      </div>
    </main>
  );
}

function useWide() {
  const query = '(min-width: 1024px)';
  const [wide, setWide] = React.useState(() => window.matchMedia(query).matches);
  React.useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setWide(m.matches);
    m.addEventListener('change', on);
    return () => m.removeEventListener('change', on);
  }, []);
  return wide;
}
