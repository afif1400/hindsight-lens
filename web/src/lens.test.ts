import { describe, expect, it } from 'vitest';
import {
  type Belief,
  type User,
  buildSessions,
  entitiesOf,
  hitRows,
  isRewrite,
  needsLook,
  nearDuplicates,
  plainText,
  redline,
  topEntities,
  weekSentence,
} from './lens';

function belief(id: string, text: string, extra: Partial<Belief> = {}): Belief {
  return {
    id,
    text,
    status: 'live',
    flags: [],
    evidence_count: 1,
    created_at: '2026-09-01T10:00:00Z',
    last_changed_at: '2026-09-01T10:00:00Z',
    deleted_at: null,
    newest_evidence_at: null,
    versions: [
      {
        n: 1,
        type: 'created',
        text,
        at: '2026-09-01T10:00:00Z',
        at_is_estimate: false,
        reason: null,
        new_evidence: [],
        triggered_by: [],
      },
    ],
    evidence: [],
    recalls: [],
    ...extra,
  };
}

function user(beliefs: Belief[], extra: Partial<User> = {}): User {
  return {
    id: 'maya',
    flags: [],
    counts: {},
    first_activity_at: null,
    last_activity_at: null,
    last_recall_at: null,
    last_synced_at: null,
    portrait: null,
    beliefs,
    facts: {},
    chunks: {},
    recalls: [],
    ...extra,
  };
}

describe('redline', () => {
  it('marks the words removed and added', () => {
    expect(redline('dinner at 9pm', 'dinner at 8pm')).toEqual([
      { op: 'same', text: 'dinner at ' },
      { op: 'del', text: '9pm' },
      { op: 'add', text: '8pm' },
    ]);
  });

  it('calls a change of most words a rewrite, and a small edit not', () => {
    expect(isRewrite('Maya walks 20 minutes after dinner', 'Maya walks 25 minutes after dinner')).toBe(false);
    expect(isRewrite('Maya walks after dinner', 'Sam cooks pasta on Sundays for the family')).toBe(true);
  });
});

describe('near-duplicates', () => {
  it('folds beliefs that say nearly the same thing under the best supported one', () => {
    const a = belief('a', 'Maya long runs this week were 12, 14 and 16 km', { evidence_count: 3 });
    const b = belief('b', 'Maya long runs this week were 12, 14, 16 and 18 km');
    const c = belief('c', 'Sam packs kadala and egg for lunch on closing days');
    const groups = nearDuplicates(user([b, a, c]));
    expect(groups.map((g) => g.map((x) => x.id))).toEqual([['a', 'b'], ['c']]);
  });

  it('does not call short beliefs alike because they share a name, in any script', () => {
    const a = belief('a', 'Abdul और Abbu ने कल डॉक्टर के पास गए।');
    const b = belief('b', 'Abdul और Abbu ने इंसुलिन लिया।');
    expect(nearDuplicates(user([a, b]))).toHaveLength(2);
  });
});

describe('entities', () => {
  const u = user([], {
    facts: {
      f1: {
        text: '',
        type: 'world',
        document: 'd1',
        chunk: null,
        at: null,
        missing: false,
        entities: ['Maya', 'half marathon', 'August'],
      },
      f2: {
        text: '',
        type: 'world',
        document: 'd1',
        chunk: null,
        at: null,
        missing: false,
        entities: ['walk', 'half marathon', 'user'],
      },
      f3: { text: '', type: 'world', document: 'd2', chunk: null, at: null, missing: false, entities: ['walk'] },
    },
  });

  it('says what a belief is about from its sources, most mentioned first, without the noise', () => {
    const b = belief('a', 'x', { evidence: ['f1', 'f2'] });
    expect(entitiesOf(u, [b], ['maya'])).toEqual(['half marathon', 'walk']);
  });

  it('drops an entity named in most facts: it is the person, and filters nothing', () => {
    const v = user([], {
      facts: Object.fromEntries(
        'abcdefghij'.split('').map((id, i) => [
          id,
          {
            text: '',
            type: 'world',
            document: 'd',
            chunk: null,
            at: null,
            missing: false,
            entities: i ? ['Gurpreet'] : ['Gurpreet', 'chai'],
          },
        ]),
      ),
    });
    expect(entitiesOf(v, [belief('x', 'x', { evidence: ['a'] })])).toEqual(['chai']);
  });

  it('offers as filters only entities more than one belief mentions', () => {
    const groups = [
      [belief('a', 'x', { evidence: ['f1'] })],
      [belief('b', 'y', { evidence: ['f2'] })],
      [belief('c', 'z', { evidence: ['f3'] })],
    ];
    expect(topEntities(u, groups)).toEqual([
      { name: 'half marathon', count: 2 },
      { name: 'walk', count: 2 },
    ]);
  });
});

