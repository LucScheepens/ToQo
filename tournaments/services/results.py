"""Results and game options (SPEC §10)."""
from django.utils import timezone

from ..models import Game, Stage, Team, Tournament
from .common import ServiceError, apply_change, touch


def _check_live(game):
    t = game.stage.category.tournament
    if t.status == Tournament.Status.DRAFT:
        raise ServiceError("Publish the tournament to start entering results.")
    if t.status == Tournament.Status.COMPLETED:
        raise ServiceError("The tournament is finished. Reopen it from the Standings page to change results.")
    if not game.teams_known:
        raise ServiceError("Both teams must be known before a result can be entered.")
    if game.is_bye:
        raise ServiceError("Byes don't need a result.")
    if game.if_necessary and game.status == Game.Status.CANCELLED:
        raise ServiceError("This game is not necessary.")


def _elimination(game):
    return game.stage.kind in (Stage.Kind.SE, Stage.Kind.DE)


def _after(game, was_space):
    from .scheduling import start_next

    t = game.stage.category.tournament
    if t.schedule_mode == Tournament.ScheduleMode.QUEUE and was_space is not None:
        start_next(t, was_space)


def submit_result(game, home_score, away_score, winner_side=None, *, user=None, confirm=False):
    _check_live(game)
    if home_score is None or away_score is None or home_score < 0 or away_score < 0:
        raise ServiceError("Enter both scores (0 or more).")
    t = game.stage.category.tournament
    if home_score > away_score:
        side = "H"
    elif away_score > home_score:
        side = "A"
    else:
        draws_ok = t.allow_draws and not _elimination(game)
        if winner_side in ("H", "A"):
            side = winner_side
        elif draws_ok:
            side = "D"
        else:
            raise ServiceError("Scores are tied — pick the winner (e.g. overtime or shootout).")
    queue_space = game.space if game.status == Game.Status.IN_PROGRESS else None

    def change():
        game.home_score, game.away_score = home_score, away_score
        game.winner_side, game.forfeit_side = side, ""
        game.status = Game.Status.COMPLETED
        game.finished_at = timezone.now()
        game.save()

    affected = apply_change(game.stage.category, change, confirm=confirm, user=user, verb="result",
                            text=f"#{game.number} {game.home_label} {home_score}–{away_score} {game.away_label}")
    if not affected:
        _after(game, queue_space)
    return affected


def forfeit(game, forfeiting_side, *, user=None, confirm=False):
    _check_live(game)
    if forfeiting_side not in ("H", "A"):
        raise ServiceError("Choose which team forfeits.")
    t = game.stage.category.tournament
    queue_space = game.space if game.status == Game.Status.IN_PROGRESS else None

    def change():
        game.status = Game.Status.FORFEIT
        game.forfeit_side = forfeiting_side
        game.winner_side = "A" if forfeiting_side == "H" else "H"
        win, loss = t.forfeit_win_score, t.forfeit_loss_score
        game.home_score, game.away_score = (loss, win) if forfeiting_side == "H" else (win, loss)
        game.finished_at = timezone.now()
        game.save()

    loser = game.home_label if forfeiting_side == "H" else game.away_label
    affected = apply_change(game.stage.category, change, confirm=confirm, user=user, verb="forfeit",
                            text=f"#{game.number} forfeited by {loser}")
    if not affected:
        _after(game, queue_space)
    return affected


def cancel(game, *, user=None, confirm=False):
    if _elimination(game):
        raise ServiceError("Elimination games can't be cancelled — record a forfeit instead.")

    def change():
        game.status = Game.Status.CANCELLED
        game.home_score = game.away_score = None
        game.winner_side = game.forfeit_side = ""
        game.save()

    return apply_change(game.stage.category, change, confirm=confirm, user=user, verb="cancel",
                        text=f"#{game.number} {game.home_label} vs {game.away_label} cancelled")


def clear_result(game, *, user=None, confirm=False):
    def change():
        game.status = Game.Status.SCHEDULED
        game.home_score = game.away_score = None
        game.winner_side = game.forfeit_side = ""
        game.finished_at = game.started_at = None
        game.save()

    return apply_change(game.stage.category, change, confirm=confirm, user=user, verb="clear",
                        text=f"#{game.number} result cleared")


def set_note(game, note, *, user=None):
    game.note = (note or "").strip()[:300]
    game.save(update_fields=["note"])
    touch(game.stage.category.tournament)


def swap_home(game, *, user=None):
    def change():
        for a, b in (("home_entry", "away_entry"), ("home_prev", "away_prev"),
                     ("home_prev_outcome", "away_prev_outcome"), ("home_team", "away_team"),
                     ("home_bye", "away_bye"), ("home_score", "away_score")):
            va, vb = getattr(game, a), getattr(game, b)
            setattr(game, a, vb)
            setattr(game, b, va)
        flip = {"H": "A", "A": "H"}
        game.winner_side = flip.get(game.winner_side, game.winner_side)
        game.forfeit_side = flip.get(game.forfeit_side, game.forfeit_side)
        game.save()

    apply_change(game.stage.category, change, confirm=True, user=user, verb="swap",
                 text=f"#{game.number} home/away swapped")


def change_teams(game, home: Team, away: Team, *, user=None):
    if game.has_result:
        raise ServiceError("Clear the result before changing the teams.")
    if home == away:
        raise ServiceError("Pick two different teams.")
    cat = game.stage.category
    if home.category_id != cat.id or away.category_id != cat.id:
        raise ServiceError("Teams must belong to this category.")

    def change():
        game.manual_teams = True
        game.home_team, game.away_team = home, away
        game.home_bye = game.away_bye = False
        game.save()

    return apply_change(cat, change, confirm=True, user=user, verb="teams",
                        text=f"#{game.number} teams changed to {home} vs {away}")


def restore_teams(game, *, user=None):
    def change():
        game.manual_teams = False
        game.save(update_fields=["manual_teams"])

    return apply_change(game.stage.category, change, confirm=True, user=user)
