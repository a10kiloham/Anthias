# Feasibility: playlists as a first-class concept

Assessment only — no implementation. Target: multiple playlists, each
containing assets and apps, assets appearing in several playlists and
more than once within one, playlists nestable, each playlist
independently enable/disable-able and schedulable.

## Verdict

**Feasible, but the four requirements have wildly uneven costs.**

| Requirement | Cost | Risk |
|---|---|---|
| Playlists contain **apps** as well as assets | **Zero** | none |
| **M:N** assets + duplicates within a playlist | ~1–1.5 wk | low |
| Per-playlist **scheduling** | moderate | **high** |
| **Nested** playlists | large | moderate |

Flat playlists alone are ~1–1.5 weeks. Adding nesting and scheduling
roughly doubles-to-triples that. Counter-intuitively, the two
requirements framed as "preferable" and "standard" are together most of
the cost and effectively all of the risk — see §Apps, §Scheduling and
§Nesting below for why.

## Apps: already free

Nothing to do. **An app is not a separate entity** — it is an ordinary
`Asset` row with `mimetype='webpage'` plus a `metadata['app']` blob
(`app/views.py:447-481`). It lands in the same `assets` table with a
`play_order`, and the viewer has no app awareness at all: dispatch
branches purely on mimetype (`anthias_viewer/__init__.py:2002-2027`),
and grepping `metadata['app']` across the viewer returns nothing.

The install view's docstring says so outright (`app/views.py:389-398`):

> "We persist it as an ordinary `webpage` asset — so playback,
> scheduling and the viewer treat it like any other web page — but
> stamp `metadata.app`…"

So any playlist mechanism that holds assets holds apps automatically.
The only app-specific UI gap is unrelated and pre-existing: an
installed app is visually indistinguishable from a hand-added webpage
in the asset table — there's no badge.

## The framing

Anthias today has **exactly one implicit playlist**: every
`is_enabled` asset, ordered by `Asset.play_order`. The feature is
"generalise 1 → N". Nothing needs to be invented; an existing concept
needs a name and a table.

This is stated as a deliberate design decision in two places, so
changing it is a real (but acknowledged) reversal:

- `src/anthias_server/app/helpers.py:132-136`
- `src/anthias_server/api/views/v2.py:460-462`

  > "The playlist has no item entity — the Asset row *is* the playlist
  > slot — so scheduling the same media twice means cloning."

Your fork's `71e3b349` ("allow scheduling the same asset multiple
times") is a workaround for precisely that limitation — it hardlinks a
duplicate Asset row to fake a second playlist slot. That commit is the
strongest argument for doing this properly.

## The model

M:N with duplicates and nesting settles the shape: a **polymorphic
through-table**, where an item points at *either* an asset or a
sub-playlist.

```
Playlist(id, name, is_enabled,
         start_date, end_date, play_days, play_time_from, play_time_to)

PlaylistItem(playlist FK,          # the containing playlist
             asset FK NULL,        # exactly one of these two is set
             child_playlist FK NULL,
             position INT)         # NOT unique on (playlist, asset)
```

`play_order` **moves off `Asset` onto `PlaylistItem.position`**. That
relocation is the single most invasive mechanical change; every writer
is enumerated in §Blast radius. Dropping the uniqueness assumption is
what buys duplicates-within-a-playlist.

This retires the fork's `duplicate_asset()` hack (`71e3b349`): the same
asset can sit in three playlists, or twice in one, with zero file
duplication and no hardlink/orphan-sweep machinery.

### The unifying frame: flatten to occurrences

Evaluate the tree at playback time into a flat list of **occurrences**,
each a `(path, asset)` pair carrying the intersection of every window
along its path:

- an asset in two playlists → two occurrences
- an asset twice in one playlist → two occurrences
- an asset inside a nested playlist → one occurrence per reaching path
- `play_order` → DFS pre-order position in the flattened list

**This is the key to bounding the work.** Once flattened, the runtime is
exactly today's flat model — an ordered list of things with windows —
just generated recursively. `generate_asset_list()` keeps its shape;
only its input changes. Nesting and duplication stop being runtime
concerns and become expansion-time concerns.

### The sub-decision that sets the nesting cost

**Can a sub-playlist have more than one parent?**

- **Tree (one parent per playlist) — recommended.** Each playlist node
  has a single ancestry chain, so its effective schedule is
  *single-valued*: AND up its one path, computed once. Cycle prevention
  collapses to "no self-ancestor" at edit time. Assets remain M:N, so
  everything asked for still works.
