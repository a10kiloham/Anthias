import json
import re
import uuid
from datetime import datetime, time
from typing import TYPE_CHECKING, Any, ClassVar

from django.db import models
from django.utils import timezone

ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]

# Upper bound for ``Asset.metadata['refresh_interval_s']`` (seconds).
# 24h cap acts as a typo guard — anything beyond is almost certainly
# a units mistake — and is a hostile-input guard for the int math
# in the C++ webview's setReloadInterval (``seconds * 1000`` would
# otherwise overflow). Imported by the v2 serializer (write
# validation), the form handler (clamping), and mirrored by
# kMaxReloadIntervalS in src/anthias_webview/src/view.cpp.
REFRESH_INTERVAL_S_MAX = 86400


# Upper bound for ``Asset.duration`` (seconds). The hard constraint is
# the viewer: ``asset_loop`` / ``view_video`` feed the value straight
# into ``threading.Event.wait``, and a timeout past C ``PyTime_t``
# range (int64 nanoseconds, ~9.2e9 s ≈ 292 years) raises
# OverflowError, crash-looping the viewer
# (Sentry ANTHIAS-3E — an operator typed 9999999999999 to mean
# "forever" and took the screen down). One year is effectively
# "pinned forever" for signage while staying a typo guard. Enforced
# by the v2 serializers + settings (write validation), the
# v1/v1.1/v1.2 create paths, the page-form handlers (clamping), and
# the viewer's read-side clamp.
DURATION_S_MAX = 365 * 24 * 60 * 60


# Per-asset custom HTTP request headers for webpage assets (feature
# #2215). Stored in ``Asset.metadata['headers']`` as a ``{name: value}``
# object and injected by the C++ webview's request interceptor on
# same-origin requests (scheme+host+port), so a private dashboard (e.g. a
# Grafana service-account token) renders without having to be made
# public. Bounds keep a hostile or typo'd row from bloating the D-Bus
# payload, the DB blob, or a single request's header block. This
# server-side validation is the primary gate keeping CR/LF out of the
# wire (header/response-splitting); the webview re-validates defensively
# at its D-Bus boundary too. ``MAX_HEADER_VALUE_LEN`` is a byte cap
# (values go on the wire as UTF-8), matching the webview's check.
MAX_ASSET_HEADERS = 20
MAX_HEADER_NAME_LEN = 256
MAX_HEADER_VALUE_LEN = 4096

# RFC 7230 ``field-name`` is ``1*tchar``. Anchored so a name carrying a
# colon, whitespace, or a control char is rejected outright rather than
# smuggled onto the wire.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _is_valid_header_name(name: str) -> bool:
    return len(name) <= MAX_HEADER_NAME_LEN and bool(
        _HEADER_NAME_RE.match(name)
    )


def _is_valid_header_value(value: str) -> bool:
    # CR / LF / NUL would let a stored value inject additional headers
    # (or split the request) once the C++ side writes it verbatim, so
    # they are rejected here — the one place every write path funnels
    # through. Everything else reaches the origin byte-for-byte.
    if any(ch in value for ch in ('\r', '\n', '\x00')):
        return False
    # Cap the UTF-8 *byte* length, not the character count: the value is
    # sent on the wire as UTF-8 (the webview's toUtf8()), so a string of
    # multi-byte characters would otherwise exceed the intended byte
    # budget. Keeps this in lockstep with the webview's byte-based cap.
    return len(value.encode('utf-8')) <= MAX_HEADER_VALUE_LEN


def normalize_asset_headers(value: Any) -> dict[str, str]:
    """Coerce an arbitrary ``metadata['headers']`` value into a safe
    ``{name: value}`` dict, dropping (not raising on) anything malformed.

    Same defensive posture as ``clamp_refresh_interval``: the strict
    reject-on-invalid path lives in the v2 serializer's write validation
    (``validate_asset_headers`` below), but a hand-edited row, a legacy
    import, or a non-string JSON value must never crash the viewer read
    path or the API GET. ``Any`` because callers pass whatever JSON the
    column happens to hold.
    """
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        if len(out) >= MAX_ASSET_HEADERS:
            break
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            continue
        name = raw_name.strip()
        if not _is_valid_header_name(name):
            continue
        if not _is_valid_header_value(raw_value):
            continue
        out[name] = raw_value
    return out


