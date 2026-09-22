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
# time-of-day boundaries don't name an exact instant, so a polling cap
# ensures transitions are picked up.
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
    """An enabled asset. Rows failing this can never become active
    without an operator edit (which the DB-mtime poll picks up), so
    they contribute neither playback nor deadline."""
    return bool(occurrence.asset.is_enabled)


def evaluate_playlist(
    now: datetime | None = None,
) -> tuple[list[Occurrence], datetime | None]:
    """Active occurrences (in DFS play order, unshuffled) plus the
    soonest future moment the playlist might need re-evaluating.

    Active filter and deadline computation share a single ``now`` so
    the two can't disagree across a midnight tick. Shuffle is the
    caller's business: the viewer and the API shuffle their own copies
    (with their own RNGs), and the viewer's membership guard needs the
    unshuffled list anyway.
    """
    if now is None:
        now = timezone.now()

    candidates = [o for o in expand_occurrences() if _is_candidate(o)]
    active = [o for o in candidates if _occurrence_is_active(o, now)]

    deadline = compute_deadline(candidates, now)
    return active, deadline


def compute_deadline(
    occurrences: list[Occurrence],
    now: datetime,
) -> datetime | None:
    """Soonest future moment when the playlist might need re-evaluating.

    With no date-based expiration, the only boundaries that can flip an
    occurrence on their own are day-of-week / time-of-day windows, and
    those don't name an exact instant — so the deadline is simply
    ``now + WINDOWED_DEADLINE_CAP_SECONDS`` when any candidate
    occurrence with a fully-enabled path carries a window filter
    anywhere on it, and ``None`` otherwise (operator edits are picked
    up by the DB-mtime poll, not the deadline).
    """
    for occurrence in occurrences:
        path_enabled = all(p.is_enabled for p in occurrence.path)
        occurrence_has_window = occurrence.asset.has_window_filter() or any(
            p.has_window_filter() for p in occurrence.path
        )
        if occurrence_has_window and path_enabled:
            return now + timedelta(seconds=WINDOWED_DEADLINE_CAP_SECONDS)
    return None
