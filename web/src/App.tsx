// Routes: #/ the fleet, #/u/<bank> a user, #/u/<bank>/<belief|session|reply>/<id> one thing
// of theirs. Everything a page shows comes from the guarded fleet and user endpoints.
import * as React from 'react';
import { loadFleet, loadUser, nameOf, useLoad } from './data';
import { FleetPage, FleetSkeleton } from './fleet';
import { Notice, Shell } from './shared';
import { type Mode, parse } from './routes';
import { UserPage, UserSkeleton } from './user';

function useHash() {
  const [hash, setHash] = React.useState(() => window.location.hash || '#/');
  React.useEffect(() => {
    const on = () => setHash(window.location.hash || '#/');
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return hash;
}

export function App() {
  const hash = useHash();
  const route = parse(hash);
  const fleet = useLoad(loadFleet, 'fleet');
  React.useEffect(() => {
    window.scrollTo(0, 0);
  }, [route.bank]);
  React.useEffect(() => {
    document.title = route.bank ? `${nameOf(route.bank)}, Hindsight Lens` : 'Hindsight Lens';
  }, [route.bank]);

  const users = fleet.state === 'ready' ? fleet.value.users : [];
  const asOf = fleet.state === 'ready' ? fleet.value.as_of : null;
  return (
    <Shell users={users} asOf={asOf}>
      {fleet.state === 'failed' ? (
        <Notice title='The fleet could not be loaded.' detail={fleet.error.message} />
      ) : route.bank ? (
        <UserRoute key={route.bank} bank={route.bank} asOf={asOf} mode={route.mode} selected={route.selected} />
      ) : fleet.state === 'ready' ? (
        <FleetPage fleet={fleet.value} />
      ) : (
        <FleetSkeleton />
      )}
    </Shell>
  );
}

function UserRoute({
  bank,
  asOf,
  mode,
  selected,
}: {
  bank: string;
  asOf: string | null;
  mode: Mode;
  selected: string | null;
}) {
  const user = useLoad(() => loadUser(bank), bank);
  if (user.state === 'failed')
    return user.error.status === 404 ? (
      <Notice
        title={`Lens holds no memory for ${bank}.`}
        detail='The bank may never have been synced, or Hindsight deleted it and Lens erased its copy.'
      />
    ) : (
      <Notice title={`${bank} could not be loaded.`} detail={user.error.message} />
    );
  if (user.state === 'loading' || !asOf) return <UserSkeleton />;
  return <UserPage u={user.value} asOf={asOf} mode={mode} selected={selected} />;
}
