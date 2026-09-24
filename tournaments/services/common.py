from django.db import transaction

from ..models import Activity, Tournament


class ServiceError(Exception):
    """A user-facing validation error."""


class NeedsConfirmation(Exception):
    def __init__(self, affected):
        super().__init__("confirmation required")
        self.affected = affected


def log(tournament, user, verb, text):
    Activity.objects.create(
        tournament=tournament,
        user=user if user and user.is_authenticated else None,
        verb=verb,
        text=text[:300],
    )


def apply_change(category, fn, *, confirm=False, user=None, verb="", text=""):
    """Run ``fn`` then re-derive the category (SPEC §1.3).

    If the change would clear other recorded results and ``confirm`` is False,
    everything is rolled back and the list of affected games is returned.
    Returns [] on success.
    """
    from .resolution import recompute_category

    tournament = category.tournament
    try:
        with transaction.atomic():
            fn()
            affected = recompute_category(category)
            if affected and not confirm:
                raise NeedsConfirmation(affected)
            touch(tournament)
            if verb:
                log(tournament, user, verb, text)
    except NeedsConfirmation as exc:
        return exc.affected
    return []


def touch(tournament):
    """Bump the version (live updates) and move published → in progress on first result."""
    from ..models import Game

    if tournament.status == Tournament.Status.PUBLISHED and Game.objects.filter(
        stage__category__tournament=tournament,
        status__in=[Game.Status.COMPLETED, Game.Status.FORFEIT],
    ).exists():
        Tournament.objects.filter(pk=tournament.pk).update(status=Tournament.Status.IN_PROGRESS)
        tournament.status = Tournament.Status.IN_PROGRESS
    tournament.bump()
