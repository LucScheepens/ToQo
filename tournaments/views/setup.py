"""Organizer setup screens (SPEC §16): tournaments, categories, teams, format, stages,
venues, home page, admins, review/publish, share."""
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Max, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from engine.standings import TIEBREAKERS

from ..forms import (AddStageForm, BulkTeamsForm, CategoryForm, FormatForm, HomeBlockForm, InviteForm, SignupForm,
                     SpaceForm, StageForm, TeamForm, TournamentCreateForm, TournamentSettingsForm, VenueForm)
from ..models import (Activity, Category, Entry, Game, HomeBlock, Invitation, Membership, Pool, Space, Stage, Team,
                      Tournament, Venue)
from ..presenters import category_view
from ..services import publishing, structure, transitions
from ..services.access import manage
from ..services.common import ServiceError, log, touch
from ..services.resolution import recompute_category, stage_status
from ..services.scheduling import find_conflicts, tournament_games
from ..services.standings import tiebreakers_for
from .base import error, page

OWNER = Membership.Role.OWNER


def home(request):
    mine = []
    if request.user.is_authenticated:
        mine = Tournament.objects.filter(Q(owner=request.user) | Q(memberships__user=request.user)).distinct()
    public = Tournament.objects.filter(visibility=Tournament.Visibility.PUBLIC).exclude(
        status=Tournament.Status.DRAFT)[:30]
    return render(request, "home.html", {"mine": mine, "public": public})


def signup(request):
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        _accept_pending(request, user)
        return redirect(request.GET.get("next") or "home")
    return render(request, "registration/signup.html", {"form": form})


def _accept_pending(request, user):
    token = request.session.pop("invite_token", None)
    if token:
        inv = Invitation.objects.filter(token=token, accepted_at__isnull=True).first()
        if inv:
            _accept(inv, user)


def _accept(inv, user):
    if inv.tournament.owner_id != user.id:
        Membership.objects.update_or_create(tournament=inv.tournament, user=user, defaults={"role": inv.role})
    inv.accepted_at = timezone.now()
    inv.save(update_fields=["accepted_at"])


def invite_accept(request, token):
    inv = get_object_or_404(Invitation, token=token)
    if inv.accepted_at:
        messages.info(request, "This invitation has already been used.")
        return redirect("home")
    if not request.user.is_authenticated:
        request.session["invite_token"] = token
        return render(request, "invite.html", {"inv": inv})
    if request.method == "POST":
        _accept(inv, request.user)
        messages.success(request, f"You're now helping run {inv.tournament.name}.")
        return redirect("m_dashboard", inv.tournament.slug)
    return render(request, "invite.html", {"inv": inv})


@login_required
def tournament_new(request):
    form = TournamentCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            t = form.save(commit=False)
            t.owner = request.user
            t.slug = form.unique_slug()
            t.save()
            structure.create_default_category(t)
            HomeBlock.objects.create(tournament=t, kind="text", title="Welcome",
                                     body=f"Welcome to {t.name}! Check the schedule and standings here "
                                          "throughout the event.", order=0)
            log(t, request.user, "create", "Tournament created")
        return redirect("m_dashboard", t.slug)
    return render(request, "manage/new.html", {"form": form})


@manage(Membership.Role.SCOREKEEPER)
def dashboard(request, t):
    cats = [category_view(c) for c in t.categories.all()]
    games = tournament_games(t)
    playable = [g for g in games if not g.is_bye and not (g.if_necessary and g.status == Game.Status.CANCELLED)]
    done = sum(1 for g in playable if g.is_resolved)
    actions = []
    steps = publishing.checklist(t)
    if not t.is_live:
        todo = [s for s in steps if not s["done"] and s["label"] != "Review & publish"]
        for s in todo[:3]:
            actions.append({"text": f"Complete setup: {s['label']}", "url": s["url"]})
        if not todo:
            actions.append({"text": "Everything is set up — review and publish", "url": reverse("m_review", args=[t.slug])})
    for cv in cats:
        for sv in cv["stages"]:
            if sv["status"] == "complete" and transitions.can_advance(sv["stage"]):
                actions.append({"text": f"{cv['category'].name}: {sv['stage'].name} is complete — advance teams",
                                "url": reverse("m_standings", args=[t.slug]) + f"#stage-{sv['stage'].id}",
                                "primary": True})
    conflicts = find_conflicts(t, games) if t.schedule_mode == Tournament.ScheduleMode.TIMED else {}
    if conflicts:
        actions.append({"text": f"{len(conflicts)} game(s) have schedule conflicts",
                        "url": reverse("m_schedule", args=[t.slug]) + "?view=list&conflicts=1"})
    if t.status == Tournament.Status.IN_PROGRESS and transitions.all_complete(t):
        actions.append({"text": "All games are done — finish the tournament",
                        "url": reverse("m_standings", args=[t.slug]), "primary": True})
    upcoming = sorted([g for g in playable if g.status in (Game.Status.SCHEDULED, Game.Status.IN_PROGRESS)
                       and g.teams_known], key=lambda g: (g.status != Game.Status.IN_PROGRESS,
                                                          g.start_at is None, g.start_at or 0, g.queue_order))[:6]
    return page(request, t, "manage/dashboard.html", "dashboard", cats=cats, done=done, total=len(playable),
                actions=actions, upcoming=upcoming, activity=t.activity.select_related("user")[:12],
                dash_steps=steps)


