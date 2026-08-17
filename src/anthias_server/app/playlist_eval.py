"""Single home for playlist evaluation: expansion, active filter and
deadline.

Both playlist evaluators — the live Python viewer
(``anthias_viewer.scheduling.generate_asset_list``) and the server-side
``GET /api/v2/viewer/playlist`` (``ViewerPlaylistViewV2``, intended for
the C++ viewer, GH #2906 Phase 3) — previously carried their own full
copy of the same filter + deadline algorithm, plus a third copy of the
60-second windowed cap in the tests. This module is the one copy both
call, so the two runtime paths cannot drift.

Playlists are evaluated by **flattening to occurrences**: the playlist
tree is walked depth-first (roots by ``position``, items by their
``PlaylistItem.position``) into an ordered list of
``(occurrence_id, asset, ancestor playlists)`` entries. An asset in two
playlists yields two occurrences; twice in one playlist, two
occurrences; inside a nested playlist, one occurrence per reaching
path. Once flattened, the runtime is exactly the pre-playlist flat
model — an ordered list of things with windows — so nesting and
duplication are expansion-time concerns, not runtime concerns.

An occurrence is active iff its asset's own ``is_active()`` admits
``now`` AND every ancestor playlist's window admits ``now``.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from anthias_server.app.models import (
    MAX_PLAYLIST_DEPTH,
    Asset,
    Playlist,
    PlaylistItem,
)

logger = logging.getLogger(__name__)

# Re-evaluate windowed playlists at most this often. Day-of-week and
# time-of-day boundaries don't show up in start_date/end_date, so we
# need a polling cap to ensure transitions are picked up.
WINDOWED_DEADLINE_CAP_SECONDS = 60


@dataclass
class Occurrence:
    """One playable slot in the flattened playlist.

    ``occurrence_id`` is the slash-joined chain of ``PlaylistItem``
    primary keys from root to the asset item (e.g. ``"12/7/3"``).
    ``PlaylistItem`` rows are stable across evaluations, so the id is a
    stable identity for the viewer's shuffle membership guard — asset_id
    alone can't distinguish two occurrences of the same asset.
    """

    occurrence_id: str
    asset: Asset
    # Root-first chain of ancestor playlists (root ... immediate parent).
    path: list[Playlist] = field(default_factory=list)

    @property
    def no_repeat_playlist_ids(self) -> list[str]:
        """Ids of ancestors with ``repeat=False``, root-first. The
        viewer's Scheduler uses these to hold a played-once subtree out
        of rotation until its activation window flips."""
        return [p.playlist_id for p in self.path if not p.repeat]


def expand_occurrences() -> list[Occurrence]:
    """Flatten the playlist tree into an ordered occurrence list.

    Two queries total (all playlists, all items), then a pure-Python
    DFS — the viewer calls this on every playlist rebuild, so it must
    not N+1 across the tree. Ordering is DFS pre-order: root playlists
    by (position, playlist_id), items within a playlist by
    (position, id).

    The tree shape is enforced at edit time; this walk stays defensive
    anyway: a cycle or over-deep chain in a hand-edited DB drops the
    offending subtree with a log line rather than hanging the viewer.
    """
    playlists = {p.playlist_id: p for p in Playlist.objects.all()}
    items_by_playlist: dict[str, list[PlaylistItem]] = {}
    for item in PlaylistItem.objects.select_related('asset').order_by(
        'position', 'id'
    ):
        items_by_playlist.setdefault(item.playlist_id, []).append(item)

    child_ids = {
        item.child_playlist_id
        for items in items_by_playlist.values()
        for item in items
        if item.child_playlist_id is not None
    }
    roots = sorted(
        (p for p in playlists.values() if p.playlist_id not in child_ids),
        key=lambda p: (p.position, p.playlist_id),
    )

    occurrences: list[Occurrence] = []

    def walk(
        playlist: Playlist,
        path: list[Playlist],
        item_path: list[int],
        seen: frozenset[str],
    ) -> None:
        if playlist.playlist_id in seen:
            logger.error(
                'Playlist cycle detected at %r (%s); dropping subtree',
                playlist.name,
                playlist.playlist_id,
            )
            return
        if len(path) >= MAX_PLAYLIST_DEPTH:
            logger.error(
                'Playlist nesting deeper than %d at %r (%s); dropping subtree',
                MAX_PLAYLIST_DEPTH,
                playlist.name,
                playlist.playlist_id,
            )
            return

        next_path = [*path, playlist]
        next_seen = seen | {playlist.playlist_id}
        for item in items_by_playlist.get(playlist.playlist_id, []):
            if item.asset is not None:
                occurrences.append(
                    Occurrence(
                        occurrence_id='/'.join(
                            str(pk) for pk in [*item_path, item.id]
                        ),
                        asset=item.asset,
                        path=next_path,
                    )
                )
            elif item.child_playlist_id is not None:
                child = playlists.get(item.child_playlist_id)
                if child is not None:
                    walk(
                        child,
                        next_path,
                        [*item_path, item.id],
                        next_seen,
                    )

    for root in roots:
        walk(root, [], [], frozenset())
    return occurrences


def _occurrence_is_active(occurrence: Occurrence, now: datetime) -> bool:
    return occurrence.asset.is_active(now=now) and all(
        p.admits(now=now) for p in occurrence.path
    )


def _is_candidate(occurrence: Occurrence) -> bool:
    """Mirror of the pre-playlist SQL candidate filter: enabled asset
    with both dates set. Rows failing this can never become active, so
    they contribute neither playback nor deadline."""
    asset = occurrence.asset
    return bool(
        asset.is_enabled
        and asset.start_date is not None
        and asset.end_date is not None
    )


def evaluate_playlist(
    now: datetime | None = None,
) -> tuple[list[Occurrence], datetime | None]:
    """Active occurrences (in DFS play order, unshuffled) plus the
    soonest future moment the playlist might need re-evaluating.

    Active filter and deadline computation share a single ``now`` so a
    row can't be filtered as active while its end_date is skipped as
    past in the deadline pass — the two would otherwise disagree across
    a midnight tick. Shuffle is the caller's business: the viewer and
    the API shuffle their own copies (with their own RNGs), and the
    viewer's membership guard needs the unshuffled list anyway.
    """
    if now is None:
        now = timezone.now()

    candidates = [o for o in expand_occurrences() if _is_candidate(o)]
    active_flags = [_occurrence_is_active(o, now) for o in candidates]
    active = [o for o, ok in zip(candidates, active_flags) if ok]

    deadline = compute_deadline(candidates, active_flags, now)
    return active, deadline


def compute_deadline(
    occurrences: list[Occurrence],
    active_flags: list[bool],
    now: datetime,
) -> datetime | None:
    """Soonest future moment when the playlist might need re-evaluating.

    Every occurrence carries a stack of windows — the asset's own dates
    plus each ancestor playlist's optional dates, all ANDed. Deadline is
    the soonest of, per occurrence:

      - **active** → the earliest close among the windows on its path
        (whichever date bound shuts first ends the occurrence);
      - **inactive** because some window hasn't opened yet (and none has
        permanently closed) → the latest future open — the first moment
        every date window admits ``now``. If any window on the path has
        already closed, the occurrence can never come back, so it
        contributes nothing (a permanently-past boundary must not pin
        the deadline to "always overdue"); if it's date-eligible but
        blocked by a day/time window or a disabled ancestor, the date
        columns can't name the flip — the windowed cap (below) or the
        operator's edit (DB-mtime poll) covers it;
      - now + WINDOWED_DEADLINE_CAP_SECONDS, if any node on some
        occurrence's path has a day/time window filter AND the
        **intersected** date range contains ``now`` (an occurrence
        outside its dates can't flip on a window boundary, so it must
        not force 60s polling).
    """
    candidates: list[datetime] = []
    has_windowed = False

    for occurrence, is_active in zip(occurrences, active_flags):
        asset = occurrence.asset
        starts = [asset.start_date] + [
            p.start_date for p in occurrence.path if p.start_date is not None
        ]
        ends = [asset.end_date] + [
            p.end_date for p in occurrence.path if p.end_date is not None
        ]
        # _is_candidate guarantees the asset's own bounds are set.
        assert asset.start_date is not None and asset.end_date is not None

        if is_active:
            candidates.append(min(d for d in ends if d is not None))
        elif all(d is not None and d > now for d in ends):
            future_starts = [d for d in starts if d is not None and d > now]
            if future_starts:
                candidates.append(max(future_starts))

        in_date_range = all(d is not None and d < now for d in starts) and all(
            d is not None and d > now for d in ends
        )
        path_enabled = all(p.is_enabled for p in occurrence.path)
        occurrence_has_window = asset.has_window_filter() or any(
            p.has_window_filter() for p in occurrence.path
        )
        if occurrence_has_window and in_date_range and path_enabled:
            has_windowed = True

    if has_windowed:
        candidates.append(
            now + timedelta(seconds=WINDOWED_DEADLINE_CAP_SECONDS)
        )

    # Guard against a candidate that is exactly ``now`` (or a hair
    # behind after the loop's own runtime) pinning the caller to an
    # always-overdue deadline.
    future = [d for d in candidates if d > now]
    return min(future) if future else None
