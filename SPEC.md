# ToQo — Technical Functional Specification

ToQo is a web application for creating, publishing and running sports tournaments.
Organizers configure an event (categories, teams, format, stages, venues, schedule),
publish it, and then run it live (results → standings → advancement → brackets →
champion). Participants follow a public, read-only tournament site.

This document is the source of truth for the implementation in this repository.
Section numbers are referenced from code comments as `SPEC §n`.

---

## 1. Architecture

| Layer | Technology | Location |
|---|---|---|
| Tournament engine (pure, framework-free) | Python 3.11 | `engine/` |
| Persistence, domain services | Django 5 ORM, SQLite (any Django DB works) | `tournaments/models.py`, `tournaments/services/` |
| Organizer UI + public site | Django templates, vanilla JS, one CSS file | `tournaments/views/`, `templates/`, `static/` |
| JSON API | Django views returning JSON | `tournaments/views/api.py` |
| Auth | Django auth (username/email + password) | `accounts/` |

Principles

1. **Results are the source of truth.** Standings, rankings, advancement, bracket
   progression and placements are *derived* from game results plus rules. They are
   never edited directly (the one escape hatch is a locked manual override of an
   advancement slot, §9.4).
2. **Connected structure.** Stages are connected by *entries* (slots) whose source is
   "rank N of pool P". Bracket games are connected by "winner/loser of game G". A
   single `recompute_category()` pass re-derives everything after any change.
3. **Change safety.** Any change that would invalidate already-recorded results (e.g.
   editing a pool result after teams were advanced and played) is first executed as a
   dry run inside a rolled-back transaction; the organizer sees exactly which results
   would be cleared and must confirm.
4. **Local time.** `USE_TZ = False`; all datetimes are the tournament's local wall time.

---

## 2. Roles & permissions

| Role | Scope |
|---|---|
| Owner | Everything, incl. admins management, publishing, deleting the tournament |
| Admin | Everything except admin management and deletion |
| Scorekeeper | Schedule/run pages: submit & edit results, game notes, queue board |
| Public visitor | Public pages of a published tournament (public or unlisted) |

A tournament has exactly one owner (creator). Owners invite by email. If a user with
that email exists they're added immediately; otherwise an invitation link is created
(`/invite/<token>/`) that the invitee accepts after signing in/up.

---

## 3. Data model

```
Tournament 1─* Membership *─1 User
Tournament 1─* Invitation
Tournament 1─* Category 1─* Team
                       1─* Stage 1─* Pool 1─* Entry (slot) ──source──> Pool (earlier stage)
                                  │          └─team──> Team
                                  1─* Game ──home/away entry──> Entry
                                            ──home/away prev──> Game (winner/loser)
                                            ──space──> Space
Tournament 1─* Venue 1─* Space
Tournament 1─* TimeWindow
Tournament 1─* HomeBlock
Tournament 1─* Activity
```

### 3.1 Tournament
| Field | Type | Notes |
|---|---|---|
| name | str(120) | required |
| slug | slug, unique | public URL `/t/<slug>/` |
| sport | enum | volleyball, soccer, basketball, badminton, squash, tennis, pickleball, hockey, baseball, softball, ultimate, other. Drives the space noun (Court/Field/Rink/Diamond) |
| start_date, end_date | date | end ≥ start |
| location, tagline | str | optional |
| banner | file | optional image |
| visibility | enum | `public` (listed), `unlisted` (link only), `private` (admins only) |
| status | enum | `draft → published → in_progress → completed` (§4) |
| schedule_mode | enum | `timed` (date/time/space per game) or `queue` (next available space, §11.4) |
| points_win / points_draw / points_loss | int | default 3/1/0 |
| allow_draws | bool | RR/Swiss only; elimination games always need a winner |
| forfeit_win_score / forfeit_loss_score | int | score recorded for forfeits (default 1/0) |
| default_game_minutes, buffer_minutes, min_rest_minutes, slot_interval_minutes | int | scheduling defaults |
| version | int | incremented on every change; drives live updates (§14) |
| published_at, completed_at | datetime | |

### 3.2 Category
`name`, `order`, `format_kind` (`rr`, `se`, `de`, `swiss`, `rr_playoff`, `custom`, or blank),
`format_params` (JSON). A tournament always has ≥ 1 category; a default "Open" category
is created with the tournament. Teams in different categories never meet.