@manage()
def settings_view(request, t):
    form = TournamentSettingsForm(request.POST or None, request.FILES or None, instance=t)
    if request.method == "POST":
        if request.POST.get("action") == "delete":
            if request.role != OWNER:
                messages.error(request, "Only the owner can delete the tournament.")
            elif request.POST.get("confirm_name") != t.name:
                messages.error(request, "Type the tournament name exactly to delete it.")
            else:
                t.delete()
                messages.success(request, "Tournament deleted.")
                return redirect("home")
            return redirect("m_settings", t.slug)
        if form.is_valid():
            if Tournament.objects.filter(slug=form.cleaned_data["slug"]).exclude(pk=t.pk).exists():
                form.add_error("slug", "That address is taken.")
            else:
                form.save()
                touch(t)
                messages.success(request, "Settings saved.")
                return redirect("m_settings", t.slug)
    return page(request, t, "manage/settings.html", "settings", form=form)


# -- categories -----------------------------------------------------------------

@manage()
def categories(request, t):
    form = CategoryForm()
    if request.method == "POST":
        action = request.POST.get("action")
        cat = t.categories.filter(pk=request.POST.get("id")).first()
        if action == "add":
            form = CategoryForm(request.POST)
            if form.is_valid():
                order = (t.categories.aggregate(m=Max("order"))["m"] or 0) + 1
                Category.objects.create(tournament=t, name=form.cleaned_data["name"], order=order)
                touch(t)
                return redirect("m_categories", t.slug)
        elif cat and action == "rename":
            name = request.POST.get("name", "").strip()[:80]
            if name:
                cat.name = name
                cat.save(update_fields=["name"])
                touch(t)
            return redirect("m_categories", t.slug)
        elif cat and action == "delete":
            if t.categories.count() == 1:
                messages.error(request, "A tournament needs at least one category.")
            elif structure.category_has_results(cat) and not request.POST.get("confirm"):
                messages.error(request, "This category has results. Tick the confirmation to delete it.")
            else:
                cat.delete()
                touch(t)
            return redirect("m_categories", t.slug)
        elif cat and action in ("up", "down"):
            cats = list(t.categories.all())
            i = cats.index(cat)
            j = i - 1 if action == "up" else i + 1
            if 0 <= j < len(cats):
                cats[i], cats[j] = cats[j], cats[i]
                for n, c in enumerate(cats):
                    Category.objects.filter(pk=c.pk).update(order=n)
                touch(t)
            return redirect("m_categories", t.slug)
    cats = t.categories.all()
    return page(request, t, "manage/categories.html", "categories", form=form, cats=cats)


# -- teams ------------------------------------------------------------------------

def _team_change(request, cat, fn):
    """Apply a team-list change, regenerating the format (SPEC §5)."""
    if cat.stages.exists() and structure.category_has_results(cat) and not request.POST.get("confirm"):
        messages.error(request, f"{cat.name} already has results. Changing teams regenerates its games and "
                                "deletes those results — tick the confirmation box to continue.")
        return False
    try:
        with transaction.atomic():
            fn()
            if cat.stages.exists() or cat.format_kind:
                structure.teams_changed(cat)
                messages.info(request, f"{cat.name}: format regenerated — its games need to be scheduled again."
                              if cat.format_kind != "custom" else
                              f"{cat.name}: place new teams on the stage page.")
        touch(cat.tournament)
    except ServiceError as exc:
        error(request, exc)
        return False
    return True


