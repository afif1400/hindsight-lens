"""The background half of the collector: one queue, one worker, one sweep timer.

Webhook deliveries only enqueue a bank and return, so Hindsight never waits on a
sync. A single worker drains the queue, which keeps syncs for one bank strictly
serial. The sweep re-enqueues every bank on a timer: it is the safety net for a
dropped ping (Hindsight gives up after six attempts) and what pulls change
reasons before Hindsight's one-day trace window closes.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from .hindsight import HindsightClient, HindsightError
from .store import Store
from .sync import BankSyncer, SyncResult
from .timeutil import utcnow

logger = logging.getLogger("lens.worker")


class Collector:
    def __init__(
        self, client: HindsightClient, store: Store, syncer: BankSyncer, *, sweep_interval_seconds: int
    ) -> None:
        self.client = client
        self.store = store
        self.syncer = syncer
        self.sweep_interval_seconds = sweep_interval_seconds
        self._pending: OrderedDict[str, bool] = OrderedDict()  # bank_id -> reconcile?
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._busy = False
        self._threads: list[threading.Thread] = []
        self.last_sweep_at = None
        self.last_sweep_error: str | None = None
        self.last_sweep_warnings: list[str] = []

    # -- queue --------------------------------------------------------------

    def enqueue(self, bank_id: str, *, reconcile: bool = False) -> None:
        """Ask for a sync. Requests for a bank already waiting are merged."""
        with self._cond:
            self._pending[bank_id] = self._pending.get(bank_id, False) or reconcile
            self._cond.notify()

    def queue_depth(self) -> int:
        with self._cond:
            return len(self._pending) + int(self._busy)

    def wait_until_idle(self, timeout: float = 30.0) -> bool:
        """Block until nothing is queued or running. For tests and the CLI."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._pending and not self._busy, timeout=timeout)

    # -- lifecycle ----------------------------------------------------------

    def start(self, *, sweep_on_start: bool = True) -> None:
        self._threads = [threading.Thread(target=self._work, name="lens-worker", daemon=True)]
        if self.sweep_interval_seconds > 0:
            self._threads.append(
                threading.Thread(target=self._sweep_loop, args=(sweep_on_start,), name="lens-sweep", daemon=True)
            )
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for thread in self._threads:
            thread.join(timeout=10)

    # -- loops --------------------------------------------------------------

    def _work(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait_for(lambda: self._pending or self._stop.is_set())
                if self._stop.is_set():
                    return
                bank_id, reconcile = self._pending.popitem(last=False)
                self._busy = True
            try:
                self.sync_now(bank_id, reconcile=reconcile)
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def sync_now(self, bank_id: str, *, reconcile: bool = False) -> SyncResult | None:
        try:
            result = self.syncer.sync_bank(bank_id, reconcile=reconcile)
            logger.info("synced %s: %s", bank_id, result.as_dict())
            return result
        except Exception as exc:  # one bad bank must not stop the worker
            logger.exception("sync of %s failed", bank_id)
            self.store.touch_bank(bank_id, last_error=f"sync failed: {exc}")
            return None

    def sweep(self) -> int:
        """Enqueue every bank Hindsight has, and erase the ones it no longer has. Returns how many it enqueued."""
        self.last_sweep_warnings = []
        try:
            bank_ids = [b["bank_id"] for b in self.client.list_banks() if b.get("bank_id")]
        except HindsightError as exc:
            self.last_sweep_error = str(exc)
            logger.warning("sweep could not list banks, so erased nothing: %s", exc)
            return 0
        self.last_sweep_error = None
        self.last_sweep_at = utcnow()
        for bank_id in bank_ids:
            self.enqueue(bank_id)
        self._erase_gone(set(bank_ids))
        return len(bank_ids)

    def _erase_gone(self, listed: set[str]) -> None:
        """Erase banks Lens holds that a complete listing lacks, once each answers "not found" on its own."""
        try:
            held = {b["bank_id"] for b in self.store.list_banks()}
        except Exception as exc:  # a store error must not stop the sweep thread
            self._warn(f"could not read the banks Lens holds, so erased nothing: {type(exc).__name__}")
            return
        missing = sorted(held - listed)
        # A fresh or different Hindsight answers "not found" for everyone. Losing most of a
        # fleet at once is not a deletion to act on unattended; `lens forget` still can.
        if missing and (not listed or len(missing) > max(1, len(held) // 2)):
            self._warn(f"{len(missing)} of {len(held)} banks are gone from Hindsight at once, so erased none")
            return
        for bank_id in missing:
            try:
                gone = not self.client.bank_exists(bank_id)
            except HindsightError as exc:
                self._warn(f"could not confirm bank {bank_id!r} is gone, so kept it: {exc}")
                continue
            if not gone:
                self._warn(f"bank {bank_id!r} is missing from the listing but Hindsight still has it; kept")
                continue
            try:
                counts = self.store.erase_bank(bank_id, actor="system", trigger="sweep: Hindsight no longer has it")
            except Exception as exc:  # e.g. the access row could not be written: the transaction rolled back
                self._warn(f"could not erase bank {bank_id!r}, so kept it: {type(exc).__name__}")
                continue
            logger.info("erased bank %r, which Hindsight no longer has: %s", bank_id, counts)

    def _warn(self, message: str) -> None:
        self.last_sweep_warnings.append(message)
        logger.warning("sweep: %s", message)

    def _sweep_loop(self, sweep_on_start: bool) -> None:
        if sweep_on_start:
            self.sweep()
        while not self._stop.wait(self.sweep_interval_seconds):
            self.sweep()
