"""One long task, from the message that started it to the reply that ends it.

Everything a lifecycle does is here, split by what it is doing rather than by
when: identity, accuracy reviews, the card, its workers, the final reply, the
timers that keep its lease alive, and how it is written down. They are mixins
rather than collaborators because they share one lock and one entry, and
separating them into objects would mean passing that lock around.
"""
from __future__ import annotations


from interface.long_tasks.accuracy import AccuracyReviewMixin
from interface.long_tasks.base import LifecycleBase
from interface.long_tasks.cards import CardMixin
from interface.long_tasks.persistence import LifecyclePersistenceMixin
from interface.long_tasks.reply import FinalReplyMixin
from interface.long_tasks.timers import CadenceMixin
from interface.long_tasks.workers import WorkerDeliveryMixin


class LongTaskLifecycle(
    AccuracyReviewMixin,
    CardMixin,
    WorkerDeliveryMixin,
    FinalReplyMixin,
    CadenceMixin,
    LifecyclePersistenceMixin,
    LifecycleBase,
):
    """One long task, assembled from the seven things it does."""