@manage()
def teams(request, t):
    cats = list(t.categories.all())
    cat = next((c for c in cats if str(c.id) == request.GET.get("cat")), cats[0] if cats else None)
    form, bulk = TeamForm(), BulkTeamsForm()
    url = reverse("m_teams", args=[t.slug]) + (f"?cat={cat.id}" if cat else "")
    if request.method == "POST" and cat:
        action = request.POST.get("action")
        if action == "add":
            form = TeamForm(request.POST)
            if form.is_valid():
                if cat.teams.filter(name=form.cleaned_data["name"]).exists():
                    form.add_error("name", "A team with this name already exists in this category.")
                else:
                    def fn():
                        team = form.save(commit=False)
                        team.category = cat
                        team.order = (cat.teams.aggregate(m=Max("order"))["m"] or 0) + 1
                        team.save()
                    if _team_change(request, cat, fn):
                        return redirect(url)
        elif action == "bulk":
            bulk = BulkTeamsForm(request.POST)
            if bulk.is_valid():
                created = []
                if _team_change(request, cat, lambda: created.append(
                        structure.bulk_add_teams(cat, bulk.cleaned_data["names"]))):
                    messages.success(request, f"Added {created[0]} team(s).")
                    return redirect(url)
        elif action == "delete":
            team = get_object_or_404(Team, pk=request.POST.get("id"), category=cat)
            if _team_change(request, cat, team.delete):
                messages.success(request, f"Removed {team.name}.")
            return redirect(url)
        elif action == "seeds":
            def fn():
                for team in cat.teams.all():
                    raw = request.POST.get(f"seed_{team.id}", "").strip()
                    team.seed = int(raw) if raw.isdigit() else None
                    team.save(update_fields=["seed"])
            if _team_change(request, cat, fn):
                messages.success(request, "Seeds saved.")
            return redirect(url)
    team_list = sorted(cat.teams.all(), key=lambda x: (x.seed is None, x.seed or 0, x.order)) if cat else []
    return page(request, t, "manage/teams.html", "teams", cats=cats, cat=cat, form=form, bulk=bulk,
                team_list=team_list, has_results=cat and structure.category_has_results(cat))


@manage()
def team_edit(request, t, team_id):
    team = get_object_or_404(Team, pk=team_id, category__tournament=t)
    form = TeamForm(request.POST or None, instance=team)
    if request.method == "POST" and form.is_valid():
        seed_changed = "seed" in form.changed_data
        form.save()
        if seed_changed and team.category.format_kind in structure.TEMPLATED and \
                not structure.category_has_results(team.category):
            structure.teams_changed(team.category)
            messages.info(request, "Seed changed — pools and brackets regenerated.")
        touch(t)
        return redirect(reverse("m_teams", args=[t.slug]) + f"?cat={team.category_id}")
    return page(request, t, "manage/team_edit.html", "teams", form=form, team=team)


# -- format & stages ------------------------------------------------------------

@manage()
def format_overview(request, t):
    cats = [category_view(c) for c in t.categories.all()]
    return page(request, t, "manage/format_overview.html", "format", cats=cats)


@manage()
def format_category(request, t, cat_id):
    cat = get_object_or_404(Category, pk=cat_id, tournament=t)
    initial = {"kind": cat.format_kind or "rr_playoff", **(cat.format_params or {})}
    form = FormatForm(request.POST or None, initial=initial)
    has_results = structure.category_has_results(cat)
    if request.method == "POST" and form.is_valid():
        if cat.stages.exists() and not form.cleaned_data["confirm"]:
            messages.error(request, "This replaces the current structure and its games" +
                           (" including all results" if has_results else "") + ". Tick the confirmation box.")
        else:
            try:
                structure.apply_format(cat, form.cleaned_data["kind"], {k: v for k, v in form.cleaned_data.items()
                                                                        if v not in (None, "")})
                touch(t)
                log(t, request.user, "format", f"{cat.name}: format set to {cat.get_format_kind_display()}")
                messages.success(request, f"{cat.name}: format created. Review the stages below.")
                return redirect("m_format", t.slug)
            except ServiceError as exc:
                error(request, exc)
    return page(request, t, "manage/format_category.html", "format", form=form, cat=cat,
                has_results=has_results, team_count=cat.teams.count())


