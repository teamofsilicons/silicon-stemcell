"""The final reply, queued durably and flushed once the card is terminal.

This existed to stop a Carbon reading "all done" before the durable card agreed
it was done: the closing message was held here until the task went terminal. Its
only producer was the `reply` manager tool, then `iwantto send --final`, and both
are gone — finishing a work is `iwantto work --completed`, which settles the card
and says so itself, so there is no longer an ordering to enforce between a
message and a card.

ponytail: nothing queues a final reply any more, so `pending_reply` is always
empty on a fresh instance and this whole mixin is inert. It is kept because an
instance updated mid-flight can still have one persisted in
`long_task_state.json`, and the timer and recovery paths that read it are what
deliver that last message instead of stranding it. Delete once no deployed
instance can still hold one.
"""
from __future__ import annotations
from interface.long_tasks import constants
from interface.long_tasks import registry as registry_module
from interface.long_tasks import util as util_module
import time
from copy import deepcopy
from typing import Callable


class FinalReplyMixin:
    def queue_final_reply(self, message: str) -> str:
        """Persist prose and its stable id before any terminal network write."""
        text = str(message or "")
        if len(text) > constants.MAX_PENDING_REPLY_CHARS:
            text = text[:constants.MAX_PENDING_REPLY_CHARS]
        with self._lock:
            if self.pending_reply:
                return str(self.pending_reply.get("client_id") or "")
            client_id = util_module._stable_id(
                "final-reply", self.contact_id, self.run_id, text
            )
            self.pending_reply = {
                "message": text,
                "client_id": client_id,
                "attempts": 0,
                "next_attempt_at": 0.0,
                "created_at": time.time(),
            }
            self._settle_requested = bool(self.task_id)
            self._persist(active=True)
            return client_id


    def deliver_final_reply(
        self,
        message: str,
        *,
        has_active_workers: bool,
        reply_sender: Callable[..., str] | None = None,
    ) -> str:
        self.queue_final_reply(message)
        return self._flush_final_reply(
            has_active_workers=has_active_workers,
            reply_sender=reply_sender,
            force=True,
        )


    def _flush_final_reply(
        self,
        *,
        has_active_workers: bool | None = None,
        reply_sender: Callable[..., str] | None = None,
        force: bool = False,
    ) -> str:
        with self._reply_lock:
            return self._flush_final_reply_locked(
                has_active_workers=has_active_workers,
                reply_sender=reply_sender,
                force=force,
            )


    def _flush_final_reply_locked(
        self,
        *,
        has_active_workers: bool | None,
        reply_sender: Callable[..., str] | None,
        force: bool,
    ) -> str:
        with self._lock:
            pending = deepcopy(self.pending_reply)
            if not pending:
                return "Message sent" if self._final_reply_sent else "No reply queued"
            now = time.time()
            if not force and now < float(pending.get("next_attempt_at") or 0):
                return "Message queued for durable delivery"

        self._reconcile_worker_intents()
        self._deliver_pending_workers(force=force)
        if has_active_workers is None:
            has_active_workers = self._has_active_workers_now()

        with self._lock:
            task_required = bool(
                self.task_id
                or self._pending_create_spec
                or self._create_attempts
            )
            cannot_settle = (
                bool(has_active_workers)
                or bool(self.pending_workers)
                or self._deferred
            )
            if task_required and (
                cannot_settle or not self.task_confirmed
            ):
                self._settle_requested = True
                self._persist(active=True)
                return "Message queued behind the durable work update"

        if task_required and not self._terminal:
            if not self._settle_task():
                return "Message queued behind the durable work update"

        sender = reply_sender or self.reply_sender
        if sender is None:
            return "Message queued for durable delivery"
        with self._lock:
            pending = deepcopy(self.pending_reply)
            if not pending:
                return "Message sent"
        try:
            status = sender(
                str(pending.get("message") or ""),
                self.contact_id,
                work_continues=False,
                client_id=str(pending.get("client_id") or ""),
            )
        except Exception:
            status = "Message delivery failed"
        with self._lock:
            current = self.pending_reply
            if (
                current
                and current.get("client_id") == pending.get("client_id")
                and status == "Message sent"
            ):
                self.pending_reply = {}
                self._final_reply_sent = True
                self._close_locked()
                should_unregister = True
            else:
                attempts = int(current.get("attempts") or 0) + 1
                terminal = (
                    util_module._terminal_reply_delivery_status(status)
                    or attempts >= constants.MAX_PENDING_REPLY_ATTEMPTS
                )
                if terminal:
                    self.pending_reply = {}
                    self._close_locked()
                    should_unregister = True
                else:
                    current["attempts"] = attempts
                    current["next_attempt_at"] = util_module._retry_at(attempts)
                    self._persist(active=True)
                    should_unregister = False
        if should_unregister:
            registry_module._unregister(self)
        if status == "Message sent":
            return status
        if terminal:
            print(
                "[Long task] final reply delivery abandoned after a "
                "non-retryable or exhausted failure",
                flush=True,
            )
            return "Message delivery abandoned"
        return "Message queued for durable delivery"


    def terminalize_before_reply(self, *, has_active_workers: bool) -> bool:
        """Compatibility helper: settle the card, but never bypass the barrier."""
        self._reconcile_worker_intents()
        self._deliver_pending_workers(force=True)
        with self._lock:
            if (
                has_active_workers
                or self.pending_workers
                or self._deferred
                or not self.task_id
                or not self.task_confirmed
            ):
                self._settle_requested = bool(self.task_id)
                self._persist(active=True)
                return False
            if self._terminal:
                return True
            self._settle_requested = True
            self._persist(active=True)
        return self._settle_task(close_terminal=False)


    def replay_pending_once(self, *, recovery: bool = False) -> None:
        """Replay every durable phase without requiring a new inbound message."""
        if not self.is_open:
            return
        self._reconcile_worker_intents(force_prepared=recovery)
        with self._lock:
            create_due = bool(
                self._pending_create_spec
                and not self.task_confirmed
            )
        if create_due:
            self.ensure("")
        self._deliver_pending_workers(force=True)
        self._reconcile_timer(force=True)
        with self._lock:
            settle_due = self._settle_requested and not self.pending_reply
            reply_due = bool(self.pending_reply)
        if settle_due:
            self._settle_task()
        if reply_due:
            self._flush_final_reply(force=True)
        self._prepare_accuracy_review_if_due()
        self.close_if_terminal()