def validate_asset_headers(value: Any) -> dict[str, str]:
    """Strict counterpart to ``normalize_asset_headers``: raises
    ``ValueError`` (with a human reason) on any malformed entry instead
    of silently dropping it.

    Used by the v2 API write path so an operator sending a bad header
    gets a 400 that names the problem, rather than a 200 that quietly
    discarded half of what they typed. The server-rendered form uses the
    forgiving ``parse_header_lines`` path instead (mirroring how
    ``refresh_interval_s`` is 400'd by the API but clamped by the form).
    """
    if not isinstance(value, dict):
        # ValueError (not TypeError) is intentional: the v2 API write path
        # maps ValueError -> HTTP 400; a TypeError would surface as 500.
        raise ValueError(  # noqa: TRY004
            'Headers must be an object of name/value pairs.'
        )
    if len(value) > MAX_ASSET_HEADERS:
        raise ValueError(
            f'At most {MAX_ASSET_HEADERS} custom headers are allowed.'
        )
    out: dict[str, str] = {}
    for raw_name, raw_value in value.items():
        name = raw_name.strip() if isinstance(raw_name, str) else ''
        if not _is_valid_header_name(name):
            raise ValueError(f'Invalid header name: {raw_name!r}')
        if not isinstance(raw_value, str) or not _is_valid_header_value(
            raw_value
        ):
            raise ValueError(f'Invalid value for header {name!r}')
        out[name] = raw_value
    return out


def parse_header_lines(text: Any) -> dict[str, str]:
    """Parse a textarea of ``Name: Value`` lines into a sanitised header
    dict for the server-rendered edit form.

    Blank lines and lines without a colon are ignored; the value keeps
    everything after the first colon (so ``Bearer a:b`` survives). The
    result is funnelled through ``normalize_asset_headers`` so the form
    clamps (drops bad entries) rather than 400ing, matching the
    ``refresh_interval_s`` form contract.
    """
    if not isinstance(text, str):
        return {}
    headers: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ':' not in line:
            continue
        name, _, raw_value = line.partition(':')
        headers[name.strip()] = raw_value.strip()
    return normalize_asset_headers(headers)


def clamp_duration(value: Any) -> int:
    """Coerce an arbitrary ``Asset.duration`` value to a safe int in
    ``[0, DURATION_S_MAX]``.

    The API write paths reject out-of-range values, but a hand-edited
    row or a legacy import can still hold junk, and the viewer must
    never crash on a DB value. Same contract as
    ``clamp_refresh_interval`` below: garbage coerces to 0.
    """
    try:
        duration = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(duration, DURATION_S_MAX))


def clamp_refresh_interval(value: Any) -> int:
    """Coerce an arbitrary ``metadata['refresh_interval_s']`` value to
    a safe int in ``[0, REFRESH_INTERVAL_S_MAX]``.

    The serializer's write path rejects out-of-range values, but a
    hand-edited row, a legacy import, or a non-int JSON value could
    leave junk in the column. Every read site (v2 serializer, edit-
    modal ``to_json`` filter, viewer ``asset_loop``, page-form
    handler) funnels through this so the clamp can't drift between
    them. ``Any`` rather than ``object`` because callers pass dict /
    list / unknown JSON values and we want ``int(value)`` to attempt
    coercion regardless — TypeError / ValueError gets caught.
    """
    try:
        interval = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(interval, REFRESH_INTERVAL_S_MAX))


def generate_asset_id() -> str:
    return uuid.uuid4().hex


def _default_play_days() -> str:
    return json.dumps(ALL_DAYS)


# Defensive bound on playlist nesting depth at expansion time. The API
# and UI enforce the tree shape (no self-ancestor) at edit time, but a
# hand-edited or migrated row must never be able to hang the viewer in
# an unbounded recursion. Subtrees past the cap are dropped and logged.
MAX_PLAYLIST_DEPTH = 8