def _source_options(stage):
    """Choices for a slot's source: pools and whole stages before this one."""
    opts = []
    for s in stage.category.stages.filter(order__lt=stage.order):
        size = 0
        for pool in s.pools.all():
            n = pool.entries.count()
            size += n
            for r in range(1, n + 1):
                opts.append((f"pool:{pool.id}:{r}", f"{s.name} · {pool.name} #{r}"))
        for r in range(1, size + 1):
            opts.append((f"stage:{s.id}:{r}", f"{s.name} · overall #{r}"))
    return opts


def _entry_source_value(e):
    if e.source_pool_id:
        return f"pool:{e.source_pool_id}:{e.source_rank}"
    if e.source_stage_id:
        return f"stage:{e.source_stage_id}:{e.source_rank}"
    return ""


@manage()
def stage_detail(request, t, stage_id):
    stage = get_object_or_404(Stage, pk=stage_id, category__tournament=t)
    cat = stage.category
    url = reverse("m_stage", args=[t.slug, stage.id])
    cfg = stage.config or {}
    form = StageForm(request.POST or None, initial={
        "name": stage.name, "game_minutes": stage.game_minutes, "cycles": cfg.get("cycles", 1),
        "games_per_team": cfg.get("games_per_team"), "placement": cfg.get("placement", "none"),
        "grand_final_reset": cfg.get("grand_final_reset", True), "rounds": cfg.get("rounds", 3)})
    add_form = AddStageForm()
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "settings" and form.is_valid():
                d = form.cleaned_data
                new_cfg = dict(cfg)
                if stage.kind == "rr":
                    new_cfg.update(cycles=int(d["cycles"] or 1), games_per_team=d["games_per_team"])
                elif stage.kind == "se":
                    new_cfg.update(placement=d["placement"] or "none")
                elif stage.kind == "de":
                    new_cfg.update(grand_final_reset=d["grand_final_reset"])
                elif stage.kind == "swiss":
                    new_cfg.update(rounds=d["rounds"] or 3)
                tbs = [request.POST.get(f"tb{i}") for i in range(6)]
                stage.tiebreakers = [x for i, x in enumerate(tbs) if x in TIEBREAKERS and x not in tbs[:i]]
                stage.name = d["name"]
                stage.game_minutes = d["game_minutes"]
                structural = new_cfg != cfg
                if structural and structure.category_has_results(cat) and stage.games.filter(
                        status__in=[Game.Status.COMPLETED, Game.Status.FORFEIT]).exists() \
                        and not request.POST.get("confirm"):
                    raise ServiceError("This stage has results; changing its structure deletes them. "
                                       "Tick the confirmation box.")
                stage.config = new_cfg
                stage.save()
                stage.games.update(duration=stage.duration)
                if structural:
                    structure.build_games(stage)
                    messages.info(request, "Games rebuilt — schedule them again.")
                else:
                    recompute_category(cat)
                touch(t)
                messages.success(request, "Stage saved.")
                return redirect(url)
            if action == "entry":
                entry = get_object_or_404(Entry, pk=request.POST.get("entry"), pool__stage=stage)
                team_raw = request.POST.get("team")
                team = None
                if team_raw not in (None, "", "__keep__"):
                    team = get_object_or_404(Team, pk=team_raw, category=cat)
                structure.set_entry(
                    entry,
                    team=(team or False) if team_raw not in (None, "__keep__") else None,
                    source=request.POST.get("source") if "source" in request.POST else None,
                )
                recompute_category(cat)
                touch(t)
                return redirect(url)
            if action == "unlock":
                entry = get_object_or_404(Entry, pk=request.POST.get("entry"), pool__stage=stage)
                entry.locked = False
                entry.team = None
                entry.save()
                recompute_category(cat)
                touch(t)
                return redirect(url)
            if action == "resize":
                pool = get_object_or_404(Pool, pk=request.POST.get("pool"), stage=stage)
                structure.resize_pool(pool, max(1, int(request.POST.get("slots") or 1)))
                touch(t)
                return redirect(url)
            if action == "add_pool":
                structure.add_pool(stage)
                touch(t)
                return redirect(url)
            if action == "delete_pool":
                structure.delete_pool(get_object_or_404(Pool, pk=request.POST.get("pool"), stage=stage))
                touch(t)
                return redirect(url)
            if action == "rename_pool":
                pool = get_object_or_404(Pool, pk=request.POST.get("pool"), stage=stage)
                pool.name = request.POST.get("name", pool.name).strip()[:60] or pool.name
                pool.save(update_fields=["name"])
                touch(t)
                return redirect(url)
            if action == "delete_stage":
                if not request.POST.get("confirm"):
                    raise ServiceError("Tick the confirmation box to delete this stage.")
                structure.delete_stage(stage)
                touch(t)
                return redirect("m_format", t.slug)
            if action == "add_stage":
                add_form = AddStageForm(request.POST)
                if add_form.is_valid():
                    d = add_form.cleaned_data
                    new = structure.add_stage(cat, d["name"], d["kind"], d["pools"], d["slots"])
                    touch(t)
                    return redirect("m_stage", t.slug, new.id)
        except (ServiceError, ValueError) as exc:
            error(request, exc)
            return redirect(url)

    is_first = stage.order == cat.stages.first().order
    pools = []
    for pool in stage.pools.prefetch_related("entries__team", "entries__source_pool", "entries__source_stage"):
        pools.append({"pool": pool, "entries": [(e, _entry_source_value(e)) for e in pool.entries.all()]})
    tbs = tiebreakers_for(stage) + [""] * 6
    return page(request, t, "manage/stage.html", "format", stage=stage, cat=cat, form=form, add_form=add_form,
                pools=pools, is_first=is_first, teams=cat.teams.all(), source_options=_source_options(stage),
                tiebreaker_choices=TIEBREAKERS.items(), tbs=tbs[:6], status=stage_status(stage),
                unassigned=structure.unassigned_teams(cat) if is_first else [],
                games=stage.games.select_related("home_team", "away_team", "space", "home_entry__team",
                                                 "away_entry__team", "home_prev", "away_prev",
                                                 "home_entry__source_pool", "away_entry__source_pool")
                .order_by("round", "pool__order", "index", "id"))