### 3.3 Team
`category`, `name` (unique within category), `short_name`, `seed` (nullable positive int),
`contact` (optional), `order`.

### 3.4 Stage ("round" in the functional description)
| Field | Notes |
|---|---|
| category, name, order | e.g. "Pool Play" (1), "Playoffs" (2) |
| kind | `rr` round robin, `se` single elimination, `de` double elimination, `swiss` |
| config (JSON) | rr: `cycles` (1–2), `games_per_team` (null = full); se: `placement` (`none`/`third`/`all`); de: `grand_final_reset` (bool); swiss: `rounds` |
| tiebreakers (JSON list) | ordered subset of §8.2 |
| game_minutes | nullable override of tournament default |
| advanced_at | set when the transition out of this stage has been confirmed |

Derived **stage status**: `waiting` (a slot has no team yet) → `not_started` →
`in_progress` → `complete` (every non-bye game is completed/forfeit/cancelled) →
`transitioned` (`advanced_at` set).

### 3.5 Pool
Group within a stage. In RR/Swiss it is a pool ("Pool A"); in brackets it is a
**flight** ("Championship", "Consolation"). `name`, `order`.

### 3.6 Entry (slot)
| Field | Notes |
|---|---|
| pool, position | position = seed within pool (1-based) |
| team | nullable; filled directly (first stage) or by a transition |
| source_pool + source_rank | "rank N in pool P" (earlier stage) |
| source_stage + source_rank | "N-th best across all pools of stage S" (e.g. best 3rd-placed) |
| locked | manual override; transitions don't overwrite it |

### 3.7 Game
| Field | Notes |
|---|---|
| stage, pool, number | `number` = tournament-wide "Game #n" |
| bracket | `''` (RR/Swiss), `W` winners, `L` losers, `F` grand final, `P` placement |
| round, index, code, label | e.g. round 2, "QF1", "Quarterfinal 1" |
| home_entry / away_entry | side source: slot |
| home_prev + home_prev_outcome (`W`/`L`), same for away | side source: another game |
| home_team / away_team | resolved team (cache, written by resolver) |
| home_bye / away_bye | resolved bye flags |
| manual_teams | set by "Change teams"; resolver leaves sides untouched |
| place_winner / place_loser | final placements decided by this game (e.g. 1/2, 3/4) |
| if_necessary | grand-final reset game |
| status | `scheduled`, `in_progress`, `completed`, `forfeit`, `cancelled` |
| home_score, away_score, winner_side (`H`/`A`/`D`), forfeit_side | result |
| note | organizer note shown publicly |
| space, start_at, duration | timed schedule |
| queue_order, started_at, finished_at | queue mode |

A side with neither entry nor prev (and not manual) is a **BYE**. A game with a BYE side
is a *bye game*: never scheduled, auto-advances the other side; its loser is BYE.

### 3.8 Venue / Space / TimeWindow
Venue: `name`, `address`. Space: `venue`, `name` ("Court 1"). TimeWindow: `date`,
`start_time`, `end_time` — availability of all spaces. (Per-space availability is a
listed extension, §17.)

### 3.9 HomeBlock
`kind` (`text`, `info`, `venues`, `categories`, `sponsors`, `links`, `image`), `title`,
`body` (plain text, line breaks kept), `data` (JSON: links/sponsors lists), `image`,
`enabled`, `order`.

### 3.10 Activity
`tournament`, `user`, `verb`, `text`, `created_at` — audit trail of results, transitions,
publishing, shown on the dashboard.

---

## 4. State machines

**Tournament**: `draft` —publish (validation passes, §12)→ `published` —first result→
`in_progress` —organizer "Finish tournament" (all stages complete)→ `completed`.
`published → draft` (unpublish) is allowed only while no results exist. "Ready" is the
derived state *draft + setup checklist complete*.

**Game**: `scheduled → in_progress → completed | forfeit`; `scheduled → cancelled`
(RR/Swiss only; elimination games must be decided or forfeited). "Clear result"
returns to `scheduled`. In the queue mode `in_progress` means "on a court now".

**Stage**: see §3.4. **Team advancement**: in pool → ranked → qualified (preview) →
advanced (entry filled) → playing in next stage.

---

## 5. Setup flow and dependencies

