"""Tournament data model (SPEC §3)."""
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.urls import reverse

SPORTS = [
    ("volleyball", "Volleyball"),
    ("beach_volleyball", "Beach volleyball"),
    ("soccer", "Soccer"),
    ("basketball", "Basketball"),
    ("badminton", "Badminton"),
    ("squash", "Squash"),
    ("tennis", "Tennis"),
    ("pickleball", "Pickleball"),
    ("hockey", "Hockey"),
    ("baseball", "Baseball"),
    ("softball", "Softball"),
    ("ultimate", "Ultimate"),
    ("other", "Other"),
]
SPACE_NOUN = {
    "soccer": "Field", "ultimate": "Field", "baseball": "Diamond", "softball": "Diamond",
    "hockey": "Rink",
}


class Tournament(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public (listed)"
        UNLISTED = "unlisted", "Unlisted (anyone with the link)"
        PRIVATE = "private", "Private (admins only)"

    class ScheduleMode(models.TextChoices):
        TIMED = "timed", "Assigned times and spaces"
        QUEUE = "queue", "Next available space (queue)"

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=80, unique=True)
    sport = models.CharField(max_length=30, choices=SPORTS, default="volleyball")
    start_date = models.DateField()
    end_date = models.DateField()
    location = models.CharField(max_length=200, blank=True)
    tagline = models.CharField(max_length=200, blank=True)
    banner = models.FileField(upload_to="banners/", blank=True)
    visibility = models.CharField(max_length=10, choices=Visibility.choices, default=Visibility.PUBLIC)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    schedule_mode = models.CharField(max_length=10, choices=ScheduleMode.choices, default=ScheduleMode.TIMED)

    points_win = models.IntegerField(default=3)
    points_draw = models.IntegerField(default=1)
    points_loss = models.IntegerField(default=0)
    allow_draws = models.BooleanField(default=False)
    forfeit_win_score = models.PositiveIntegerField(default=1)
    forfeit_loss_score = models.PositiveIntegerField(default=0)

    default_game_minutes = models.PositiveIntegerField(default=60)
    buffer_minutes = models.PositiveIntegerField(default=0)
    min_rest_minutes = models.PositiveIntegerField(default=0)
    slot_interval_minutes = models.PositiveIntegerField(null=True, blank=True)

    version = models.PositiveIntegerField(default=1)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="owned_tournaments")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-start_date", "name"]

    def __str__(self):
        return self.name

    @property
    def space_noun(self):
        return SPACE_NOUN.get(self.sport, "Court")

    @property
    def is_live(self):
        return self.status != self.Status.DRAFT

    @property
    def interval(self):
        return self.slot_interval_minutes or (self.default_game_minutes + self.buffer_minutes)

    def get_absolute_url(self):
        return reverse("public_home", args=[self.slug])

    def bump(self):
        Tournament.objects.filter(pk=self.pk).update(version=models.F("version") + 1)
        self.refresh_from_db(fields=["version"])


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        SCOREKEEPER = "scorekeeper", "Scorekeeper"

    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=12, choices=Role.choices)

    class Meta:
        unique_together = [("tournament", "user")]


def _token():
    return secrets.token_urlsafe(24)


class Invitation(models.Model):
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=12, choices=Membership.Role.choices, default=Membership.Role.ADMIN)
    token = models.CharField(max_length=64, unique=True, default=_token)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)


class Category(models.Model):
    FORMATS = [
        ("rr", "Round robin"),
        ("se", "Single elimination"),
        ("de", "Double elimination"),
        ("swiss", "Swiss ladder"),
        ("rr_playoff", "Round robin → playoffs"),
        ("custom", "Custom"),
    ]
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=80)
    order = models.PositiveIntegerField(default=0)
    format_kind = models.CharField(max_length=12, choices=FORMATS, blank=True)
    format_params = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["order", "id"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Team(models.Model):
    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=80)
    short_name = models.CharField(max_length=12, blank=True)
    seed = models.PositiveIntegerField(null=True, blank=True)
    contact = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        unique_together = [("category", "name")]

    def __str__(self):
        return self.name


class Stage(models.Model):
    class Kind(models.TextChoices):
        RR = "rr", "Round robin"
        SE = "se", "Single elimination"
        DE = "de", "Double elimination"
        SWISS = "swiss", "Swiss"

    category = models.ForeignKey(Category, on_delete=models.CASCADE, related_name="stages")
    name = models.CharField(max_length=80)
    order = models.PositiveIntegerField(default=1)
    kind = models.CharField(max_length=6, choices=Kind.choices)
    config = models.JSONField(default=dict, blank=True)
    tiebreakers = models.JSONField(default=list, blank=True)
    game_minutes = models.PositiveIntegerField(null=True, blank=True)
    advanced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.category} · {self.name}"

    @property
    def is_bracket(self):
        return self.kind in (self.Kind.SE, self.Kind.DE)

    @property
    def duration(self):
        return self.game_minutes or self.category.tournament.default_game_minutes


class Pool(models.Model):
    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="pools")
    name = models.CharField(max_length=60)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name

    @property
    def short(self):
        if self.name.startswith("Pool "):
            return self.name[5:].strip()[:3]
        return (self.name[:2] or str(self.order + 1)).upper()


