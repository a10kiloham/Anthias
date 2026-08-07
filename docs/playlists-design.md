# Design: playlists as first-class items

Implementation companion to `playlists-feasibility.md`. That document
establishes feasibility, blast radius, and phasing; this one records the
decisions taken for the implementation.

## Decisions

| Question | Decision | Rationale |
|---|---|---|
| Nesting shape | **Tree** (a playlist has at most one parent) | Single-valued effective schedule per node; cycle check is "no self-ancestor" (feasibility §The model) |
| Item model | Polymorphic `PlaylistItem` (`asset` XOR `child_playlist`), `position` int, duplicates allowed for assets | M:N + duplicates-within-a-playlist |
| Single parent enforcement | Unique constraint on `PlaylistItem.child_playlist` + edit-time ancestor check | Keeps one interleaved ordered item list per playlist |
| Repeat semantics | `Playlist.repeat` bool, **default True** (loop forever — today's behaviour). `repeat=False`: each occurrence under that playlist plays once per activation window, then the subtree is held out until the playlist's active window flips or membership changes | User requirement: "repeat unless otherwise edited" |
| Playlist scheduling fields | Same window vocabulary as `Asset` (`start_date`, `end_date`, `play_days`, `play_time_from`, `play_time_to`) but dates are **nullable = unbounded** (unlike assets, which require both) | A bare playlist should not need dates to play |
| Effective activeness | Occurrence plays iff asset `is_active()` AND every ancestor playlist admits `now` | Feasibility §Scheduling |
| Occurrence identity | Slash-joined `PlaylistItem` PKs along the path (`"12/7/3"`) | Stable across evaluations; feeds the shuffle membership guard |
| Deadline generalisation | Active occurrence → `min(end_date over path)`. Inactive → if any window on the path has already ended: no candidate; else `max(future start_dates)` (the moment all date windows are open). 60s cap fires iff any node on the path has a day/time window AND the **intersected** date range contains `now` | Resolves the inactive-boundary ambiguity flagged as the top risk |
| `play_order` back-compat | Column stays on `Asset`, mirrored from the asset's first occurrence position in the **Default** playlist by `save_active_assets_ordering()`. v1/v1.1/v1.2/v2 serializers unchanged | "Never break the REST API" |
| Untargeted writes | Asset create paths (API + HTML + apps + defaults) append a `PlaylistItem` to the Default playlist | Feasibility §API back-compat |
| Default playlist | `is_default=True`, created by the backfill migration with one item per existing asset at its `play_order`; not deletable via API/UI | Migration must strand no asset |
| Root ordering | `Playlist.position` orders root playlists; flattening walks roots in that order | Deterministic full-screen order |
| Cycle/depth safety | Edit-time "no self-ancestor" check + defensive `MAX_PLAYLIST_DEPTH = 8` at expansion (drop subtree + log) | A hand-edited row must not hang the viewer |
| Shuffle × nesting | Shuffle across the whole flattened occurrence list (matches today's whole-playlist shuffle); membership guard keys on occurrence ids | Cheapest coherent answer; per-node shuffle deferred |
| `duplicate_asset()` | Kept working (it just clones a row that lands in Default). Retirement deferred until occurrences prove out | Endpoint contract unchanged |
| New API surface | v2 only: `GET/POST /api/v2/playlists`, `GET/PATCH/DELETE /api/v2/playlists/<id>`, `POST /api/v2/playlists/<id>/items`, `DELETE .../items/<item_id>`, `POST .../order` | Additive; nothing changes in v1.x |
| UI | New `/playlists` page (server-rendered + htmx/Alpine, same patterns as home): playlist list with nesting, per-playlist item table, add-asset/add-subplaylist, reorder, enable/repeat toggles, schedule editor | Keeps home untouched; tree drag-drop deferred in favour of explicit move controls |

## Shared evaluator (phase 0)

`src/anthias_server/app/playlist_eval.py` owns:

- `WINDOWED_DEADLINE_CAP_SECONDS = 60` (single copy)
- `evaluate_playlist(now) -> (list[Occurrence], datetime | None)` —
  expansion + active filter + deadline in one place.
- `Occurrence`: `(occurrence_id, asset, path_playlist_ids)`.

`anthias_viewer.scheduling.generate_asset_list()` and
`api.views.v2._evaluate_viewer_playlist()` both delegate to it. The
viewer converts occurrences to dicts (adding `occurrence_id`); the API
serialises the asset rows.

Non-repeat play-once state lives in the viewer `Scheduler` (per-device
runtime state, not DB): a `played` set of occurrence ids per
non-repeating playlist, reset when that playlist's activation flips or
its occurrence membership changes.
