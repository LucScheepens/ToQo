"""Merge two categories into one with an upper and a lower starting poule.

    manage.py merge_categories <tournament-slug> "<upper category>" "<lower category>" [--name NAME]

Teams of the upper category get starting poule 1, the lower category's poule 2. The upper
category is kept (and renamed); the lower one is deleted. Neither may have results.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tournaments.models import Tournament
from tournaments.services.structure import category_has_results


class Command(BaseCommand):
    help = "Merge two categories into one, keeping each team's origin as its starting poule."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("upper")
        parser.add_argument("lower")
        parser.add_argument("--name", help="Name of the merged category (default: keep the upper's name)")

    @transaction.atomic
    def handle(self, slug, upper, lower, name=None, **opts):
        t = Tournament.objects.filter(slug=slug).first()
        if not t:
            raise CommandError(f"No tournament with slug {slug!r}.")
        cats = {c.name.strip(): c for c in t.categories.all()}
        try:
            up, low = cats[upper.strip()], cats[lower.strip()]
        except KeyError as exc:
            raise CommandError(f"No category {exc.args[0]!r}; have: {', '.join(cats)}") from None
        for c in (up, low):
            if category_has_results(c):
                raise CommandError(f"{c.name} already has results; merge before play starts.")
        clash = set(up.teams.values_list("name", flat=True)) & set(low.teams.values_list("name", flat=True))
        if clash:
            raise CommandError(f"Team names in both categories: {', '.join(sorted(clash))}")

        up.teams.update(group=1)
        offset = max(up.teams.values_list("order", flat=True), default=0)
        for team in low.teams.all():
            team.category, team.group, team.order = up, 2, offset + team.order
            team.save(update_fields=["category", "group", "order"])
        up.stages.all().delete()
        up.format_kind, up.format_params = "", {}
        up.name = name or up.name.strip()
        up.save()
        low.delete()
        self.stdout.write(self.style.SUCCESS(
            f"{up.name}: {up.teams.filter(group=1).count()} upper + {up.teams.filter(group=2).count()} lower players."))
