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


TONES = {"advanced": 1, "intermediate": 2, "beginner": 3, "upper": 4, "lower": 5}


@register.filter
def pool_tone(pool):
    """Colour class for a pool/flight: named tiers get fixed colours, others cycle by order."""
    first = (pool.name.split() or [""])[0].lower()
    return f"tone-{TONES.get(first, pool.order % 6 + 1)}"


@register.simple_tag
def asset(path):
    """Static URL with the file's mtime appended, so browsers pick up CSS/JS changes."""
    import os

    from django.contrib.staticfiles import finders
    from django.templatetags.static import static
    found = finders.find(path)
    return f"{static(path)}?v={int(os.path.getmtime(found))}" if found else static(path)


@register.filter
def round_label(game):
    """Short round name without the pool prefix: "Round 2", "Quarterfinal 3"."""
    return (game.label or game.title).split(" · ")[-1].replace("Swiss round", "Round")


@register.filter
def minutes_since(dt):
    """Compact elapsed time for a live game: "7 min", "1 h 04"."""
    from datetime import datetime
    if not dt:
        return ""
    m = max(0, int((datetime.now() - dt).total_seconds() // 60))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d}"
