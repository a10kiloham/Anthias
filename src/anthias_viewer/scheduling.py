import logging
import secrets
from datetime import datetime
from os import path
from typing import Any

from django.utils import timezone

from anthias_server.app.models import Asset
from anthias_server.app.playlist_eval import (
    WINDOWED_DEADLINE_CAP_SECONDS,
    Occurrence,
    evaluate_playlist,
)
from anthias_server.settings import settings

# The windowed-cap constant now lives in playlist_eval (single copy for
# both evaluators) but stays importable from here for its existing
# consumers (tests, and any operator tooling poking the viewer).
__all__ = [
    'WINDOWED_DEADLINE_CAP_SECONDS',
    'Scheduler',
    'generate_asset_list',
    'get_specific_asset',
]

logger = logging.getLogger(__name__)

_sysrandom = secrets.SystemRandom()


def get_specific_asset(asset_id: str) -> dict[str, Any] | None:
    logger.info('Getting specific asset')
    try:
        result: dict[str, Any] = Asset.objects.get(asset_id=asset_id).__dict__
        return result
    except Asset.DoesNotExist:
        logger.debug('Asset %s not found in database', asset_id)
        return None


def _asset_to_dict(asset: Asset) -> dict[str, Any]:
    return {
        k: v for k, v in asset.__dict__.items() if k not in ['_state', 'md5']
    }


def _occurrence_to_dict(occurrence: Occurrence) -> dict[str, Any]:
    """The plain dict the viewer consumes: the asset's fields plus the
    occurrence identity and the repeat-once bookkeeping keys.

    ``occurrence_id`` (not ``asset_id``) is the playlist-slot identity:
    the same asset can appear several times in the flattened list, and
    the shuffle membership guard needs to tell those slots apart.
    """
    result = _asset_to_dict(occurrence.asset)
    result['occurrence_id'] = occurrence.occurrence_id
    result['no_repeat_playlist_ids'] = occurrence.no_repeat_playlist_ids
    return result


def generate_asset_list() -> tuple[list[dict[str, Any]], datetime | None]:
    """Build the playlist plus a deadline for the next re-evaluation.

    Expansion, filtering and deadline computation live in the shared
    ``anthias_server.app.playlist_eval`` module (one copy for this
    live path and the ``GET /api/v2/viewer/playlist`` shim); this
    wrapper converts the occurrences to the plain dicts the viewer
    consumes and applies the device shuffle setting.
    """
    logger.info('Generating asset-list...')

    occurrences, deadline = evaluate_playlist(timezone.now())
    playlist = [_occurrence_to_dict(o) for o in occurrences]

    if settings['shuffle_playlist']:
        _sysrandom.shuffle(playlist)

    logger.debug(
        'generate_asset_list: %d occurrences, deadline %s',
        len(playlist),
        deadline,
    )
    return playlist, deadline


