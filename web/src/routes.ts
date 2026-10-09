// The dashboard's addresses, all in the hash so a reload or a shared link reopens the
// same thing: #/ the fleet, #/u/<bank> a user, #/u/<bank>/<mode>/<id> one of their things.

export type Mode = 'belief' | 'session' | 'recall';
export const MODES: Mode[] = ['belief', 'session', 'recall'];

export type Route = { bank: string | null; mode: Mode; selected: string | null };

export function parse(hash: string): Route {
  const [, root, bank, mode, ...rest] = hash.replace(/^#/, '').split('/');
  if (root !== 'u' || !bank) return { bank: null, mode: 'belief', selected: null };
  return {
    bank: decodeURIComponent(bank),
    mode: MODES.includes(mode as Mode) ? (mode as Mode) : 'belief',
    selected: rest.length ? decodeURIComponent(rest.join('/')) : null,
  };
}

export const fleetHref = () => '#/';

export function userHref(bank: string, mode?: Mode, id?: string): string {
  const parts = ['#', 'u', encodeURIComponent(bank)];
  if (mode) parts.push(mode);
  if (mode && id) parts.push(encodeURIComponent(id));
  return parts.join('/');
}

export function go(hash: string) {
  window.location.hash = hash;
}

/** Change the address without adding a history entry (moving through a list). */
export function replace(hash: string) {
  window.history.replaceState(null, '', hash);
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}