describe('a recall in full', () => {
  it('names every result: a belief, a fact, or one Lens never collected', () => {
    const u = user([belief('b1', 'Maya walks after dinner')], {
      facts: {
        f1: {
          text: 'Maya walked 30 minutes | Involving: Sam',
          type: 'world',
          document: 'd1',
          chunk: null,
          at: null,
          missing: false,
          entities: [],
        },
      },
    });
    const rows = hitRows(u, {
      id: 'r1',
      at: '2026-09-02T00:00:00Z',
      query: 'q',
      results: 3,
      transport: 'http',
      hits: [
        { id: 'b1', rank: 1, score: 0.3, type: 'observation' },
        { id: 'f1', rank: 2, score: 0.2, type: 'world' },
        { id: 'zz', rank: 3, score: 0.1, type: 'world' },
      ],
    });
    expect(rows.map((r) => [r.rank, r.kind, r.isBelief, r.text])).toEqual([
      [1, 'belief', true, 'Maya walks after dinner'],
      [2, 'fact', false, 'Maya walked 30 minutes'],
      [3, 'unknown', false, null],
    ]);
  });
});

describe('a recall, as it was', () => {
  it('words each belief as it stood at the recall, and says what happened since', () => {
    const v = (n: number, type: string, text: string, at: string) => ({
      n,
      type,
      text,
      at,
      at_is_estimate: false,
      reason: null,
      new_evidence: [],
      triggered_by: [],
    });
    const rewritten = belief('b1', 'dinner at 8pm', {
      versions: [
        v(1, 'created', 'dinner at 9pm', '2026-09-01T10:00:00Z'),
        v(2, 'updated', 'dinner at 8pm', '2026-09-10T10:00:00Z'),
      ],
    });
    const gone = belief('b2', 'walks daily', {
      status: 'deleted',
      versions: [
        v(1, 'created', 'walks daily', '2026-09-01T10:00:00Z'),
        v(2, 'deleted', 'walks daily', '2026-09-12T10:00:00Z'),
      ],
    });
    const same = belief('b3', 'drinks decaf');
    const recall = {
      id: 'r',
      at: '2026-09-05T00:00:00Z',
      query: 'q',
      results: 3,
      transport: null,
      hits: ['b1', 'b2', 'b3'].map((id, i) => ({ id, rank: i + 1, score: null, type: 'observation' })),
    };

    expect(hitRows(user([rewritten, gone, same]), recall).map((r) => [r.text, r.since])).toEqual([
      ['dinner at 9pm', 'rewritten'],
      ['walks daily', 'forgotten'],
      ['drinks decaf', null],
    ]);
  });
});