class PlayWindowMixin(models.Model):
    """Day-of-week / time-of-day window shared by ``Asset`` and
    ``Playlist``.

    Expects the concrete model to define ``play_days`` (JSON text),
    ``play_time_from`` and ``play_time_to``.
    """

    if TYPE_CHECKING:
        # The concrete models declare the actual columns; these stubs
        # give the mixin's methods something to type-check against.
        play_days: Any
        play_time_from: time | None
        play_time_to: time | None

    class Meta:
        abstract = True

    def get_play_days(self) -> list[int]:
        """Parse play_days into a sorted, deduped list of ints 1-7.

        Falls back to all days if the value is missing, malformed JSON,
        not a list, empty, or contains anything outside the 1-7 range.
        The API validates on write, but admin / direct DB edits could
        otherwise leave a row with junk in this column. Normalising on
        read also keeps API responses consistent (sorted, no dupes).
        """
        if isinstance(self.play_days, list):
            value = self.play_days
        else:
            try:
                value = json.loads(self.play_days)
            except (TypeError, json.JSONDecodeError):
                return list(ALL_DAYS)

        if not isinstance(value, list):
            return list(ALL_DAYS)
        if not all(isinstance(d, int) and 1 <= d <= 7 for d in value):
            return list(ALL_DAYS)

        deduped = sorted(set(value))
        if not deduped:
            return list(ALL_DAYS)
        return deduped

    def has_window_filter(self) -> bool:
        """True if any day-of-week or time-of-day filter is set.

        A time-of-day filter only applies when both endpoints are set —
        _matches_play_window() treats a partial window as no filter — so
        report it that way here too. Otherwise a stray single-endpoint
        value (rejected by the v2 API but possible via admin / direct DB
        edits) would force the windowed deadline cap on every tick
        without actually filtering anything.
        """
        if self.play_time_from is not None and self.play_time_to is not None:
            return True
        return self.get_play_days() != list(ALL_DAYS)

    def _matches_play_window(self, now_local: datetime) -> bool:
        """Day-of-week and time-of-day filter, evaluated in local time.

        Overnight windows (play_time_from > play_time_to) wrap past
        midnight; play_days refers to the **start** day of such a
        window. With no window fields set this is a no-op (returns
        True), so unscheduled rows behave as before.
        """
        weekday = now_local.isoweekday()
        days = self.get_play_days()

        if self.play_time_from is None or self.play_time_to is None:
            return weekday in days

        current_time = now_local.time()

        if self.play_time_from <= self.play_time_to:
            if weekday not in days:
                return False
            return self.play_time_from <= current_time < self.play_time_to

        # Overnight: window is [play_time_from, 24:00) on day D plus
        # [00:00, play_time_to) on day D+1. play_days lists the D side.
        if current_time >= self.play_time_from:
            return weekday in days
        if current_time < self.play_time_to:
            yesterday = weekday - 1 if weekday > 1 else 7
            return yesterday in days
        return False


class Asset(PlayWindowMixin):
    asset_id = models.TextField(
        primary_key=True, default=generate_asset_id, editable=False
    )
    name = models.TextField(blank=True, null=True)
    uri = models.TextField(blank=True, null=True)
    md5 = models.TextField(blank=True, null=True)
    start_date = models.DateTimeField(blank=True, null=True)
    end_date = models.DateTimeField(blank=True, null=True)
    duration = models.BigIntegerField(blank=True, null=True)
    mimetype = models.TextField(blank=True, null=True)
    is_enabled = models.BooleanField(default=False)
    is_processing = models.BooleanField(default=False)
    nocache = models.BooleanField(default=False)
    play_order = models.IntegerField(default=0)
    skip_asset_check = models.BooleanField(default=False)
    # Per-asset opt-out of TLS certificate verification for a remote
    # HTTPS URI (e.g. media served from an intranet host with a
    # self-signed / untrusted-CA cert). Composes with the device-wide
    # ``verify_ssl`` setting: verification is skipped when the global
    # setting is off OR this flag is set. Only ever loosens, never
    # tightens. Consulted by the reachability probe (url_fails) and,
    # for images/web pages, by the C++ webview per load.
    skip_ssl_verify = models.BooleanField(default=False)
    play_days = models.TextField(default=_default_play_days)
    play_time_from = models.TimeField(blank=True, null=True)
    play_time_to = models.TimeField(blank=True, null=True)
    is_reachable = models.BooleanField(default=True)
    last_reachability_check = models.DateTimeField(blank=True, null=True)
    # Per-asset bag of processing-pipeline state. Carries flags written
    # by the upload-time normalisation tasks (normalize_image_asset,
    # normalize_video_asset) — original file extension, whether a
    # transcode happened, the last processing error if any — without
    # widening the schema for each new field. The pipeline writes; the
    # model itself never reads/branches on it. Default ``dict`` (not
    # None) so callers can ``asset.metadata['k'] = v`` without an
    # ``or {}`` guard.
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'assets'

    def __str__(self) -> str:
        return str(self.name)

    def is_active(self, now: datetime | None = None) -> bool:
        if not (self.is_enabled and self.start_date and self.end_date):
            return False
        if now is None:
            now = timezone.now()
        if not (self.start_date < now < self.end_date):
            return False
        return self._matches_play_window(timezone.localtime(now))