- **DAG (sub-playlists M:N too).** A node becomes reachable by multiple
  paths with *different* effective windows, so playlist-level schedule
  goes path-dependent and cycle detection needs full graph traversal.

Tree-for-playlists + M:N-for-assets delivers every stated requirement
while keeping each node's schedule single-valued — which is precisely
what keeps the deadline machinery (§Scheduling) tractable. DAG is
strictly more expensive; take it only if genuinely needed.

## Scheduling: the semantics are cheap, the machinery is not

The semantics are exactly as expected — an occurrence plays iff every
window along its path AND the asset's own window admit `now`. No
argument there, and the activeness half is nearly free.

The cost is split unevenly across two layers, and it's worth being
precise about which is which.

**Cheap — the activeness check.** `Asset.is_active()`
(`app/models.py:288`) is a single choke point every consumer funnels
through: `api/helpers.py:107,120,145`, `serializers/v2.py:137-147`,
`asset_filters.py:298`, `api/views/v1_2.py:86`,
`anthias_viewer/__init__.py:1848`, and both playlist builders. ANDing a
container window there propagates everywhere for free. (Needs a
`select_related` on the two candidate queries — `scheduling.py:55-59`,
`v2.py:765-769` — to avoid an N+1 across the playlist.)

**Expensive — the re-evaluation machinery.** The deadline logic reads
`start_date`/`end_date`/`has_window_filter()` **directly, bypassing
`is_active()`**, so it inherits none of that propagation. These must
each be edited explicitly:

- `_compute_deadline()` — `scheduling.py:79`
- `_compute_viewer_deadline()` — `v2.py:782` *(duplicate of the above)*
- `generate_asset_list()` — `scheduling.py:36`
- `_evaluate_viewer_playlist()` — `v2.py:752`
- `has_window_filter()` — `models.py:274`

Two spots need design rather than a mechanical edit:

1. **Boundary selection doesn't generalise.** `scheduling.py:95` /
   `v2.py:799`:
   ```python
   boundary = asset.end_date if is_active else asset.start_date
   ```
   Under ANDed windows the next flip is a `min` over every window on
   the path. The active branch is tractable (whichever closes first).
   The **inactive branch is genuinely ambiguous**: inactive because a
   container hasn't started, because the asset hasn't started, or
   because a day/time window blocks? The right "soonest re-eval"
   differs per cause. Get it wrong and you either miss a transition or
   reintroduce the always-overdue bug the comments at
   `scheduling.py:86-89` exist to prevent. **This is the single
   riskiest spot in the whole feature**, and nesting multiplies the
   "why is it inactive?" cases by depth.

2. **The 60s cap gate must become a disjunction.**
   `scheduling.py:102-107` / `v2.py:802-808` must read "asset *or any
   ancestor* has a window filter, and the **intersected** date range
   contains `now`" — intersected, not the asset's own. An asset inside
   its own dates but outside its container's can't flip on a window
   boundary and shouldn't force 60s polling.

**Prerequisite, not a nice-to-have:** extract the shared filter +
deadline logic into one helper that both `scheduling.py` and `v2.py`
call, *before* layering nesting or container windows on top. There are
currently two full copies of the deadline algorithm and three copies of
the 60s constant (`scheduling.py:15`, `v2.py:747`,
`tests/test_viewer_api.py:35`). Layering onto that guarantees drift.

## Blast radius

### 1. `play_order` writers — the main mechanical cost

Every one of these moves from `Asset` to `PlaylistItem`:

| Site | What it does |
|---|---|
| `api/helpers.py:123-125` `save_active_assets_ordering()` | the **only** bulk-order writer; becomes playlist-scoped |
| `api/helpers.py:114-120` `get_active_asset_ids()` | needs a playlist filter |
| `api/helpers.py:128-151` `finalize_asset_update()` | re-inserts edited asset at its order |
| `app/page_context.py:328-329` | the sort that drives the UI |
| `app/helpers.py:177-192` `duplicate_asset()` | order-shift logic; mostly deleted — occurrences replace it |
| `app/views.py:297-319`, `:466-477`, `:698-711` | three new-asset "append to end" sites |
| `app/helpers.py:79` | default sample assets |

Good news: `save_active_assets_ordering()` being the single bulk writer,
and the htmx reorder view delegating straight to it (`app/views.py:993-1002`),
means the drag-reorder UI and the REST API share one code path. One fix
covers both.

### 2. Runtime gate — must land in TWO places