describe('sessions', () => {
  it('orders by time, not by how the timestamp is written', () => {
    const facts = {
      f1: {
        text: 'a',
        type: 'world',
        document: 'u:s01',
        chunk: null,
        at: '2026-09-01T09:00:00+05:30',
        missing: false,
        entities: [],
      },
      f2: {
        text: 'b',
        type: 'world',
        document: 'u:s02',
        chunk: null,
        at: '2026-09-01T05:00:00+00:00',
        missing: false,
        entities: [],
      },
    };
    // s01 began 03:30 UTC and s02 at 05:00 UTC; a recall at 04:00 UTC belongs to s01.
    const recall = { id: 'r1', at: '2026-09-01T04:00:00+00:00', query: 'q', results: 0, transport: null, hits: [] };
    const sessions = buildSessions(user([], { facts, recalls: [recall] }));
    expect(sessions.map((s) => [s.id, s.replies.map((r) => r.id)])).toEqual([
      ['s02', []],
      ['s01', ['r1']],
    ]);
  });

  it('groups each change under the conversation its facts came from, and each reply under the latest before it', () => {
    const facts = {
      f1: {
        text: 'a',
        type: 'world',
        document: 'maya:s01',
        chunk: 'c1',
        at: '2026-09-01T09:00:00Z',
        missing: false,
        entities: [],
      },
      f2: {
        text: 'b',
        type: 'world',
        document: 'maya:s02',
        chunk: 'c2',
        at: '2026-09-05T09:00:00Z',
        missing: false,
        entities: [],
      },
    };
    const b1 = belief('b1', 'one', {
      versions: [
        {
          n: 1,
          type: 'created',
          text: 'one',
          at: '2026-09-01T10:00:00Z',
          at_is_estimate: false,
          reason: null,
          new_evidence: ['f1'],
          triggered_by: [],
        },
      ],
    });
    const b2 = belief('b2', 'two', {
      versions: [
        {
          n: 1,
          type: 'created',
          text: 'two',
          at: '2026-09-05T10:00:00Z',
          at_is_estimate: false,
          reason: null,
          new_evidence: ['f2'],
          triggered_by: [],
        },
      ],
    });
    const recall = { id: 'r1', at: '2026-09-03T00:00:00Z', query: 'q', results: 0, transport: 'http', hits: [] };
    const sessions = buildSessions(
      user([b1, b2], { facts, chunks: { c1: 'said one', c2: 'said two' }, recalls: [recall] }),
    );

    expect(sessions.map((s) => [s.id, s.changes.map((c) => c.b.id), s.replies.map((r) => r.id), s.fragments])).toEqual([
      ['s02', ['b2'], [], ['said two']],
      ['s01', ['b1'], ['r1'], ['said one']],
    ]);
  });
});

describe('words on screen', () => {
  it('reads a markdown portrait as plain sentences', () => {
    expect(plainText('## Profile\n**Name:** Maya\n- **Age**: 52')).toBe('Name: Maya Age: 52');
  });

  it('says what memory did this week', () => {
    const asOf = '2026-10-06T18:00:00Z';
    const fresh = belief('a', 'x', { created_at: '2026-10-05T10:00:00Z' });
    const rewritten = belief('b', 'y', {
      versions: [
        {
          n: 1,
          type: 'created',
          text: 'y0',
          at: '2026-09-01T10:00:00Z',
          at_is_estimate: false,
          reason: null,
          new_evidence: [],
          triggered_by: [],
        },
        {
          n: 2,
          type: 'updated',
          text: 'y',
          at: '2026-10-04T10:00:00Z',
          at_is_estimate: false,
          reason: null,
          new_evidence: [],
          triggered_by: [],
        },
      ],
    });
    expect(weekSentence(user([fresh, rewritten]), asOf)).toBe('This week memory learned 1 belief, rewrote 1.');
    expect(weekSentence(user([]), asOf)).toBe('Nothing changed this week.');
  });
});

describe('needsLook', () => {
  it('does not ask for a look at one-source beliefs: most Hindsight beliefs have one', () => {
    expect(needsLook(['new', 'changed', 'thin'])).toBe(false);
    expect(needsLook(['thin', 'stale'])).toBe(true);
  });
});