```
Tournament info → Categories → Teams → Format → Stages (pools/brackets/advancement)
→ Venues/Spaces → Schedule → Review → Publish → Share
```

Dependency rules (enforced by services, surfaced as confirm dialogs):

| Change | Effect |
|---|---|
| Add/remove team, or change seeds, after a format exists | Category format is regenerated from stored params; all games of that category are rebuilt and **unscheduled**. Requires confirmation. Blocked if the category has results unless explicitly confirmed (results are lost). |
| Change format | Same as above. |
| Change stage config (pool assignment, bracket options, games per team) | That stage's games are rebuilt and unscheduled; downstream entries keep their sources. |
| Add/remove venues/spaces | Games on a deleted space become unscheduled. |
| Edit/clear a result | `recompute_category`; if any *other* recorded result would be invalidated (team changed), show list + confirm (§9.5). |

The dashboard checklist marks each step ✓ when: info (always), categories (≥1),
teams (every category ≥2 teams), format (every category has stages), stages (all slots
have a team or a valid source), venues (≥1 space), schedule (timed mode: every playable
game has a time and space and no conflicts; queue mode: always ✓), review (published).

---

## 6. Formats (templates)

Format selection per category generates stages, pools, entries and games. Parameters
are stored on the category so they can be regenerated.

| Kind | Params | Generated |
|---|---|---|
| Round robin (`rr`) | pools, cycles, games_per_team | 1 RR stage |
| Single elimination (`se`) | placement | 1 SE stage, flight "Bracket", entries = teams by seed |
| Double elimination (`de`) | grand_final_reset | 1 DE stage |
| Swiss (`swiss`) | rounds | 1 Swiss stage |
| RR → Playoffs (`rr_playoff`) | pools, cycles, advance_per_pool, playoff_kind (se/de), placement, consolation | RR stage + bracket stage with a Championship flight (top *k* per pool) and optionally a Consolation flight (next *k* per pool) |
| Custom | — | empty; stages are added manually with any kind/pool count/slot count and any advancement sources |

Team distribution into pools uses **snake seeding** (seed ascending, unseeded last in
list order): A B C D D C B A …

Playoff seeding from pools interleaves by rank then pool: A1, B1, A2, B2, … which with
standard bracket seeding produces cross-over matchups (A1–B4, B1–A4, A2–B3, B2–A3).

---

## 7. Game generation (engine)

### 7.1 Round robin — `engine.roundrobin`
Circle method. Odd pool sizes get a phantom team (bye, no game). Home/away alternates
per round. `cycles=2` repeats with home/away swapped. `games_per_team=k` keeps only the
first *k* rounds (each team plays *k* games; with an odd pool some play *k−1*).

### 7.2 Single elimination — `engine.bracket.single_elimination(n, placement)`
Bracket size = next power of two; standard seed order (1v8, 4v5, 2v7, 3v6 for 8);
seeds > n are byes. Round labels: Final, Semifinal, Quarterfinal, Round of N.
Placement: `third` adds a 3rd-place game; `all` recursively plays out every place
(losers of a round with *k* games form a sub-bracket for places `offset+k+1 … offset+2k`).

### 7.3 Double elimination — `engine.bracket.double_elimination(n, reset)`
Winners bracket as 7.2. Losers bracket for size N=2^k:
LB round 1 pairs WB-R1 losers; for r = 2..k a *drop-in* round (LB survivors vs WB-round-r
losers, order reversed on even rounds to reduce rematches) followed, when r < k, by an
*internal* round. Grand final: WB champion (home) vs LB champion. With reset, a second
"if necessary" game is played only when the LB champion wins game 1; otherwise it is
auto-cancelled as not required.

### 7.4 Swiss — `engine.swiss.pair_round`
Round 1: top half vs bottom half by seed. Later rounds: teams ordered by current
standings; greedy pairing with backtracking avoids rematches (falls back to allowing a
rematch only if no rematch-free pairing exists). Odd count: lowest-ranked team without
a previous bye gets a bye (counts as a win, no points for/against). Placeholder games
for every round are created up front so the whole event can be scheduled in advance;
round *r* is paired automatically when round *r−1* is complete. A round that already has
results is never re-paired.

---

## 8. Standings

