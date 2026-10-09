import * as React from 'react';
import { RiArrowLeftSLine, RiArrowRightSLine, RiSearchLine } from '@remixicon/react';
import { fleetHref, go, userHref } from './routes';
import * as Badge from '@/components/ui/badge';
import * as Button from '@/components/ui/button';
import * as CommandMenu from '@/components/ui/command-menu';
import * as Kbd from '@/components/ui/kbd';
import * as SegmentedControl from '@/components/ui/segmented-control';
import { aboutOf, nameOf } from './data';
import {
  FLAG_LABEL,
  type Belief,
  type Recall,
  type Summary,
  type User,
  type Version,
  day,
  documentLabel,
  documentOf,
  factText,
  hitRows,
  isGone,
  isRewrite,
  needsLook,
  redline,
  stamp,
} from './lens';

export function Muted({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <span className={`text-paragraph-sm text-text-sub-600 ${className}`}>{children}</span>;
}

/** Flags as quiet words; only the ones that need a look get the amber accent. */
export function Flags({ flags, only }: { flags: string[]; only?: 'attention' }) {
  const shown = only ? flags.filter((f) => needsLook([f])) : flags;
  if (!shown.length) return null;
  return (
    <span className='inline-flex flex-wrap gap-1'>
      {shown.map((f) => (
        <Badge.Root key={f} size='medium' variant='lighter' color={needsLook([f]) ? 'orange' : 'gray'}>
          {FLAG_LABEL[f] ?? f}
        </Badge.Root>
      ))}
    </span>
  );
}

/** Small edits read best inline; a near-total rewrite reads best as was, then now. */
export function Redline({ before, after }: { before: string; after: string }) {
  if (isRewrite(before, after))
    return (
      <div className='space-y-1 text-paragraph-sm leading-relaxed'>
        <p className='text-text-soft-400'>
          <span className='mr-2 text-label-xs'>Was</span>
          {before}
        </p>
        <p>
          <span className='mr-2 text-label-xs text-text-sub-600'>Now</span>
          {after}
        </p>
      </div>
    );
  return (
    <p className='text-paragraph-sm leading-relaxed'>
      {redline(before, after).map((p, i) =>
        p.op === 'same' ? (
          <span key={i}>{p.text}</span>
        ) : p.op === 'del' ? (
          <del key={i} className='bg-error-lighter text-error-base'>
            {p.text}
          </del>
        ) : (
          <ins key={i} className='bg-success-lighter text-success-base no-underline'>
            {p.text}
          </ins>
        ),
      )}
    </p>
  );
}

export function Section({
  title,
  aside,
  children,
}: {
  title: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className='space-y-3'>
      <div className='flex items-baseline justify-between gap-4'>
        <h3 className='text-label-sm text-text-strong-950'>{title}</h3>
        {aside ? <span className='text-paragraph-xs text-text-soft-400'>{aside}</span> : null}
      </div>
      {children}
    </section>
  );
}

// -- a belief in full ------------------------------------------------------------------------

export const VERB: Record<string, string> = {
  created: 'Learned',
  earliest_known: 'First seen',
  updated: 'Rewritten',
  deleted: 'Forgotten',
  invalidated: 'Forgotten',
};

/** One version's wording: a redline against the version before it when it was a rewrite. */
export function VersionText({ b, v }: { b: Belief; v: Version }) {
  const prev = b.versions[v.n - 2];
  if (v.type === 'updated' && prev) return <Redline before={prev.text} after={v.text} />;
  return (
    <p className={`text-paragraph-sm ${isGone(v.type) ? 'text-text-soft-400 line-through' : 'text-text-sub-600'}`}>
      {v.text}
    </p>
  );
}

export function BeliefDetail({ u, b }: { u: User; b: Belief }) {
  const versions = [...b.versions].reverse();
  const [allRecalls, setAllRecalls] = React.useState(false);
  return (
    <div className='space-y-8'>
      <div className='space-y-2'>
        <p className='text-paragraph-md text-text-strong-950'>{b.text}</p>
        <p className='text-paragraph-xs text-text-soft-400'>
          {b.status === 'live' ? 'Current wording' : 'Last wording before it was forgotten'}. {b.evidence_count}{' '}
          {b.evidence_count === 1 ? 'source' : 'sources'}, returned in {b.recalls.length}{' '}
          {b.recalls.length === 1 ? 'recall' : 'recalls'}.
        </p>
        <Flags flags={b.flags} />
      </div>

      <Section title='History' aside={`${b.versions.length} ${b.versions.length === 1 ? 'version' : 'versions'}`}>
        {b.versions.some((v) => v.type === 'updated') ? (
          <p className='-mt-1 flex gap-3 text-paragraph-xs text-text-soft-400'>
            <span>
              <del className='bg-error-lighter text-error-base'>struck</del> was removed
            </span>
            <span>
              <ins className='bg-success-lighter text-success-base no-underline'>marked</ins> was added
            </span>
          </p>
        ) : null}
        <ol className='space-y-5'>
          {versions.map((v) => {
            const doc = documentOf(u, v);
            return (
              <li key={v.n} className='grid grid-cols-[6.5rem_1fr] gap-x-4 gap-y-1'>
                <div className='text-paragraph-xs text-text-soft-400'>
                  <div className='text-label-xs text-text-sub-600'>{VERB[v.type] ?? v.type}</div>
                  <div>{v.at ? `${day(v.at)}${v.at_is_estimate ? ', about' : ''}` : 'before collection'}</div>
                  {doc ? <div className='truncate'>{documentLabel(doc)}</div> : null}
                </div>
                <div className='min-w-0 space-y-1'>
                  <VersionText b={b} v={v} />
                  <p className='text-paragraph-xs text-text-soft-400'>Why: {v.reason ?? 'not captured'}</p>
                </div>
              </li>
            );
          })}
        </ol>
      </Section>

      <Section title='Sources' aside={b.evidence.length ? `${b.evidence.length}` : undefined}>
        {b.evidence.length ? (
          <ul className='space-y-3'>
            {b.evidence.map((id) => {
              const f = u.facts[id];
              const chunk = f?.chunk ? u.chunks[f.chunk] : null;
              return (
                <li key={id} className='space-y-1'>
                  <p className='text-paragraph-sm'>{factText(f?.text) || 'Not collected'}</p>
                  <p className='text-paragraph-xs text-text-soft-400'>
                    {documentLabel(f?.document) ?? 'unknown conversation'}, {day(f?.at)}
                  </p>
                  {chunk ? (
                    <details className='text-paragraph-xs text-text-sub-600'>
                      <summary className='cursor-pointer text-text-soft-400'>What was said</summary>
                      <p className='mt-1 whitespace-pre-wrap border-l-2 border-stroke-soft-200 pl-3'>{chunk}</p>
                    </details>
                  ) : null}
                </li>
              );
            })}
          </ul>
        ) : (
          <Muted>No sources were collected.</Muted>
        )}
      </Section>

      <Section title='Returned in recalls' aside={b.recalls.length ? `${b.recalls.length}` : undefined}>
        {b.recalls.length ? (
          <ul className='space-y-2'>
            {(allRecalls ? b.recalls : b.recalls.slice(0, 8)).map((r, i) => (
              <li key={i} className='grid grid-cols-[1fr_auto] gap-4 text-paragraph-sm'>
                <span className='truncate'>{r.query}</span>
                <span className='whitespace-nowrap text-paragraph-xs text-text-soft-400'>
                  {r.rank} of {r.of}, {day(r.at)}
                </span>
              </li>
            ))}
            {b.recalls.length > 8 && !allRecalls ? (
              <button
                type='button'
                onClick={() => setAllRecalls(true)}
                className='text-paragraph-xs text-text-sub-600 underline underline-offset-4'
              >
                Show all {b.recalls.length}
              </button>
            ) : null}
          </ul>
        ) : (
          <Muted>No recall has returned this belief.</Muted>
        )}
        <p className='text-paragraph-xs text-text-soft-400'>
          Returned is not used: Hindsight does not record whether a reply relied on it.
        </p>
      </Section>
    </div>
  );
}

// -- what a recall returned ----------------------------------------------------------------------

/** Ranked results, beliefs first by default, a page at a time. */
export function RecallResults({ u, r, pageSize = 15 }: { u: User; r: Recall; pageSize?: number }) {
  const [kind, setKind] = React.useState<'belief' | 'all'>('belief');
  const [page, setPage] = React.useState(0);
  const all = hitRows(u, r);
  const rows = kind === 'all' ? all : all.filter((h) => h.isBelief);
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  const beliefs = all.filter((h) => h.isBelief).length;
  const gone = all.filter((h) => h.kind === 'unknown').length;
  return (
    <div className='space-y-4'>
      <div className='space-y-1'>
        <p className='text-paragraph-md'>{r.query}</p>
        <p className='text-paragraph-xs text-text-soft-400'>
          {stamp(r.at)}. Returned {r.results}: {beliefs} {beliefs === 1 ? 'belief' : 'beliefs'} and{' '}
          {r.results - beliefs} {r.results - beliefs === 1 ? 'fact' : 'facts'}. Beliefs are worded as they stood then.
          {gone ? ` Lens no longer holds ${gone === all.length ? 'any of them' : `${gone} of them`}.` : ''}
        </p>
      </div>
      <SegmentedControl.Root value={kind} onValueChange={(v) => (setKind(v as 'belief' | 'all'), setPage(0))}>
        <SegmentedControl.List>
          <SegmentedControl.Trigger value='belief'>Beliefs {beliefs}</SegmentedControl.Trigger>
          <SegmentedControl.Trigger value='all'>Everything {all.length}</SegmentedControl.Trigger>
        </SegmentedControl.List>
      </SegmentedControl.Root>
      {rows.length ? (
        <ol className='divide-y divide-stroke-soft-200'>
          {rows.slice(page * pageSize, (page + 1) * pageSize).map((h) => (
            <li key={h.id} className='grid grid-cols-[2.25rem_1fr] gap-3 py-2.5 text-paragraph-sm'>
              <span className='text-right tabular-nums text-text-soft-400'>{h.rank}</span>
              <span className={h.kind === 'belief' ? '' : 'text-text-sub-600'}>
                {h.kind === 'unknown' ? <Muted>No longer held by Lens</Muted> : h.text}
                {kind === 'all' && h.kind === 'fact' ? (
                  <span className='ml-2 text-paragraph-xs text-text-soft-400'>fact</span>
                ) : null}
                {h.since ? <span className='ml-2 text-paragraph-xs text-text-soft-400'>{h.since} since</span> : null}
              </span>
            </li>
          ))}
        </ol>
      ) : (
        <Muted>{r.results ? 'No belief was among the results.' : 'This recall returned nothing.'}</Muted>
      )}
      {pages > 1 ? (
        <div className='flex items-center justify-between text-paragraph-xs text-text-soft-400'>
          <span>
            {page * pageSize + 1} to {Math.min(rows.length, (page + 1) * pageSize)} of {rows.length}
          </span>
          <span className='flex gap-1'>
            <Button.Root
              variant='neutral'
              mode='ghost'
              size='xxsmall'
              disabled={!page}
              onClick={() => setPage(page - 1)}
              aria-label='Previous page'
            >
              <Button.Icon as={RiArrowLeftSLine} />
            </Button.Root>
            <Button.Root
              variant='neutral'
              mode='ghost'
              size='xxsmall'
              disabled={page >= pages - 1}
              onClick={() => setPage(page + 1)}
              aria-label='Next page'
            >
              <Button.Icon as={RiArrowRightSLine} />
            </Button.Root>
          </span>
        </div>
      ) : null}
    </div>
  );
}

// -- the frame ---------------------------------------------------------------------------------------

export function Shell({ users, asOf, children }: { users: Summary[]; asOf: string | null; children: React.ReactNode }) {
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = (e.target as HTMLElement)?.closest('input, textarea, [contenteditable]');
      if ((e.key === '/' && !typing) || (e.key === 'k' && (e.metaKey || e.ctrlKey))) {
        e.preventDefault();
        setOpen(true);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  return (
    <div className='min-h-dvh'>
      <header className='sticky top-0 z-10 border-b border-stroke-soft-200 bg-bg-white-0'>
        <div className='mx-auto flex h-14 max-w-6xl items-center gap-4 px-4 sm:px-6'>
          <a href={fleetHref()} className='shrink-0 text-label-md'>
            Lens
          </a>
          <button
            type='button'
            onClick={() => setOpen(true)}
            className='flex h-8 w-full min-w-0 max-w-xs items-center gap-2 rounded-10 px-2.5 text-paragraph-sm text-text-soft-400 ring-1 ring-inset ring-stroke-soft-200 hover:bg-bg-weak-50'
          >
            <RiSearchLine className='size-4 shrink-0' />
            <span className='truncate'>Find a user</span>
            <Kbd.Root className='ml-auto'>/</Kbd.Root>
          </button>
          {asOf ? (
            <span className='ml-auto hidden whitespace-nowrap text-paragraph-xs text-text-soft-400 sm:block'>
              Data as of {stamp(asOf)}
            </span>
          ) : null}
        </div>
      </header>
      <CommandMenu.Dialog open={open} onOpenChange={setOpen}>
        <CommandMenu.DialogTitle className='sr-only'>Find a user</CommandMenu.DialogTitle>
        <CommandMenu.Input placeholder='Name or bank id' />
        <CommandMenu.List>
          <CommandMenu.Group>
            {users.map((s) => (
              <CommandMenu.Item
                key={s.id}
                value={`${nameOf(s.id)} ${aboutOf(s.id) ?? ''} ${s.id}`}
                onSelect={() => {
                  setOpen(false);
                  go(userHref(s.id));
                }}
              >
                <span className='truncate'>{nameOf(s.id)}</span>
                <span className='ml-auto truncate text-paragraph-xs text-text-soft-400'>{aboutOf(s.id) ?? ''}</span>
              </CommandMenu.Item>
            ))}
          </CommandMenu.Group>
        </CommandMenu.List>
      </CommandMenu.Dialog>
      {children}
    </div>
  );
}

/** A grey bar shaped like the text it stands in for. */
export function Bone({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse rounded-10 bg-bg-weak-50 ${className}`} aria-hidden='true' />;
}

/** Something could not be shown: say what, and offer the way back. */
export function Notice({ title, detail }: { title: string; detail?: string }) {
  return (
    <main className='mx-auto max-w-[44rem] space-y-2 px-4 py-16 sm:px-6' role='status'>
      <p className='text-paragraph-md'>{title}</p>
      {detail ? <p className='text-paragraph-sm text-text-sub-600'>{detail}</p> : null}
      <a
        href={fleetHref()}
        className='inline-block pt-2 text-paragraph-sm text-text-sub-600 underline underline-offset-4'
      >
        Back to the fleet
      </a>
    </main>
  );
}