@manage()
def category_add_stage(request, t, cat_id):
    cat = get_object_or_404(Category, pk=cat_id, tournament=t)
    form = AddStageForm(request.POST or None, initial={"name": "Pool play" if not cat.stages.exists() else "Playoffs",
                                                       "slots": max(2, cat.teams.count())})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        stage = structure.add_stage(cat, d["name"], d["kind"], d["pools"], d["slots"])
        touch(t)
        return redirect("m_stage", t.slug, stage.id)
    return page(request, t, "manage/add_stage.html", "format", form=form, cat=cat)


# -- venues -----------------------------------------------------------------------

@manage()
def venues(request, t):
    vform, sform = VenueForm(), SpaceForm()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add_venue":
            vform = VenueForm(request.POST)
            if vform.is_valid():
                v = vform.save(commit=False)
                v.tournament = t
                v.order = t.venues.count()
                v.save()
                n = int(request.POST.get("spaces") or 0)
                for i in range(1, min(n, 50) + 1):
                    Space.objects.create(venue=v, name=f"{t.space_noun} {i}", order=i)
                touch(t)
                return redirect("m_venues", t.slug)
        elif action == "add_space":
            v = get_object_or_404(Venue, pk=request.POST.get("venue"), tournament=t)
            name = request.POST.get("name", "").strip() or f"{t.space_noun} {v.spaces.count() + 1}"
            Space.objects.create(venue=v, name=name[:60], order=v.spaces.count() + 1)
            touch(t)
            return redirect("m_venues", t.slug)
        elif action == "rename_space":
            s = get_object_or_404(Space, pk=request.POST.get("space"), venue__tournament=t)
            s.name = request.POST.get("name", s.name).strip()[:60] or s.name
            s.save(update_fields=["name"])
            touch(t)
            return redirect("m_venues", t.slug)
        elif action == "delete_space":
            s = get_object_or_404(Space, pk=request.POST.get("space"), venue__tournament=t)
            n = s.games.count()
            s.delete()
            if n:
                messages.info(request, f"{n} game(s) on {s.name} are now unscheduled.")
            touch(t)
            return redirect("m_venues", t.slug)
        elif action == "edit_venue":
            v = get_object_or_404(Venue, pk=request.POST.get("venue"), tournament=t)
            f = VenueForm(request.POST, instance=v)
            if f.is_valid():
                f.save()
                touch(t)
            return redirect("m_venues", t.slug)
        elif action == "delete_venue":
            v = get_object_or_404(Venue, pk=request.POST.get("venue"), tournament=t)
            v.delete()
            touch(t)
            return redirect("m_venues", t.slug)
    return page(request, t, "manage/venues.html", "venues", vform=vform, sform=sform,
                venue_list=t.venues.prefetch_related("spaces"))


# -- home page blocks ---------------------------------------------------------------