This is the easiest thing to under-scope. There are two independent
playlist evaluators that must not diverge:

- **`src/anthias_viewer/scheduling.py:36` `generate_asset_list()`** —
  the **live** path. The Python viewer imports the Django model
  directly (`scheduling.py:9`) and detects change by stat'ing the
  SQLite file's mtime (`scheduling.py:240`). Add
  `playlist__is_enabled=True` to the queryset at `scheduling.py:59`.
- **`api/views/v2.py:818` `ViewerPlaylistViewV2`** (`GET
  /api/v2/viewer/playlist`) — a server-side reimplementation intended
  for the C++ viewer, **not yet adopted** (GH #2906 Phase 3; see the
  comment at `views/v2.py:745`). It duplicates the active-filter and
  deadline logic and needs the identical gate.

For flat playlists these are single-line filter additions; with nesting
they become the recursive expansion described in §The model. Either
way, missing the second one produces a bug that only appears after
Phase 3 lands. Extracting one shared helper (see §Scheduling) is the
fix.

No raw SQL exists against the `assets` table anywhere — all access is
ORM. That removes an entire class of migration risk.

Change propagation needs **no new plumbing, even for nesting**:
`get_db_mtime()` (`scheduling.py:240-265`) stats `anthias.db`, `-wal`
and `-shm` and takes the max, so a write to *any* table in that file —
including new `Playlist` / `PlaylistItem` rows — is picked up by the
poll the viewer already runs on every asset transition.

### 2b. Nesting-specific work

Beyond the expansion itself:

- **Cycle detection.** Edit-time check ("no self-ancestor" under the
  tree model; full traversal under DAG) *plus* a defensive depth cap at
  evaluation time, so a hand-edited or migrated row can't hang the
  viewer.
- **Depth cap** on expansion, with defined behaviour on breach (drop
  the subtree and log, rather than partial output).
- **Empty playlists become load-bearing.** `_compute_deadline()`
  returns `None` when nothing qualifies and has no fallback — already
  the source of one past bug (see the comment at
  `scheduling.py:243-252`). Enable/disable plus nesting makes empty and
  fully-disabled subtrees common rather than exotic.
- **Shuffle × nesting is an unresolved design question.** Shuffle
  within each node, or across the whole flattened list? The existing
  membership-comparison guard (`scheduling.py:204-220`) that stops the
  60s windowed re-evaluation from scrambling play-through order assumes
  a flat list of asset_ids; occurrences need a stable identity for that
  guard to keep working. Name it early — it is cheap to decide and
  expensive to retrofit.
- **`asset_filters.py:270-310`** — the `schedule_window` chip has
  `disabled`/`upcoming`/`expired`/`scheduled`/`live` kinds. A
  container-blocked asset currently has no distinguishable state and
  would silently render as "off-window now". Operators need to see
  *which* ancestor blocked it, or scheduling becomes undebuggable.
- **`page_context.py:304-320`** — its docstring explicitly argues
  against using `is_active()` for the operator-facing Active/Inactive
  split. That reasoning applies doubly with containers.

### 3. API back-compat — four versions to keep working

The flat asset list is baked into v1, v1.1, v1.2 and v2. Recommended
contract:

- Flat `GET /assets` endpoints keep returning the **union across all
  playlists** — unchanged response shape.
- Writes without a playlist target land in the **Default** playlist.
- `play_order` in the existing serializers (`serializers/__init__.py:106-123`,
  `v2.py:196-219`) becomes ambiguous — an asset now has *N* positions,
  one per occurrence. Simplest rule: report the Default playlist's
  position, or drop the field from v2 in favour of the new playlist
  endpoints. Whatever you pick, document it; this is the most likely
  place an existing v1 integration breaks.
- Playlists get **additive** endpoints under v2 only:
  `GET/POST /v2/playlists`, `GET/PATCH/DELETE /v2/playlists/<id>`,
  `POST /v2/playlists/<id>/items`, `POST /v2/playlists/<id>/order`.
  Nothing new in v1/v1.1/v1.2.

`POST /assets/order` exists in v1 and v2 only (`urls/v1.py:22`,
`urls/v2.py:33`, both via `PlaylistOrderViewMixin`). Ambiguous once
playlists exist — keep it operating on Default.

### 4. UI

Server-rendered Django templates + htmx + Alpine. There is **no
React** — `static/src` is plain TypeScript, and the React references
in comments (`app/views.py:227`, `:994-995`, `page_context.py:315`)
are vestigial. Drag-reorder is hand-rolled pointer events
(`app/static/src/home.ts:634-694`), posting via `postOrder`
(`home.ts:602-631`).