class Scheduler:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        logger.debug('Scheduler init')
        self.assets: list[dict[str, Any]] = []
        self.counter: int = 0
        self.current_asset_id: str | None = None
        self.deadline: datetime | None = None
        self.extra_asset: str | None = None
        self.index: int = 0
        self.reverse: bool = False
        self.last_update_db_mtime: float = 0
        # Play-once bookkeeping for repeat=False playlists. Runtime
        # state, deliberately not persisted: a viewer restart starts a
        # fresh play-through, matching how shuffle order and playlist
        # position already reset. Keyed by playlist_id; values are
        # occurrence_ids already shown (``_played``) and the full
        # occurrence membership of that playlist in the current
        # evaluated list (``_members`` — the reset detector).
        self._played: dict[str, set[str]] = {}
        self._members: dict[str, set[str]] = {}
        self.update_playlist()

    def _occurrence_played_out(self, asset: dict[str, Any]) -> bool:
        """True if this occurrence sits under a repeat=False playlist
        and has already been shown in the current play-through."""
        return any(
            asset.get('occurrence_id') in self._played.get(playlist_id, ())
            for playlist_id in asset.get('no_repeat_playlist_ids') or ()
        )

    def _mark_played(self, asset: dict[str, Any]) -> None:
        occurrence_id = asset.get('occurrence_id')
        if occurrence_id is None:
            return
        for playlist_id in asset.get('no_repeat_playlist_ids') or ():
            self._played.setdefault(playlist_id, set()).add(occurrence_id)

    def get_next_asset(self) -> dict[str, Any] | None:
        logger.debug('get_next_asset')

        if self.extra_asset is not None:
            asset = get_specific_asset(self.extra_asset)
            if asset and not asset['is_processing']:
                self.current_asset_id = self.extra_asset
                self.extra_asset = None
                return asset
            # An operator asked to jump to a specific asset that has
            # since been deleted or is still processing — a benign race
            # between their action and the asset's state, not a bug.
            # Warning (not error) so it doesn't page Sentry (ANTHIAS-3V).
            logger.warning(
                'Requested asset %s not found or still processing; '
                'falling back to the playlist',
                self.extra_asset,
            )
            self.extra_asset = None

        self.refresh_playlist()
        logger.debug('get_next_asset after refresh')
        if not self.assets:
            self.current_asset_id = None
            return None

        # Walk at most one full cycle looking for an occurrence that
        # hasn't played out its repeat=False budget. Everything played
        # out -> blank screen until an activation flip or a DB edit
        # resets the play-once state — that IS the semantics of a
        # non-repeating playlist with nothing else scheduled.
        current_asset: dict[str, Any] | None = None
        for _ in range(len(self.assets)):
            if self.reverse:
                idx = (self.index - 2) % len(self.assets)
                self.index = (self.index - 1) % len(self.assets)
                self.reverse = False
            else:
                idx = self.index
                self.index = (self.index + 1) % len(self.assets)

            if settings['shuffle_playlist'] and self.index == 0:
                self.counter += 1

            candidate = self.assets[idx]
            if not self._occurrence_played_out(candidate):
                current_asset = candidate
                break

        if current_asset is None:
            logger.debug(
                'get_next_asset: all occurrences played out '
                '(repeat=False playlists exhausted)'
            )
            self.current_asset_id = None
            return None

        logger.debug(
            'get_next_asset counter %s returning asset %s of %s',
            self.counter,
            idx + 1,
            len(self.assets),
        )

        self._mark_played(current_asset)
        self.current_asset_id = current_asset.get('asset_id')
        return current_asset

    def refresh_playlist(self) -> None:
        logger.debug('refresh_playlist')
        time_cur = timezone.now()

        logger.debug(
            'refresh: counter: (%s) deadline (%s) timecur (%s)',
            self.counter,
            self.deadline,
            time_cur,
        )

        if self.get_db_mtime() > self.last_update_db_mtime:
            logger.debug('updating playlist due to database modification')
            self.update_playlist()
        elif settings['shuffle_playlist'] and self.counter >= 5:
            # End-of-cycle reshuffle: the current play-through is over,
            # so it's safe to take the freshly shuffled order.
            self.update_playlist(allow_reshuffle=True)
        elif self.deadline and self.deadline <= time_cur:
            self.update_playlist()

    def update_playlist(self, *, allow_reshuffle: bool = False) -> None:
        logger.debug('update_playlist')
        self.last_update_db_mtime = self.get_db_mtime()
        (new_assets, new_deadline) = generate_asset_list()
        self._sync_play_once_state(new_assets)

        if settings['shuffle_playlist'] and not allow_reshuffle:
            # generate_asset_list() reshuffles on every call, so list
            # equality would always fail and disrupt the play-through
            # whenever the cap-driven refresh fires (~60s for windowed
            # assets). Compare by membership only here; legitimate
            # reshuffles (end-of-cycle, counter >= 5) opt in via
            # allow_reshuffle. Keyed on occurrence_id, not asset_id:
            # the same asset can hold several slots in the flattened
            # playlist, and each slot is its own membership entry.
            current_ids = sorted(a['occurrence_id'] for a in self.assets)
            new_ids = sorted(a['occurrence_id'] for a in new_assets)
            if current_ids == new_ids:
                # Membership unchanged: preserve current order, but
                # refresh each dict so DB-driven field edits (duration,
                # uri, etc.) take effect on the next get_next_asset().
                new_by_id = {a['occurrence_id']: a for a in new_assets}
                self.assets = [
                    new_by_id[a['occurrence_id']] for a in self.assets
                ]
                self.deadline = new_deadline
                return
        elif new_assets == self.assets and new_deadline == self.deadline:
            # Shuffle off: list equality is meaningful, so a no-op
            # refresh shouldn't disturb the current play-through.
            return

        self.assets, self.deadline = new_assets, new_deadline
        self.counter = 0
        # Try to keep the same position in the play list. E.g., if a new asset
        # is added to the end of the list, we don't want to start over from
        # the beginning.
        self.index = self.index % len(self.assets) if self.assets else 0
        logger.debug(
            'update_playlist done, count %s, counter %s, index %s, deadline %s',
            len(self.assets),
            self.counter,
            self.index,
            self.deadline,
        )

    def _sync_play_once_state(self, new_assets: list[dict[str, Any]]) -> None:
        """Reset play-once bookkeeping where it no longer applies.

        A repeat=False playlist's ``_played`` set survives ordinary
        refreshes (a 60s cap tick must not restart a finished
        play-through) and resets when:

          - its occurrence membership changes (operator edited the
            playlist — a new play-through with the new content), or
          - it leaves the evaluated list entirely (its activation
            window closed — the next window starts a fresh
            play-through).
        """
        new_members: dict[str, set[str]] = {}
        for asset in new_assets:
            occurrence_id = asset.get('occurrence_id')
            if occurrence_id is None:
                continue
            for playlist_id in asset.get('no_repeat_playlist_ids') or ():
                new_members.setdefault(playlist_id, set()).add(occurrence_id)

        for playlist_id in list(self._played):
            if new_members.get(playlist_id) != self._members.get(playlist_id):
                del self._played[playlist_id]
        self._members = new_members

    def get_db_mtime(self) -> float:
        # Newest mtime across the SQLite database and its WAL sidecars.
        #
        # Since the DB is opened with journal_mode=WAL (#3015), commits
        # are written to ``anthias.db-wal`` and ``anthias.db-shm``; the
        # main ``anthias.db`` file's mtime stays frozen until a (rare)
        # checkpoint. Stat'ing only the main file therefore never sees
        # an asset add/edit, so refresh_playlist() never reloads — most
        # visibly, the first asset on a fresh install never displays
        # (its empty playlist has no deadline fallback either). Take the
        # max over all three so a write bumps the value regardless of
        # journal mode: WAL commits move ``-wal``/``-shm``, and a
        # checkpoint moves the main file.
        database = settings['database']
        if not database:
            return 0

        newest = 0.0
        for suffix in ('', '-wal', '-shm'):
            try:
                mtime = path.getmtime(database + suffix)
            except OSError:
                continue
            newest = max(newest, mtime)
        return newest