### 8.1 Table columns
GP, W, D, L, PF, PA, PD, Pts (and Buchholz for Swiss). Forfeit: counted as W/L with the
forfeit scores. Cancelled: ignored. Bye (Swiss): +1 W and win points, but no PF/PA and
not counted in GP (GP counts played and forfeited games only).

### 8.2 Tiebreakers (ordered, configurable per stage)
`points`, `wins`, `win_pct`, `head_to_head` (points in games among the tied teams),
`h2h_point_diff`, `point_diff`, `points_for`, `points_against` (fewer is better),
`buchholz` (sum of opponents' points), `seed`.

Algorithm: sort by the first criterion, split into groups of equal value, recurse into
each group > 1 with the remaining criteria (head-to-head is evaluated *within the current
tied group*). After all configured criteria, remaining ties are broken by seed, then name,
and marked `tie_unresolved` — the UI flags these so the organizer can override (§9.4).

Default tiebreakers: RR `[points, head_to_head, point_diff, points_for]`;
Swiss `[points, buchholz, point_diff, points_for]`.

### 8.3 Bracket placements
Explicit from placement games (final 1/2, 3rd place 3/4, …). Otherwise teams still alive
rank above eliminated teams; eliminated teams rank by how late they were eliminated
(shared rank, e.g. both semifinal losers "3"). DE: grand final decides 1/2, LB final
loser 3, and so on. Final category standings = last stage flights stacked
(Championship places 1..k, Consolation k+1..).

---

## 9. Transitions (advancement)

1. When a stage is **complete**, the Standings page shows "Advance teams" with a
   preview: every downstream slot sourced from this stage, and the team that currently
   qualifies for it. Qualifying positions decided by an unresolved tie are highlighted.
2. Organizer confirms → slots are filled, `advanced_at` is set, bracket sides resolve,
   Swiss round 1 is paired. Activity is logged.
3. A stage cannot be advanced while any game is unresolved; unplayed games must be
   given a result, forfeited, or cancelled (RR/Swiss).
4. **Manual override**: an entry's team can be set manually and locked (e.g. coin toss).
5. **Changes after advancement**: if a result in an advanced stage is edited, the
   mapping is recomputed. Unchanged mapping → nothing else happens. Changed mapping →
   slots are refilled and every downstream result whose teams changed is cleared (after
   confirmation). If the stage becomes incomplete (result cleared), its downstream
   slots are emptied (after confirmation if they had results).

---

## 10. Results & game options

| Action | Rules |
|---|---|
| Submit result | scores ≥ 0; if equal and draws not allowed (or elimination game), a winner must be picked (overtime/shootout) |
| Edit result | same form; triggers recompute + §9.5 confirmation |
| Forfeit | pick forfeiting side; forfeit scores recorded |
| Cancel | RR/Swiss only; no points to either side |
| Clear result | back to `scheduled`; recompute |
| Note | free text, shown on schedule |
| Change home team | swaps sources, teams, scores |
| Change teams | pick any two teams of the category; sets `manual_teams` |
| Reschedule | set date/time/space (timed) |

Every change bumps `tournament.version` and writes an Activity row. First result moves
the tournament to `in_progress`.

---

## 11. Venues & scheduling

### 11.1 Inputs
Spaces, time windows (date + start/end), game duration (tournament default, per-stage
override), buffer between games on a space, minimum rest per team, slot interval
(grid step, default = duration + buffer).

### 11.2 Generator — `engine.scheduler.schedule`
Greedy list scheduling:
1. Playable (non-bye) games are ordered by (stage order, dependency depth, round, pool,
   index) so pools progress in parallel and brackets follow feeders.
2. For each game: `earliest = max(end of dependency games + rest, last end of each team
   + rest)`. Dependencies: feeder games (winner/loser sources) and *all* games of the
   source pool for slots filled by a transition (unknown teams are represented by a
   placeholder key per slot so a slot is never double-booked).
3. Scan grid start times ≥ earliest across windows; take the first time that has a free
   space for `[start, start + duration + buffer)`.
4. Games that fit nowhere are returned as *unscheduled* with the message: add time,
   add spaces, shorten games or relax rest.
Options: "keep already scheduled games" (fixed) or "rebuild all". Game numbers are
reassigned by start time afterwards.

### 11.3 Manual editing & conflicts
Drag a game card onto a grid cell (drop on an occupied cell swaps the two games), drag
to the tray to unschedule, or edit time/space in the game dialog. Conflict detection
flags: space overlap, team double-booked, team rest violation, game before its feeder
ends, game outside time windows. Conflicts show on the grid and block the checklist.

### 11.4 Queue mode (next available space)
Games have a queue order (same ordering as 11.2). The queue board shows each space with
its current game. "Start next" assigns the first *ready* game (both teams known, not a
bye, neither team currently playing) to a free space; submitting the result frees the
space and automatically starts the next ready game there.

---

## 12. Review & publish

Validation errors (block publishing): no teams in a category, category < 2 teams,
category without format, slot without team or source, source pointing to a
missing/later stage. Warnings (don't block): unscheduled games, schedule conflicts,
empty home page. Payment is an integration point (`services.publishing.ensure_paid`) that
is a no-op in this build. Publishing sets `status=published`, `published_at`.
Preview links render the public pages for admins before publishing.

---

## 13. Public site

`/t/<slug>/` Home (enabled blocks in order, champions when completed) ·
`/t/<slug>/teams/` · `/t/<slug>/teams/<id>/` (team schedule & results) ·
`/t/<slug>/schedule/` (by day, filter category/team/space) ·
`/t/<slug>/standings/` (pool tables, brackets, final standings) ·
`/t/<slug>/qr.svg`. Draft/private tournaments return 404 to non-admins. Unlisted are
reachable by link only; public ones are listed on the landing page.

## 14. Live updates
Public and organizer run pages poll `GET /api/t/<slug>/version/` every 15 s; when the
version changes the page's `<main>` is re-fetched and swapped in place.

## 15. JSON API

Public (read, same visibility rules as the site):
| Method | Path | Returns |
|---|---|---|
| GET | `/api/t/<slug>/version/` | `{version, status}` |
| GET | `/api/t/<slug>/` | tournament, categories, stages, pools, teams |
| GET | `/api/t/<slug>/schedule/` | games with teams, scores, status, space, time |
| GET | `/api/t/<slug>/standings/` | per stage/pool tables or placements |

Organizer (session auth + CSRF header):
| Method | Path | Body |
|---|---|---|
| POST | `/api/manage/<slug>/games/<id>/result/` | `{home_score, away_score, winner_side?, confirm?}` → `{ok}` or `{needs_confirmation, affected[]}` |
| POST | `/api/manage/<slug>/games/<id>/move/` | `{space_id, start}` or `{unschedule: true}` |

## 16. Screens (organizer)

| Screen | Path | Main elements |
|---|---|---|
| My tournaments | `/` | list, "New tournament" |
| New tournament | `/manage/new/` | name, sport, dates, visibility |
| Dashboard | `/manage/<slug>/` | checklist, status, current stage per category, games done/total, next actions, activity |
| Settings | `/manage/<slug>/settings/` | info, banner, scoring, delete |
| Categories | `/manage/<slug>/categories/` | add, rename, reorder, delete |
| Teams | `/manage/<slug>/teams/` | per category: add, bulk add (one per line), edit, seed, delete |
| Format | `/manage/<slug>/format/<cat>/` | format picker + parameters, regenerate with confirmation |
| Stage | `/manage/<slug>/stages/<id>/` | name, kind options, tiebreakers, duration, slots (team or source, lock), move team between pools, games list |
| Venues | `/manage/<slug>/venues/` | venues, spaces |
| Schedule | `/manage/<slug>/schedule/` | settings, time windows, generate, day grid (drag & drop), unscheduled tray, conflicts, list view with result entry |
| Game | `/manage/<slug>/games/<id>/` | result form, options (§10) |
| Queue board | `/manage/<slug>/queue/` | spaces, current games, start next, finish |
| Standings | `/manage/<slug>/standings/` | tables, brackets, advance preview/confirm, finish tournament |
| Home page | `/manage/<slug>/home/` | blocks: add, edit, enable, reorder |
| Admins | `/manage/<slug>/admins/` | members, invite, remove |
| Review & publish | `/manage/<slug>/review/` | validation, preview links, publish/unpublish |
| Share | `/manage/<slug>/share/` | URL, QR code |

## 17. Out of scope / extension points
Payments (stub), email delivery (console backend), set-by-set scoring, per-space
availability windows, team/player self-registration, participant score submission,
i18n. The model and services are structured so each can be added without changing the
engine.
