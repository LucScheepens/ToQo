from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.text import slugify

from .models import Category, HomeBlock, Membership, Space, Team, TimeWindow, Tournament, Venue


class SignupForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ["username", "email"]

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email


DATE = forms.DateInput(attrs={"type": "date"})
TIME = forms.TimeInput(attrs={"type": "time"}, format="%H:%M")


class TournamentCreateForm(forms.ModelForm):
    class Meta:
        model = Tournament
        fields = ["name", "sport", "start_date", "end_date", "location", "visibility"]
        widgets = {"start_date": DATE, "end_date": DATE}

    def clean(self):
        data = super().clean()
        s, e = data.get("start_date"), data.get("end_date")
        if s and e and e < s:
            self.add_error("end_date", "The end date can't be before the start date.")
        return data

    def unique_slug(self):
        base = slugify(self.cleaned_data["name"])[:60] or "tournament"
        slug, i = base, 2
        while Tournament.objects.filter(slug=slug).exists():
            slug = f"{base}-{i}"
            i += 1
        return slug


class TournamentSettingsForm(TournamentCreateForm):
    class Meta(TournamentCreateForm.Meta):
        fields = ["name", "slug", "sport", "start_date", "end_date", "location", "tagline", "banner",
                  "visibility", "schedule_mode", "points_win", "points_draw", "points_loss", "allow_draws",
                  "forfeit_win_score", "forfeit_loss_score"]
        help_texts = {"slug": "Public address: /t/<slug>/", "allow_draws": "Round robin and Swiss only."}

    def clean_banner(self):
        f = self.cleaned_data.get("banner")
        if f and hasattr(f, "name") and not f.name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            raise forms.ValidationError("Upload a PNG, JPG, GIF or WebP image.")
        return f


class ScheduleSettingsForm(forms.ModelForm):
    class Meta:
        model = Tournament
        fields = ["schedule_mode", "default_game_minutes", "buffer_minutes", "min_rest_minutes",
                  "slot_interval_minutes"]
        labels = {"default_game_minutes": "Game length (min)", "buffer_minutes": "Buffer between games (min)",
                  "min_rest_minutes": "Minimum team rest (min)", "slot_interval_minutes": "Slot interval (min)"}
        help_texts = {"slot_interval_minutes": "Empty = game length + buffer."}


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["name"]


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ["name", "short_name", "seed", "contact"]


class BulkTeamsForm(forms.Form):
    names = forms.CharField(widget=forms.Textarea(attrs={"rows": 6, "placeholder": "One team per line"}))


class FormatForm(forms.Form):
    kind = forms.ChoiceField(choices=Category.FORMATS, widget=forms.RadioSelect)
    pools = forms.IntegerField(min_value=1, initial=2, required=False, label="Number of pools")
    cycles = forms.ChoiceField(choices=[(1, "Single round robin"), (2, "Double round robin")], initial=1,
                               required=False, label="Round robin")
    games_per_team = forms.IntegerField(min_value=1, required=False, label="Games per team (optional)",
                                        help_text="Leave empty for a full round robin.")
    advance_per_pool = forms.IntegerField(min_value=1, initial=2, required=False, label="Teams advancing per pool")
    playoff_kind = forms.ChoiceField(choices=[("se", "Single elimination"), ("de", "Double elimination")],
                                     required=False, label="Playoff bracket")
    placement = forms.ChoiceField(choices=[("none", "No placement games"), ("third", "3rd-place game"),
                                           ("all", "Play out every place")], required=False,
                                  label="Placement games")
    consolation = forms.BooleanField(required=False, label="Consolation flight for the next teams in each pool")
    grand_final_reset = forms.BooleanField(required=False, initial=True,
                                           label="Grand final reset (if the losers-bracket team wins)")
    rounds = forms.IntegerField(min_value=1, initial=3, required=False, label="Swiss rounds")
    confirm = forms.BooleanField(required=False)


class StageForm(forms.Form):
    name = forms.CharField(max_length=80)
    game_minutes = forms.IntegerField(min_value=5, required=False, label="Game length override (min)")
    cycles = forms.ChoiceField(choices=[(1, "Single"), (2, "Double")], required=False, label="Round robin")
    games_per_team = forms.IntegerField(min_value=1, required=False)
    placement = forms.ChoiceField(choices=FormatForm.base_fields["placement"].choices, required=False)
    grand_final_reset = forms.BooleanField(required=False)
    rounds = forms.IntegerField(min_value=1, required=False, label="Swiss rounds")


class AddStageForm(forms.Form):
    name = forms.CharField(max_length=80, initial="Playoffs")
    kind = forms.ChoiceField(choices=[("rr", "Round robin"), ("se", "Single elimination"),
                                      ("de", "Double elimination"), ("swiss", "Swiss")])
    pools = forms.IntegerField(min_value=1, max_value=26, initial=1, label="Pools / flights")
    slots = forms.IntegerField(min_value=2, max_value=128, initial=4, label="Teams per pool / flight")


class VenueForm(forms.ModelForm):
    class Meta:
        model = Venue
        fields = ["name", "address"]


class SpaceForm(forms.ModelForm):
    class Meta:
        model = Space
        fields = ["name"]


class TimeWindowForm(forms.ModelForm):
    class Meta:
        model = TimeWindow
        fields = ["date", "start_time", "end_time"]
        widgets = {"date": DATE, "start_time": TIME, "end_time": TIME}

    def clean(self):
        data = super().clean()
        if data.get("start_time") and data.get("end_time") and data["end_time"] <= data["start_time"]:
            self.add_error("end_time", "End must be after start.")
        return data


class HomeBlockForm(forms.ModelForm):
    links = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 4}),
                            help_text="Sponsors/links: one per line as “Label | https://url”.")

    class Meta:
        model = HomeBlock
        fields = ["kind", "title", "body", "image", "enabled"]
        widgets = {"body": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.data:
            self.initial["links"] = "\n".join(
                f"{d.get('label', '')} | {d.get('url', '')}".strip(" |") for d in self.instance.data)

    def clean_links(self):
        out = []
        for line in self.cleaned_data.get("links", "").splitlines():
            if not line.strip():
                continue
            label, _, url = line.partition("|")
            url = url.strip()
            if url and not url.startswith(("http://", "https://")):
                raise forms.ValidationError(f"“{url}” must start with http:// or https://")
            out.append({"label": label.strip(), "url": url})
        return out

    def save(self, commit=True):
        self.instance.data = self.cleaned_data.get("links", [])
        return super().save(commit)


class InviteForm(forms.Form):
    email = forms.EmailField()
    role = forms.ChoiceField(choices=[(Membership.Role.ADMIN, "Admin"), (Membership.Role.SCOREKEEPER, "Scorekeeper")])


class ResultForm(forms.Form):
    home_score = forms.IntegerField(min_value=0)
    away_score = forms.IntegerField(min_value=0)
    winner_side = forms.ChoiceField(choices=[("", "—"), ("H", "Home"), ("A", "Away")], required=False)
    confirm = forms.BooleanField(required=False)
