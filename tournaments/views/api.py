"""JSON API (SPEC §15)."""
import json
from datetime import datetime

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from ..models import Game, Membership, Space, Tournament
from ..presenters import category_view, public_games
from ..services import results, scheduling
from ..services.access import can, public_tournament
from ..services.common import ServiceError


def _team(t):
    return {"id": t.id, "name": t.name} if t else None


def _game(g):
    return {
        "id": g.id, "number": g.number, "code": g.code, "label": g.title,
        "category": g.stage.category.name, "stage": g.stage.name, "pool": g.pool.name,
        "home": _team(g.home_team), "away": _team(g.away_team),
        "home_label": g.home_label, "away_label": g.away_label,
        "status": g.status, "home_score": g.home_score, "away_score": g.away_score,
        "winner_side": g.winner_side or None, "note": g.note,
        "space": g.space.full_name if g.space else None,
        "start": g.start_at.isoformat() if g.start_at else None, "duration": g.duration,
    }


@require_GET
def version(request, slug):
    t, _ = public_tournament(request, slug)
    return JsonResponse({"version": t.version, "status": t.status})


@require_GET
def tournament(request, slug):
    t, _ = public_tournament(request, slug)
    return JsonResponse({
        "name": t.name, "slug": t.slug, "sport": t.sport, "status": t.status,
        "start_date": t.start_date.isoformat(), "end_date": t.end_date.isoformat(), "location": t.location,
        "categories": [{
            "id": c.id, "name": c.name, "format": c.format_kind,
            "teams": [_team(x) for x in c.teams.all()],
            "stages": [{"id": s.id, "name": s.name, "kind": s.kind,
                        "pools": [{"id": p.id, "name": p.name} for p in s.pools.all()]} for s in c.stages.all()],
        } for c in t.categories.all()],
    })


@require_GET
def schedule(request, slug):
    t, _ = public_tournament(request, slug)
    games = sorted(public_games(t), key=lambda g: (g.start_at is None, g.start_at or datetime.min, g.number))
    return JsonResponse({"version": t.version, "games": [_game(g) for g in games]})


@require_GET
def standings(request, slug):
    t, _ = public_tournament(request, slug)
    out = []
    for c in t.categories.all():
        cv = category_view(c)
        out.append({
            "category": c.name,
            "stages": [{
                "name": sv["stage"].name, "kind": sv["stage"].kind, "status": sv["status"],
                "pools": [{"name": p["pool"].name, "rows": [{
                    "rank": r.rank, "team": _team(r.team), "gp": r.gp, "w": r.w, "d": r.d, "l": r.l,
                    "pf": r.pf, "pa": r.pa, "pd": r.pd, "pts": r.pts} for r in p["rows"]]}
                    for p in sv["pools"]],
            } for sv in cv["stages"]],
            "final": [{"place": p, "team": _team(team)} for p, team, _ in cv["final"]],
        })
    return JsonResponse({"version": t.version, "categories": out})


def _manage_game(request, slug, game_id, role):
    t = get_object_or_404(Tournament, slug=slug)
    if not can(request.user, t, role):
        return None, None
    return t, get_object_or_404(Game, pk=game_id, stage__category__tournament=t)


@require_POST
def submit_result(request, slug, game_id):
    t, game = _manage_game(request, slug, game_id, Membership.Role.SCOREKEEPER)
    if game is None:
        return JsonResponse({"error": "forbidden"}, status=403)
    try:
        body = json.loads(request.body or "{}")
        affected = results.submit_result(game, int(body["home_score"]), int(body["away_score"]),
                                         body.get("winner_side") or None, user=request.user,
                                         confirm=bool(body.get("confirm")))
    except (KeyError, ValueError, TypeError):
        return JsonResponse({"error": "home_score and away_score are required integers"}, status=400)
    except ServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    if affected:
        return JsonResponse({"needs_confirmation": True, "affected": affected}, status=409)
    return JsonResponse({"ok": True})


@require_POST
def move_game(request, slug, game_id):
    t, game = _manage_game(request, slug, game_id, Membership.Role.ADMIN)
    if game is None:
        return JsonResponse({"error": "forbidden"}, status=403)
    try:
        body = json.loads(request.body or "{}")
        if body.get("unschedule"):
            scheduling.move_game(game, unschedule=True, user=request.user)
        else:
            space = get_object_or_404(Space, pk=body["space_id"], venue__tournament=t)
            scheduling.move_game(game, space, datetime.fromisoformat(body["start"]), user=request.user)
    except (KeyError, ValueError, TypeError):
        return JsonResponse({"error": "space_id and start (ISO datetime) are required"}, status=400)
    except ServiceError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({"ok": True})
