import * as React from 'react';
import * as TabMenu from '@/components/ui/tab-menu-horizontal';
import * as Table from '@/components/ui/table';
import { aboutOf, nameOf } from './data';
import { type Fleet, day, needsLook, stamp } from './lens';
import { go, userHref } from './routes';
import { Bone, Flags } from './shared';

const HEALTH: [key: string, label: string, unit: string][] = [
  ['users', 'Users with memory', ''],
  ['beliefs_per_user', 'Beliefs per user', ''],
  ['thin_pct', 'Beliefs with one source', '%'],
  ['stale_pct', 'Stale beliefs', '%'],
  ['churn_per_user', 'Weekly changes per user', ''],
  ['empty_recall_pct', 'Recalls that returned nothing', '%'],
];

const num = (v: number | string | null | undefined) =>
  typeof v === 'number' ? String(Math.round(v * 10) / 10) : 'none yet';

export function FleetPage({ fleet }: { fleet: Fleet }) {
  const [tab, setTab] = React.useState('look');
  const look = fleet.users.filter((s) => needsLook(s.flags));
  const rows = tab === 'all' ? fleet.users : look;
  const now = fleet.fleet.now;
  const before = fleet.fleet.series[fleet.fleet.series.length - 2] ?? {};
  const c = fleet.collector;
  return (
    <main className='mx-auto grid max-w-6xl gap-10 px-4 py-8 sm:px-6 lg:grid-cols-[1fr_18rem]'>
      <div className='min-w-0 space-y-6'>
        <div className='space-y-1'>
          <h1 className='text-title-h5'>Fleet</h1>
          <p className='text-paragraph-sm text-text-sub-600'>
            {fleet.users.length} {fleet.users.length === 1 ? 'user' : 'users'}, {num(now.live_beliefs)} live beliefs.
          </p>
        </div>
        <TabMenu.Root value={tab} onValueChange={setTab}>
          <TabMenu.List>
            <TabMenu.Trigger value='look'>Needs a look {look.length}</TabMenu.Trigger>
            <TabMenu.Trigger value='all'>All {fleet.users.length}</TabMenu.Trigger>
          </TabMenu.List>
        </TabMenu.Root>
        {rows.length ? (
          <Table.Root>
            <Table.Header>
              <Table.Row>
                <Table.Head>User</Table.Head>
                <Table.Head className='hidden md:table-cell'>This week</Table.Head>
                <Table.Head className='text-right'>Beliefs</Table.Head>
                <Table.Head className='hidden text-right sm:table-cell'>Last active</Table.Head>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {rows.map((s) => (
                <Table.Row key={s.id} className='cursor-pointer' onClick={() => go(userHref(s.id))}>
                  <Table.Cell className='py-3.5'>
                    <a href={userHref(s.id)} className='text-label-sm' onClick={(e) => e.stopPropagation()}>
                      {nameOf(s.id)}
                    </a>
                    {aboutOf(s.id) ? <div className='text-paragraph-xs text-text-soft-400'>{aboutOf(s.id)}</div> : null}
                    <div className='mt-1'>
                      <Flags flags={s.flags} only='attention' />
                    </div>
                  </Table.Cell>
                  <Table.Cell className='hidden text-paragraph-sm text-text-sub-600 md:table-cell'>
                    {s.counts.changed_recent || s.counts.deleted_recent
                      ? `${s.counts.changed_recent} rewritten, ${s.counts.deleted_recent} forgotten`
                      : 'No change'}
                  </Table.Cell>
                  <Table.Cell className='text-right tabular-nums'>{s.counts.live}</Table.Cell>
                  <Table.Cell className='hidden whitespace-nowrap text-right text-paragraph-sm text-text-sub-600 sm:table-cell'>
                    {day(s.last_activity_at)}
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table.Root>
        ) : (
          <p className='text-paragraph-sm text-text-sub-600'>
            {fleet.users.length
              ? 'Nobody needs a look right now.'
              : 'Lens holds no memory yet. It fills in after Hindsight’s first consolidation ping or sweep.'}
          </p>
        )}
      </div>

      <aside className='space-y-8'>
        <Stats title='Fleet this week'>
          {HEALTH.map(([key, label, unit]) => {
            const v = now[key];
            const prev = before[key];
            const delta = typeof v === 'number' && typeof prev === 'number' ? Math.round((v - prev) * 10) / 10 : null;
            return (
              <Stat key={key} label={label}>
                {num(v)}
                {typeof v === 'number' ? unit : ''}
                {delta ? (
                  <span className='ml-1.5 text-text-soft-400'>{delta > 0 ? `+${delta}` : `${delta}`} on last week</span>
                ) : null}
              </Stat>
            );
          })}
        </Stats>
        <Stats title='Collector'>
          <Stat label='Last ping'>{stamp(c.last_ping_at)}</Stat>
          <Stat label='Reasons harvested up to'>{stamp(c.oldest_reasons_check_at)}</Stat>
          <Stat label='Changes without a reason'>
            {num(c.versions_without_reason)} of {num(c.versions)}
          </Stat>
        </Stats>
      </aside>
    </main>
  );
}

function Stats({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className='space-y-3'>
      <h2 className='text-label-sm'>{title}</h2>
      <dl className='space-y-2.5'>{children}</dl>
    </section>
  );
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className='text-paragraph-xs text-text-soft-400'>{label}</dt>
      <dd className='text-paragraph-sm tabular-nums'>{children}</dd>
    </div>
  );
}

export function FleetSkeleton() {
  return (
    <main className='mx-auto max-w-6xl space-y-6 px-4 py-8 sm:px-6' aria-busy='true'>
      <Bone className='h-7 w-32' />
      <Bone className='h-4 w-56' />
      <div className='space-y-4 pt-6'>
        {Array.from({ length: 6 }, (_, i) => (
          <div key={i} className='flex items-center gap-6'>
            <div className='flex-1 space-y-2'>
              <Bone className='h-4 w-48' />
              <Bone className='h-3 w-72 max-w-full' />
            </div>
            <Bone className='h-4 w-10' />
          </div>
        ))}
      </div>
    </main>
  );
}
