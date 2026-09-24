from django import template
from django.utils.html import format_html

register = template.Library()

STATUS_CLASS = {
    "draft": "muted", "published": "info", "in_progress": "live", "completed": "ok",
    "scheduled": "muted", "forfeit": "warn", "cancelled": "muted",
    "waiting": "muted", "not_started": "muted", "complete": "ok", "transitioned": "ok",
}
STATUS_LABEL = {
    "waiting": "Waiting for teams", "not_started": "Not started", "in_progress": "In progress",
    "complete": "Complete", "transitioned": "Advanced", "draft": "Draft", "published": "Published",
    "completed": "Completed", "scheduled": "Scheduled", "forfeit": "Forfeit", "cancelled": "Cancelled",
}


@register.simple_tag
def badge(status, label=None):
    return format_html('<span class="badge badge-{}">{}</span>', STATUS_CLASS.get(status, "muted"),
                       label or STATUS_LABEL.get(status, status))


@register.filter
def get(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def side_class(game, side):
    """'win' / 'loss' CSS class for a side of a decided game."""
    if not game.has_result or game.winner_side == "D":
        return ""
    return "win" if game.winner_side == side else "loss"


@register.filter
def ordinal(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return n
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