class Playlist(PlayWindowMixin):
    """A named, orderable, schedulable container of playlist items.

    Scheduling fields use the same vocabulary as ``Asset`` with one
    deliberate difference: ``start_date`` / ``end_date`` are optional,
    and an unset bound means "unbounded" (an asset must carry both
    dates to play; a playlist without dates is always date-eligible).
    An occurrence plays iff its asset is active AND every ancestor
    playlist admits ``now``.

    ``repeat=True`` (the default) loops the playlist's content forever —
    exactly the pre-playlist behaviour. ``repeat=False`` plays each
    occurrence under this playlist once per activation window; the
    viewer's Scheduler owns that runtime state (it is per-device
    play-state, not configuration).
    """

    playlist_id = models.TextField(
        primary_key=True, default=generate_asset_id, editable=False
    )
    name = models.TextField()
    is_enabled = models.BooleanField(default=True)
    repeat = models.BooleanField(default=True)
    # Exactly one playlist carries is_default=True: the landing target
    # for every write path that doesn't name a playlist (v1/v1.1/v1.2
    # creates, the HTML add-asset form, app installs, default assets).
    # It cannot be deleted or nested via the API/UI.
    is_default = models.BooleanField(default=False)
    # Orders root playlists relative to each other; the flattener walks
    # roots by (position, playlist_id). Ignored for nested playlists,
    # whose order among siblings is their PlaylistItem's position.
    position = models.IntegerField(default=0)
    start_date = models.DateTimeField(blank=True, null=True)
    end_date = models.DateTimeField(blank=True, null=True)
    play_days = models.TextField(default=_default_play_days)
    play_time_from = models.TimeField(blank=True, null=True)
    play_time_to = models.TimeField(blank=True, null=True)

    class Meta:
        db_table = 'playlists'

    def __str__(self) -> str:
        return str(self.name)

    def admits(self, now: datetime | None = None) -> bool:
        """Does this playlist's own window admit ``now``?

        The playlist-local half of activeness — is_enabled, optional
        date bounds, day/time window. Ancestors are ANDed in by the
        expansion walk in ``playlist_eval``, not here.
        """
        if not self.is_enabled:
            return False
        if now is None:
            now = timezone.now()
        if self.start_date and now <= self.start_date:
            return False
        if self.end_date and now >= self.end_date:
            return False
        return self._matches_play_window(timezone.localtime(now))


class PlaylistItem(models.Model):
    """One slot in a playlist: either an asset occurrence or a nested
    playlist. Exactly one of ``asset`` / ``child_playlist`` is set
    (DB-enforced by the check constraint below).

    Asset items are deliberately NOT unique per (playlist, asset): the
    same asset may appear in several playlists and more than once in
    one — each row is an independent occurrence. Child-playlist items
    ARE unique across the whole table (``child_playlist`` is a
    OneToOneField), which is what enforces the tree shape: a playlist
    has at most one parent.
    """

    playlist = models.ForeignKey(
        Playlist, related_name='items', on_delete=models.CASCADE
    )
    asset = models.ForeignKey(
        Asset,
        related_name='playlist_items',
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    child_playlist = models.OneToOneField(
        Playlist,
        related_name='parent_item',
        on_delete=models.CASCADE,
        blank=True,
        null=True,
    )
    position = models.IntegerField(default=0)

    class Meta:
        db_table = 'playlist_items'
        ordering: ClassVar[list[str]] = ['position', 'id']
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=(
                    models.Q(asset__isnull=False, child_playlist__isnull=True)
                    | models.Q(
                        asset__isnull=True, child_playlist__isnull=False
                    )
                ),
                name='playlist_item_exactly_one_target',
            ),
        ]

    def __str__(self) -> str:
        target = self.asset or self.child_playlist
        return f'{self.playlist_id}[{self.position}] -> {target}'


