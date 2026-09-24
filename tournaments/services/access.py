"""Roles and visibility (SPEC §2, §13)."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404

from ..models import Membership, Tournament

RANK = {Membership.Role.SCOREKEEPER: 1, Membership.Role.ADMIN: 2, Membership.Role.OWNER: 3}


def role_of(user, tournament):
    if not user.is_authenticated:
        return None
    if tournament.owner_id == user.id:
        return Membership.Role.OWNER
    m = Membership.objects.filter(tournament=tournament, user=user).first()
    return m.role if m else None


def can(user, tournament, needed):
    r = role_of(user, tournament)
    return r is not None and RANK[r] >= RANK[needed]


def manage(needed=Membership.Role.ADMIN):
    """View decorator: resolves ``slug`` to ``request.tournament`` and checks the role."""
    def deco(view):
        @login_required
        @wraps(view)
        def wrapped(request, slug, *args, **kwargs):
            t = get_object_or_404(Tournament, slug=slug)
            r = role_of(request.user, t)
            if r is None or RANK[r] < RANK[needed]:
                raise Http404
            request.tournament = t
            request.role = r
            return view(request, t, *args, **kwargs)
        return wrapped
    return deco


def public_tournament(request, slug):
    t = get_object_or_404(Tournament, slug=slug)
    is_admin = role_of(request.user, t) is not None
    if not is_admin and (t.status == Tournament.Status.DRAFT or t.visibility == Tournament.Visibility.PRIVATE):
        raise Http404
    return t, is_admin
