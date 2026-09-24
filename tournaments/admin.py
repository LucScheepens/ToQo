from django.contrib import admin

from . import models

for m in (models.Tournament, models.Membership, models.Invitation, models.Category, models.Team,
          models.Stage, models.Pool, models.Entry, models.Venue, models.Space, models.TimeWindow,
          models.Game, models.HomeBlock, models.Activity):
    admin.site.register(m)