def get_default_playlist() -> Playlist:
    """The Default playlist — the landing target for every write path
    that doesn't name a playlist.

    Created by the 0009 backfill migration; the get_or_create is a
    belt-and-braces guard for a hand-edited DB where the row was
    deleted (an asset outside any playlist silently never plays, so
    this must never be allowed to fail).
    """
    playlist, _ = Playlist.objects.get_or_create(
        is_default=True,
        defaults={'name': 'Default'},
    )
    return playlist


def playlist_is_self_or_ancestor(candidate: Playlist, of: Playlist) -> bool:
    """True if ``candidate`` is ``of`` itself or an ancestor of it.

    The edit-time cycle gate for the tree model: nesting ``candidate``
    under ``of`` is illegal exactly when this returns True. Walks the
    single-parent chain (``parent_item`` reverse OneToOne), bounded by
    MAX_PLAYLIST_DEPTH so even a corrupted DB can't loop it forever —
    an over-deep walk reports True (refuse the edit) rather than
    risking a false "safe".
    """
    node: Playlist | None = of
    for _ in range(MAX_PLAYLIST_DEPTH + 1):
        if node is None:
            return False
        if node.playlist_id == candidate.playlist_id:
            return True
        # RelatedObjectDoesNotExist subclasses AttributeError, so
        # getattr-with-default cleanly maps "no parent" to None.
        parent_item = getattr(node, 'parent_item', None)
        node = parent_item.playlist if parent_item else None
    return True


def mirror_play_order_to_default_playlist() -> None:
    """Re-sort the Default playlist's asset items to match
    ``Asset.play_order``.

    ``play_order`` is still what every legacy write surface speaks —
    the v1/v1.1/v1.2/v2 serializers, ``POST /assets/order``, the
    home-page drag reorder — while the playlist evaluators read order
    from item positions. This
    is the single reconciliation point: asset items are permuted among
    their **existing** position slots by (play_order, current order),
    so nested-playlist items keep their exact positions and other
    playlists are never touched. Named playlists are ordered through
    the v2 playlist endpoints and have no play_order coupling.
    """
    items = list(
        PlaylistItem.objects.filter(
            playlist=get_default_playlist()
        ).select_related('asset')
    )
    listed = [
        (index, item)
        for index, item in enumerate(items)
        if item.asset is not None
    ]
    slots = [item.position for _, item in listed]
    listed.sort(
        key=lambda pair: (
            pair[1].asset.play_order if pair[1].asset else 0,
            pair[0],
        )
    )
    changed = []
    for (_, item), position in zip(listed, slots):
        if item.position != position:
            item.position = position
            changed.append(item)
    if changed:
        PlaylistItem.objects.bulk_update(changed, ['position'])


def _append_asset_to_default_playlist(
    sender: object, instance: Asset, created: bool, **kwargs: object
) -> None:
    """post_save hook: a brand-new asset with no playlist membership
    lands in the Default playlist at its ``play_order``.

    A signal rather than per-call-site plumbing because asset creation
    is scattered across the v1/v1.1/v1.2/v2 APIs, the HTML add form,
    app installs, sample-asset seeding, duplication and the content
    importers — and a single missed site would strand an asset outside
    every playlist, where it silently never plays (feasibility doc,
    risk 4). The item's position seeds from ``play_order`` (ties break
    by item id, i.e. creation order) so legacy creators that place a
    row via play_order keep working; the mirror helper above keeps the
    two in step on every subsequent reorder. Callers that DO target a
    specific playlist create their ``PlaylistItem`` explicitly and are
    skipped here only if they did so inside ``Asset.save()``; created
    rows can simply be moved afterwards.
    """
    del sender, kwargs
    if created and not PlaylistItem.objects.filter(asset=instance).exists():
        PlaylistItem.objects.create(
            playlist=get_default_playlist(),
            asset=instance,
            position=instance.play_order or 0,
        )
    # Any save can carry a play_order edit (Django admin, the v1/v1.1
    # update serializers write instance.play_order and call save()
    # directly, without going through save_active_assets_ordering), so
    # re-sync unconditionally — the mirror is a no-op when order
    # already matches, and playlist evaluation must never read a stale
    # position after a legacy write.
    mirror_play_order_to_default_playlist()


models.signals.post_save.connect(
    _append_asset_to_default_playlist,
    sender=Asset,
    dispatch_uid='append_asset_to_default_playlist',
)
