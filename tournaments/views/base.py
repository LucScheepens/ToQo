from django.contrib import messages
from django.shortcuts import render

from ..models import Membership
from ..services.publishing import checklist


def page(request, t, template, active, **ctx):
    """Render an organizer page with the shared sidebar context."""
    ctx.update({
        "t": t,
        "role": request.role,
        "is_admin": request.role in (Membership.Role.ADMIN, Membership.Role.OWNER),
        "is_owner": request.role == Membership.Role.OWNER,
        "active": active,
        "steps": checklist(t) if request.role != Membership.Role.SCOREKEEPER else [],
    })
    return render(request, template, ctx)


def safe_next(request, fallback):
    """The ?next= target if it's a local URL, else ``fallback`` (no open redirects)."""
    from django.utils.http import url_has_allowed_host_and_scheme

    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return nxt
    return fallback


def error(request, exc):
    messages.error(request, str(exc))
