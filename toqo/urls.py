from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from tournaments.views import api, public, run, setup

m = "manage/<slug:slug>/"

urlpatterns = [
    path("", setup.home, name="home"),
    path("admin/", admin.site.urls),
    path("accounts/login/", auth_views.LoginView.as_view(), name="login"),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("accounts/signup/", setup.signup, name="signup"),
    path("invite/<str:token>/", setup.invite_accept, name="invite"),

    # organizer
    path("manage/new/", setup.tournament_new, name="m_new"),
    path(m, setup.dashboard, name="m_dashboard"),
    path(m + "settings/", setup.settings_view, name="m_settings"),
    path(m + "categories/", setup.categories, name="m_categories"),
    path(m + "teams/", setup.teams, name="m_teams"),
    path(m + "teams/<int:team_id>/", setup.team_edit, name="m_team_edit"),
    path(m + "format/", setup.format_overview, name="m_format"),
    path(m + "format/<int:cat_id>/", setup.format_category, name="m_format_category"),
    path(m + "format/<int:cat_id>/add-stage/", setup.category_add_stage, name="m_add_stage"),
    path(m + "stages/<int:stage_id>/", setup.stage_detail, name="m_stage"),
    path(m + "venues/", setup.venues, name="m_venues"),
    path(m + "schedule/", run.schedule, name="m_schedule"),
    path(m + "games/<int:game_id>/", run.game_detail, name="m_game"),
    path(m + "standings/", run.standings, name="m_standings"),
    path(m + "queue/", run.queue, name="m_queue"),
    path(m + "home/", setup.home_blocks, name="m_home"),
    path(m + "admins/", setup.admins, name="m_admins"),
    path(m + "review/", setup.review, name="m_review"),
    path(m + "share/", setup.share, name="m_share"),
    path(m + "activity/", setup.activity, name="m_activity"),

    # public site
    path("t/<slug:slug>/", public.home, name="public_home"),
    path("t/<slug:slug>/teams/", public.teams, name="public_teams"),
    path("t/<slug:slug>/teams/<int:team_id>/", public.team_detail, name="public_team"),
    path("t/<slug:slug>/schedule/", public.schedule, name="public_schedule"),
    path("t/<slug:slug>/standings/", public.standings, name="public_standings"),
    path("t/<slug:slug>/qr.svg", public.qr, name="public_qr"),

    # API
    path("api/t/<slug:slug>/", api.tournament, name="api_tournament"),
    path("api/t/<slug:slug>/version/", api.version, name="api_version"),
    path("api/t/<slug:slug>/schedule/", api.schedule, name="api_schedule"),
    path("api/t/<slug:slug>/standings/", api.standings, name="api_standings"),
    path("api/manage/<slug:slug>/games/<int:game_id>/result/", api.submit_result, name="api_result"),
    path("api/manage/<slug:slug>/games/<int:game_id>/move/", api.move_game, name="api_move"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
