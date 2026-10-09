// Where the dashboard's data comes from. Served by Lens: the fleet summary on start, each
// user's detail when first opened, kept for the page's lifetime. Offline demo: a snapshot
// of every user embedded in the page, plus the simulated personas; no requests at all.
import * as React from 'react';
import type { Fleet, Persona, User } from './lens';

type Embedded = { snapshot: Fleet & { users: User[] }; sim?: Record<string, Persona> };

export class LoadError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}

function embedded(): Embedded | null {
  const raw = document.getElementById('lens-data')?.textContent?.trim();
  return raw && raw.startsWith('{') ? (JSON.parse(raw) as Embedded) : null;
}

const offline = embedded();
export const personas: Record<string, Persona> = offline?.sim ?? {};

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  const body = await r.json().catch(() => ({}));
  if (!r.ok)
    throw new LoadError(typeof body.detail === 'string' ? body.detail : `the server answered ${r.status}`, r.status);
  return body as T;
}

export function loadFleet(): Promise<Fleet> {
  if (offline) return Promise.resolve(offline.snapshot);
  return getJSON<Fleet>('api/fleet');
}

const users = new Map<string, Promise<User>>();
for (const u of offline?.snapshot.users ?? []) users.set(u.id, Promise.resolve(u));

/** A user's detail; one request per user for the page's lifetime. A failure is not kept. */
export function loadUser(id: string): Promise<User> {
  let p = users.get(id);
  if (!p) {
    p = offline
      ? Promise.reject(new LoadError(`This page holds no user called ${id}.`, 404))
      : getJSON<{ user: User }>(`api/users/${encodeURIComponent(id)}`).then((d) => d.user);
    users.set(id, p);
    p.catch(() => users.delete(id));
  }
  return p;
}

export type Loaded<T> = { state: 'loading' } | { state: 'ready'; value: T } | { state: 'failed'; error: LoadError };

export function useLoad<T>(load: () => Promise<T>, key: string): Loaded<T> {
  const [result, setResult] = React.useState<Loaded<T>>({ state: 'loading' });
  React.useEffect(() => {
    let live = true;
    setResult({ state: 'loading' });
    load().then(
      (value) => live && setResult({ state: 'ready', value }),
      (error) =>
        live &&
        setResult({ state: 'failed', error: error instanceof LoadError ? error : new LoadError(String(error)) }),
    );
    return () => {
      live = false;
    };
  }, [key]); // reloads only when the key changes: `load` is a fresh closure every render
  return result;
}

/** A display name: the simulated person's in the demo, the bank id otherwise. */
export const nameOf = (id: string) => personas[id]?.profile.full_name ?? id;
export const aboutOf = (id: string) => personas[id]?.profile.about ?? null;
/** The person's own name, so it is not offered as "what a belief is about". */
export const ownName = (id: string) => [
  id.toLowerCase(),
  ...id
    .toLowerCase()
    .split(/[^\p{L}]+/u)
    .filter((w) => w.length > 2),
  ...(personas[id]?.profile.full_name.toLowerCase().split(' ') ?? []),
];
