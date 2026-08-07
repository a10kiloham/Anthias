"""Single home for playlist evaluation: active filter + deadline.

Both playlist evaluators — the live Python viewer
(``anthias_viewer.scheduling.generate_asset_list``) and the server-side
``GET /api/v2/viewer/playlist`` (``ViewerPlaylistViewV2``, intended for
the C++ viewer, GH #2906 Phase 3) — previously carried their own full
copy of the same filter + deadline algorithm, plus a third copy of the
60-second windowed cap in the tests. This module is the one copy both
call, so the two runtime paths cannot drift.
"""

import logging
from datetime import datetime, timedelta

from django.utils import timezone

from anthias_server.app.models import Asset

logger = logging.getLogger(__name__)

# Re-evaluate windowed playlists at most this often. Day-of-week and
# time-of-day boundaries don't show up in start_date/end_date, so we
# need a polling cap to ensure transitions are picked up.
WINDOWED_DEADLINE_CAP_SECONDS = 60


def get_playlist_candidates(now: datetime | None = None) -> list[Asset]:
    """Enabled, dated assets in play order — the rows a playlist build
    considers before the ``is_active()`` filter."""
    del now  # reserved for future container filtering
    return list(
        Asset.objects.filter(
            is_enabled=True,
            start_date__isnull=False,
            end_date__isnull=False,
        ).order_by('play_order')
    )


def evaluate_playlist(
    now: datetime | None = None,
) -> tuple[list[Asset], datetime | None]:
    """Active assets (in play order, unshuffled) plus the soonest future
    moment the playlist might need re-evaluating.

    Active filter and deadline computation share a single ``now`` so a
    row can't be filtered as active while its end_date is skipped as
    past in the deadline pass — the two would otherwise disagree across
    a midnight tick. Shuffle is the caller's business: the viewer and
    the API shuffle their own copies (with their own RNGs), and the
    viewer's membership guard needs the unshuffled list anyway.
    """
    if now is None:
        now = timezone.now()

    candidates = get_playlist_candidates(now)
    active_flags = [a.is_active(now=now) for a in candidates]
    active_assets = [a for a, ok in zip(candidates, active_flags) if ok]

    deadline = compute_deadline(candidates, active_flags, now)
    return active_assets, deadline


def compute_deadline(
    assets: list[Asset],
    active_flags: list[bool],
    now: datetime,
) -> datetime | None:
    """Soonest future moment when the playlist might need re-evaluating.

    Deadline is the soonest of:
      - any inactive asset's start_date,
      - any active asset's end_date,
      - now + WINDOWED_DEADLINE_CAP_SECONDS, if any asset has a window
        filter (those transitions don't show up in date columns).

    Past boundaries are dropped so a long-ago start_date on an asset
    that's currently inactive (e.g. blocked by its play_days filter)
    doesn't pin the deadline to "always overdue" and cause the caller's
    refresh loop to fire on every tick.
    """
    candidates: list[datetime] = []
    has_windowed = False

    for asset, is_active in zip(assets, active_flags):
        boundary = asset.end_date if is_active else asset.start_date
        if boundary and boundary > now:
            candidates.append(boundary)
        # Cap only matters while the asset is in its date range — the
        # day/time window can't change activeness before start_date or
        # after end_date, so future/expired windowed assets should rely
        # on their date boundary alone, not periodic polling.
        if (
            asset.has_window_filter()
            and asset.start_date is not None
            and asset.end_date is not None
            and asset.start_date < now < asset.end_date
        ):
            has_windowed = True

    if has_windowed:
        candidates.append(
            now + timedelta(seconds=WINDOWED_DEADLINE_CAP_SECONDS)
        )

    return min(candidates) if candidates else None