@manage()
def home_blocks(request, t):
    edit = t.blocks.filter(pk=request.GET.get("edit")).first()
    form = HomeBlockForm(instance=edit)
    if request.method == "POST":
        action = request.POST.get("action")
        block = t.blocks.filter(pk=request.POST.get("id")).first()
        if action == "save":
            form = HomeBlockForm(request.POST, request.FILES, instance=block)
            if form.is_valid():
                b = form.save(commit=False)
                b.tournament = t
                if not block:
                    b.order = (t.blocks.aggregate(m=Max("order"))["m"] or 0) + 1
                b.save()
                touch(t)
                return redirect("m_home", t.slug)
            edit = block
        elif block and action == "toggle":
            block.enabled = not block.enabled
            block.save(update_fields=["enabled"])
            touch(t)
            return redirect("m_home", t.slug)
        elif block and action == "delete":
            block.delete()
            touch(t)
            return redirect("m_home", t.slug)
        elif block and action in ("up", "down"):
            blocks = list(t.blocks.all())
            i = blocks.index(block)
            j = i - 1 if action == "up" else i + 1
            if 0 <= j < len(blocks):
                blocks[i], blocks[j] = blocks[j], blocks[i]
                for n, b in enumerate(blocks):
                    HomeBlock.objects.filter(pk=b.pk).update(order=n)
                touch(t)
            return redirect("m_home", t.slug)
    return page(request, t, "manage/home_blocks.html", "home", form=form, edit=edit, blocks=t.blocks.all())


# -- admins ---------------------------------------------------------------------------

@manage(OWNER)
def admins(request, t):
    form = InviteForm(request.POST or None)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "invite" and form.is_valid():
            email, role = form.cleaned_data["email"].lower(), form.cleaned_data["role"]
            user = User.objects.filter(email__iexact=email).first()
            if user and user.id == t.owner_id:
                messages.info(request, "That's you — you already own this tournament.")
            elif user:
                Membership.objects.update_or_create(tournament=t, user=user, defaults={"role": role})
                messages.success(request, f"{user.username} added as {role}.")
            else:
                inv = Invitation.objects.create(tournament=t, email=email, role=role, created_by=request.user)
                link = request.build_absolute_uri(reverse("invite", args=[inv.token]))
                messages.success(request, f"Invitation created. Send this link to {email}: {link}")
            log(t, request.user, "admin", f"Invited {email} as {role}")
            return redirect("m_admins", t.slug)
        if action == "remove":
            Membership.objects.filter(tournament=t, pk=request.POST.get("id")).delete()
            return redirect("m_admins", t.slug)
        if action == "role":
            m = get_object_or_404(Membership, tournament=t, pk=request.POST.get("id"))
            if request.POST.get("role") in (Membership.Role.ADMIN, Membership.Role.SCOREKEEPER):
                m.role = request.POST["role"]
                m.save(update_fields=["role"])
            return redirect("m_admins", t.slug)
        if action == "revoke":
            Invitation.objects.filter(tournament=t, pk=request.POST.get("id"), accepted_at__isnull=True).delete()
            return redirect("m_admins", t.slug)
    invites = [(i, request.build_absolute_uri(reverse("invite", args=[i.token])))
               for i in t.invitations.filter(accepted_at__isnull=True)]
    return page(request, t, "manage/admins.html", "admins", form=form,
                members=t.memberships.select_related("user"), invites=invites)


# -- review, publish, share -------------------------------------------------------------

@manage()
def review(request, t):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "publish":
                if request.role != OWNER:
                    raise ServiceError("Only the owner can publish.")
                publishing.publish(t, request.user)
                messages.success(request, "Published! Share the link with your participants.")
                return redirect("m_share", t.slug)
            if action == "unpublish":
                publishing.unpublish(t, request.user)
                messages.info(request, "The tournament is a draft again.")
        except ServiceError as exc:
            error(request, exc)
        return redirect("m_review", t.slug)
    errors, warnings = publishing.validate(t)
    return page(request, t, "manage/review.html", "review", errors=errors, warnings=warnings)


@manage(Membership.Role.SCOREKEEPER)
def share(request, t):
    import segno

    url = request.build_absolute_uri(reverse("public_home", args=[t.slug]))
    qr = segno.make(url, error="m").svg_inline(scale=6, dark="#111827")
    return page(request, t, "manage/share.html", "share", url=url, qr=qr)


@manage(Membership.Role.SCOREKEEPER)
def activity(request, t):
    return page(request, t, "manage/activity.html", "dashboard",
                items=Activity.objects.filter(tournament=t).select_related("user")[:300])