Practically this means: no state-management layer to rework. Flat
playlists are cheap here — a selector/grouping in `_asset_table.html`,
an enable toggle, and scoping `data-order-url` per playlist so
`postOrder` targets the right list. The 5s htmx poll
(`_asset_table.html:10-13`) refreshes it all for free.

Bulk enable/disable already exists in the app views only, not the REST
API (`app/views.py:1022-1100`) — an "add selection to playlist" bulk
action slots into the same `_bulk_action_bar.html` pattern.

**Nesting is where the UI stops being cheap.** The hand-rolled flat
pointer-event drag handler (`home.ts:634-694`) does not extend to a
tree: dragging *between* levels, reordering a subtree, and showing
effective (inherited) schedule state per row are all new interaction
design, not adaptation. Budget UI work as a real line item once nesting
is in scope, and expect this to be the part that gets iterated on after
first contact with operators.

## Risks

Roughly in order of how much trouble each can cause:

1. **The inactive-boundary ambiguity in `_compute_deadline()`** — see
   §Scheduling. Pre-existing bug class, multiplied by depth. Gets
   things wrong *silently and intermittently* — content that fails to
   appear at 09:00 and nobody knows why. Highest-value place to put
   tests, ideally property-based over generated schedule trees.
2. **The two evaluators diverging** (`scheduling.py` / `v2.py`) —
   already duplicated today, with three copies of the 60s constant.
   Unify before extending, not after.
3. **Occurrence identity vs shuffle** — the membership-comparison guard
   at `scheduling.py:204-220` keeps 60s windowed re-evaluation from
   scrambling play-through order. It assumes flat asset_ids;
   occurrences need a stable key or shuffle regressions appear only on
   long-running devices.
4. **Migration of live devices** — must be idempotent and must not
   strand assets outside any playlist; a stranded asset silently stops
   playing.
5. **Fork/upstream merge friction** — this touches files upstream is
   actively changing (`views/v2.py`, `page_context.py`, `helpers.py`,
   `scheduling.py`). You already carry a 5-commit delta; at this scope
   you are effectively forking the scheduler. Worth deciding
   deliberately whether this is a permanent divergence or something to
   propose upstream.
6. **`duplicate_asset()` retirement** — occurrences supersede it.
   Leaving both mechanisms live is the worst outcome.

## Suggested phasing

Ordered so each phase is shippable and the risky work lands on a clean
foundation.

0. **Unify the duplicated evaluators.** Extract filter + deadline into
   one helper called by both `scheduling.py` and `v2.py`. No behaviour
   change; pure prerequisite. Do this first regardless of what follows.
1. **Model + migration.** `Playlist` / `PlaylistItem`, backfill a
   "Default" playlist containing every existing asset at its current
   `play_order`. No UI. Verify viewer behaviour is byte-identical.
2. **Flat runtime gate.** `is_enabled` on playlists, wired through the
   unified helper. Test: disabling Default empties the screen.
3. **API + UI for flat playlists.** Additive v2 endpoints, back-compat
   tests pinning v1/v1.1/v1.2 shapes, playlist CRUD and grouping in the
   asset table.
4. **Nesting.** Polymorphic items, recursive expansion to occurrences,
   cycle detection, depth cap. Tree UI.
5. **Per-playlist scheduling.** Container windows ANDed up the path,
   plus the deadline rework. Deliberately last — it is the riskiest
   work and benefits most from everything above being settled.
6. **Cleanup.** Retire `duplicate_asset()`.

Phases 0–3 deliver most of the practical value and carry most of the
low-risk work. **If you want to de-risk this, ship 0–3, live with it
for a while, then decide whether 4 and 5 are still worth it** — nesting
and scheduling are the expensive half, and flat playlists may cover
more real use cases than expected.

## Future, explicitly out of scope

- Playlist priority or interleaving.
- DAG-shaped nesting (multiple parents per playlist) — see §The model.
- An app badge in the asset table (pre-existing gap, unrelated but
  cheap and worth folding into the UI phase).
- Importers: all four upstream providers (Yodeck, ScreenCloud,
  PiSignage, Xibo) **have** playlists that Anthias currently flattens
  away — `grep -i playlist lib/integrations/*.py` returns zero hits.
  Once a `Playlist` entity exists, importing them becomes possible.
  Attractive, but not part of this estimate.