class Entry(models.Model):
    """A slot in a pool/flight: filled directly or by advancement (SPEC §3.6)."""
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="entries")
    position = models.PositiveIntegerField()
    team = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="entries")
    source_pool = models.ForeignKey(Pool, on_delete=models.SET_NULL, null=True, blank=True, related_name="feeds")
    source_stage = models.ForeignKey(Stage, on_delete=models.SET_NULL, null=True, blank=True, related_name="feeds")
    source_rank = models.PositiveIntegerField(null=True, blank=True)
    locked = models.BooleanField(default=False)

    class Meta:
        ordering = ["position"]
        unique_together = [("pool", "position")]

    @property
    def has_source(self):
        return bool(self.source_rank and (self.source_pool_id or self.source_stage_id))

    @property
    def source_label(self):
        if self.source_pool_id and self.source_rank:
            return f"{self.source_pool.name} #{self.source_rank}"
        if self.source_stage_id and self.source_rank:
            return f"{self.source_stage.name} overall #{self.source_rank}"
        return ""

    @property
    def label(self):
        if self.team_id:
            return self.team.name
        return self.source_label or f"Seed {self.position}"


class Venue(models.Model):
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="venues")
    name = models.CharField(max_length=100)
    address = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name


class Space(models.Model):
    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name="spaces")
    name = models.CharField(max_length=60)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["venue__order", "venue_id", "order", "id"]

    def __str__(self):
        return self.name

    @property
    def full_name(self):
        return f"{self.venue.name} · {self.name}"


class TimeWindow(models.Model):
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="windows")
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["date", "start_time"]


class Game(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        FORFEIT = "forfeit", "Forfeit"
        CANCELLED = "cancelled", "Cancelled"

    RESOLVED = (Status.COMPLETED, Status.FORFEIT, Status.CANCELLED)

    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name="games")
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="games")
    number = models.PositiveIntegerField(default=0)
    bracket = models.CharField(max_length=1, blank=True)
    round = models.PositiveIntegerField(default=1)
    index = models.PositiveIntegerField(default=1)
    code = models.CharField(max_length=20, blank=True)
    label = models.CharField(max_length=80, blank=True)

    home_entry = models.ForeignKey(Entry, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    away_entry = models.ForeignKey(Entry, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    home_prev = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    away_prev = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    home_prev_outcome = models.CharField(max_length=1, blank=True)
    away_prev_outcome = models.CharField(max_length=1, blank=True)
    home_team = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    away_team = models.ForeignKey(Team, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    home_bye = models.BooleanField(default=False)
    away_bye = models.BooleanField(default=False)
    manual_teams = models.BooleanField(default=False)
    place_winner = models.PositiveIntegerField(null=True, blank=True)
    place_loser = models.PositiveIntegerField(null=True, blank=True)
    if_necessary = models.BooleanField(default=False)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED)
    home_score = models.IntegerField(null=True, blank=True)
    away_score = models.IntegerField(null=True, blank=True)
    winner_side = models.CharField(max_length=1, blank=True)  # H, A, D
    forfeit_side = models.CharField(max_length=1, blank=True)  # side that forfeited
    note = models.CharField(max_length=300, blank=True)

    space = models.ForeignKey(Space, on_delete=models.SET_NULL, null=True, blank=True, related_name="games")
    start_at = models.DateTimeField(null=True, blank=True)
    duration = models.PositiveIntegerField(default=60)
    queue_order = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["stage__order", "id"]

    def __str__(self):
        return f"#{self.number} {self.home_label} vs {self.away_label}"

    # -- presentation helpers -------------------------------------------------
    def _side_label(self, side):
        team = getattr(self, f"{side}_team")
        if team:
            return team.name
        if getattr(self, f"{side}_bye"):
            return "BYE"
        entry = getattr(self, f"{side}_entry")
        if entry:
            return entry.label
        prev = getattr(self, f"{side}_prev")
        if prev:
            word = "Winner" if getattr(self, f"{side}_prev_outcome") == "W" else "Loser"
            return f"{word} {prev.code or '#' + str(prev.number)}"
        return "TBD"

    @property
    def home_label(self):
        return self._side_label("home")

    @property
    def away_label(self):
        return self._side_label("away")

    @property
    def is_bye(self):
        return self.home_bye or self.away_bye

    @property
    def is_resolved(self):
        return self.status in self.RESOLVED

    @property
    def has_result(self):
        return self.status in (self.Status.COMPLETED, self.Status.FORFEIT)

    @property
    def teams_known(self):
        return bool(self.home_team_id and self.away_team_id)

    @property
    def end_at(self):
        return self.start_at + timedelta(minutes=self.duration) if self.start_at else None

    @property
    def winner(self):
        if not self.has_result:
            return None
        return {"H": self.home_team, "A": self.away_team}.get(self.winner_side)

    @property
    def loser(self):
        if not self.has_result:
            return None
        return {"H": self.away_team, "A": self.home_team}.get(self.winner_side)

    @property
    def title(self):
        return self.label or self.code or f"Game {self.number}"


class HomeBlock(models.Model):
    KINDS = [
        ("text", "Text"),
        ("info", "Important information"),
        ("venues", "Venues"),
        ("categories", "Categories"),
        ("sponsors", "Sponsors"),
        ("links", "Links"),
        ("image", "Image"),
    ]
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="blocks")
    kind = models.CharField(max_length=12, choices=KINDS, default="text")
    title = models.CharField(max_length=120, blank=True)
    body = models.TextField(blank=True)
    data = models.JSONField(default=list, blank=True)  # [{"label":..., "url":...}]
    image = models.FileField(upload_to="blocks/", blank=True)
    enabled = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]


class Activity(models.Model):
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name="activity")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    verb = models.CharField(max_length=30)
    text = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name_plural = "activity"
