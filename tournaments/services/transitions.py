"""Advancing teams between stages and finishing a tournament (SPEC §9)."""
from django.db import transaction
from django.utils import timezone

from ..models import Tournament
from .common import ServiceError, log, touch
from .resolution import downstream_entries, qualifying_map, recompute_category, stage_complete, stage_status
from .standings import stage_games


def can_advance(stage):
    return stage_complete(stage) and not stage.advanced_at and downstream_entries(stage).exists()


def preview(stage):
    return qualifying_map(stage)


@transaction.atomic
def advance(stage, user=None):
    games = stage_games(stage)
    if not stage_complete(stage, games):
        raise ServiceError("All games in this stage must be completed, forfeited or cancelled first.")
    mapping = qualifying_map(stage, games)
    if not mapping:
        raise ServiceError("No later stage takes teams from this stage.")
    for e, team, _ in mapping:
        if not e.locked:
            e.team = team
            e.save(update_fields=["team"])
    stage.advanced_at = timezone.now()
    stage.save(update_fields=["advanced_at"])
    recompute_category(stage.category)
    t = stage.category.tournament
    touch(t)
    log(t, user, "advance", f"{stage.category.name}: teams advanced from {stage.name}")


def undo_advance(stage, user=None):
    """Empty the downstream slots again (only while the next stage has no results)."""
    from .common import apply_change

    def change():
        for e in downstream_entries(stage).filter(locked=False):
            e.team = None
            e.save(update_fields=["team"])
        stage.advanced_at = None
        stage.save(update_fields=["advanced_at"])

    return apply_change(stage.category, change, confirm=False, user=user, verb="advance",
                        text=f"{stage.category.name}: advancement from {stage.name} undone")


def all_complete(tournament):
    for cat in tournament.categories.all():
        stages = list(cat.stages.all())
        if not stages or any(stage_status(s) not in ("complete", "transitioned") for s in stages):
            return False
    return True


def finish(tournament, user=None):
    if not all_complete(tournament):
        raise ServiceError("Every stage of every category must be complete first.")
    tournament.status = Tournament.Status.COMPLETED
    tournament.completed_at = timezone.now()
    tournament.save(update_fields=["status", "completed_at"])
    touch(tournament)
    log(tournament, user, "finish", "Tournament finished")


def reopen(tournament, user=None):
    if tournament.status == Tournament.Status.COMPLETED:
        tournament.status = Tournament.Status.IN_PROGRESS
        tournament.completed_at = None
        tournament.save(update_fields=["status", "completed_at"])
        touch(tournament)
        log(tournament, user, "reopen", "Tournament reopened")
