"""Setup checklist, publish validation and publishing (SPEC §5, §12)."""
from django.urls import reverse
from django.utils import timezone

from ..models import Entry, Game, Space, Tournament
from .common import ServiceError, log, touch
from .scheduling import find_conflicts, is_playable, tournament_games


def ensure_paid(tournament):
    """Billing integration point (SPEC §12). No-op in this build."""
    return True


def _errors(tournament):
    errors = []
    cats = list(tournament.categories.prefetch_related("teams", "stages"))
    if not cats:
        errors.append("Add at least one category.")
    for c in cats:
        n = c.teams.count()
        if n < 2:
            errors.append(f"{c.name}: add at least two teams.")
        if not c.stages.exists():
            errors.append(f"{c.name}: choose a format.")
            continue
        first = c.stages.first()
        for e in Entry.objects.filter(pool__stage__category=c).select_related("pool__stage", "pool"):
            if e.pool.stage_id == first.id:
                if not e.team_id:
                    errors.append(f"{c.name}: {e.pool.name} slot {e.position} has no team.")
            elif not e.team_id and not e.has_source:
                errors.append(f"{c.name} · {e.pool.stage.name}: {e.pool.name} slot {e.position} has no team or source.")
            elif e.source_pool_id and e.source_rank > e.source_pool.entries.count():
                errors.append(f"{c.name} · {e.pool.stage.name}: {e.source_label} doesn't exist.")
        placed = set(Entry.objects.filter(pool__stage=first).values_list("team_id", flat=True))
        missing = [t.name for t in c.teams.all() if t.id not in placed]
        if missing:
            errors.append(f"{c.name}: not placed in {first.name}: {', '.join(missing[:5])}"
                          + ("…" if len(missing) > 5 else ""))
    return errors


def validate(tournament):
    errors = _errors(tournament)
    warnings = []
    games = tournament_games(tournament)
    if tournament.schedule_mode == Tournament.ScheduleMode.TIMED:
        unscheduled = [g for g in games if is_playable(g) and not g.start_at]
        if unscheduled:
            warnings.append(f"{len(unscheduled)} game(s) have no time or {tournament.space_noun.lower()} yet.")
        conflicts = find_conflicts(tournament, games)
        if conflicts:
            warnings.append(f"{len(conflicts)} game(s) have schedule conflicts.")
    elif not Space.objects.filter(venue__tournament=tournament).exists():
        warnings.append(f"Add {tournament.space_noun.lower()}s to use the queue board.")
    if not tournament.blocks.filter(enabled=True).exists():
        warnings.append("The public home page has no content blocks.")
    return errors, warnings


def checklist(tournament):
    slug = tournament.slug
    cats = list(tournament.categories.all())
    games = tournament_games(tournament)
    errors = _errors(tournament)
    has_teams = bool(cats) and all(c.teams.count() >= 2 for c in cats)
    has_format = bool(cats) and all(c.stages.exists() for c in cats)
    slots_ok = has_format and not any("slot" in e or "not placed" in e or "doesn't exist" in e for e in errors)
    spaces = Space.objects.filter(venue__tournament=tournament).count()
    if tournament.schedule_mode == Tournament.ScheduleMode.TIMED:
        playable = [g for g in games if is_playable(g)]
        sched_ok = bool(playable) and all(g.start_at for g in playable) and not find_conflicts(tournament, games)
        sched_detail = f"{sum(1 for g in playable if g.start_at)}/{len(playable)} games scheduled"
    else:
        sched_ok, sched_detail = spaces > 0, "Queue mode"
    return [
        {"label": "Tournament information", "done": True, "url": reverse("m_settings", args=[slug])},
        {"label": "Categories", "done": bool(cats), "url": reverse("m_categories", args=[slug]),
         "detail": f"{len(cats)} categor{'y' if len(cats) == 1 else 'ies'}"},
        {"label": "Teams", "done": has_teams, "url": reverse("m_teams", args=[slug]),
         "detail": f"{sum(c.teams.count() for c in cats)} teams"},
        {"label": "Format", "done": has_format, "url": reverse("m_format", args=[slug])},
        {"label": "Stages & advancement", "done": slots_ok, "url": reverse("m_format", args=[slug])},
        {"label": "Venues", "done": spaces > 0, "url": reverse("m_venues", args=[slug]),
         "detail": f"{spaces} {tournament.space_noun.lower()}{'s' if spaces != 1 else ''}"},
        {"label": "Schedule", "done": sched_ok, "url": reverse("m_schedule", args=[slug]), "detail": sched_detail},
        {"label": "Review & publish", "done": tournament.is_live, "url": reverse("m_review", args=[slug])},
    ]


def publish(tournament, user=None):
    errors, _ = validate(tournament)
    if errors:
        raise ServiceError("Fix the errors before publishing.")
    ensure_paid(tournament)
    tournament.status = Tournament.Status.PUBLISHED
    tournament.published_at = timezone.now()
    tournament.save(update_fields=["status", "published_at"])
    touch(tournament)  # moves straight to in-progress if results already exist
    log(tournament, user, "publish", "Tournament published")


def unpublish(tournament, user=None):
    if Game.objects.filter(stage__category__tournament=tournament,
                           status__in=[Game.Status.COMPLETED, Game.Status.FORFEIT]).exists():
        raise ServiceError("Results have been entered; the tournament can't go back to draft.")
    tournament.status = Tournament.Status.DRAFT
    tournament.save(update_fields=["status"])
    touch(tournament)
    log(tournament, user, "unpublish", "Tournament unpublished")
