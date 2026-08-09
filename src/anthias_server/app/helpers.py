import logging
import uuid
from os import getenv, path, remove
from typing import Any

import yaml
from django.conf import settings as django_settings
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from anthias_common.utils import get_video_duration
from anthias_server.app.models import (
    Asset,
    Playlist,
    PlaylistItem,
    get_default_playlist,
)
from anthias_server.app.page_context import navbar as _navbar_context
from anthias_server.settings import ViewerPublisher, settings

logger = logging.getLogger(__name__)


def template(
    request: HttpRequest,
    template_name: str,
    context: dict[str, Any],
) -> HttpResponse:
    """
    This is a helper function that is used to render a template
    with some global context. This is used to avoid having to
    repeat code in other views.
    """

    context['date_format'] = settings['date_format']
    context['default_duration'] = settings['default_duration']
    context['default_streaming_duration'] = settings[
        'default_streaming_duration'
    ]
    context['template_settings'] = {
        'imports': [
            'from anthias_common.utils import template_handle_unicode'
        ],
        'default_filters': ['template_handle_unicode'],
    }
    context['use_24_hour_clock'] = settings['use_24_hour_clock']
    # Store-catalog index URL for the Add → Apps tab (read client-side
    # off a <meta> tag). Sourced from Django settings so it stays
    # env-overridable in one place.
    context['app_store_index_url'] = django_settings.APP_STORE_INDEX_URL
    # Navbar needs is_balena / up_to_date / player_name on every page.
    context.update(_navbar_context())

    return render(request, template_name, context)


def prepare_default_asset(**kwargs: Any) -> dict[str, Any] | None:
    if kwargs['mimetype'] not in ['image', 'video', 'webpage']:
        return None

    asset_id = f'default_{uuid.uuid4().hex}'
    if 'video' == kwargs['mimetype']:
        video_duration = get_video_duration(kwargs['uri'])
        if video_duration is None:
            raise ValueError(
                f'Could not determine duration of video {kwargs["uri"]!r}'
            )
        duration = int(video_duration.total_seconds())
    else:
        duration = kwargs['duration']

    return {
        'asset_id': asset_id,
        'duration': duration,
        'end_date': kwargs['end_date'],
        'is_enabled': True,
        'is_processing': 0,
        'mimetype': kwargs['mimetype'],
        'name': kwargs['name'],
        'nocache': 0,
        'play_order': 0,
        'skip_asset_check': 0,
        'start_date': kwargs['start_date'],
        'uri': kwargs['uri'],
    }


def add_default_assets() -> None:
    settings.load()

    datetime_now = timezone.now()
    default_asset_settings = {
        'start_date': datetime_now,
        'end_date': datetime_now.replace(year=datetime_now.year + 6),
        'duration': settings['default_duration'],
    }

    default_assets_yaml = path.join(
        getenv('HOME') or '',
        '.anthias/default_assets.yml',
    )

    with open(default_assets_yaml, 'r') as yaml_file:
        default_assets = yaml.safe_load(yaml_file).get('assets')

        for default_asset in default_assets:
            default_asset_settings.update(
                {
                    'name': default_asset.get('name'),
                    'uri': default_asset.get('uri'),
                    'mimetype': default_asset.get('mimetype'),
                }
            )
            asset = prepare_default_asset(**default_asset_settings)

            if asset:
                Asset.objects.create(**asset)


def remove_default_assets() -> None:
    settings.load()

    for asset in Asset.objects.all():
        if asset.asset_id.startswith('default_'):
            asset.delete()


class AssetDuplicationError(Exception):
    """Raised when an asset can't be scheduled again (still
    processing)."""


def schedule_asset_occurrence(asset: Asset) -> PlaylistItem:
    """Add another occurrence of ``asset`` to the Default playlist,
    directly after the asset's last occurrence there (appended at the
    end if it has none), and return the new item.

    Supersedes the pre-playlist ``duplicate_asset()`` row clone: back
    then the ``Asset`` row *was* the playlist slot, so scheduling the
    same media twice meant cloning the row and hardlinking its file.
    With first-class playlists the same asset can hold any number of
    slots, so "duplicate" is now just another ``PlaylistItem`` — zero
    file duplication, no hardlink/orphan-sweep machinery, and edits to
    the asset apply to every occurrence at once.

    Shared by the v2 API duplicate endpoint and the HTML row action.
    """
    if asset.is_processing:
        raise AssetDuplicationError('Asset is still processing')

    default = get_default_playlist()
    items = list(default.items.order_by('position', 'id'))
    last_occurrence_index = None
    for index, item in enumerate(items):
        if item.asset_id == asset.asset_id:
            last_occurrence_index = index
    insert_at = (
        last_occurrence_index + 1
        if last_occurrence_index is not None
        else len(items)
    )

    # Renumber around the insertion point rather than shifting with an
    # F() expression: mirror-seeded positions can carry ties, and a
    # blind +1 shift on a tie would leave the new item's ordering to
    # the id tiebreak instead of "directly after the source".
    new_item = PlaylistItem.objects.create(
        playlist=default, asset=asset, position=insert_at
    )
    changed = []
    for index, item in enumerate(items):
        target = index if index < insert_at else index + 1
        if item.position != target:
            item.position = target
            changed.append(item)
    if changed:
        PlaylistItem.objects.bulk_update(changed, ['position'])

    # Wake the viewer so the new occurrence joins the rotation now
    # rather than on the next DB-mtime poll.
    ViewerPublisher.get_instance().send_to_viewer('reload')

    return new_item


def delete_asset_with_file(asset: Asset, *, nudge_viewer: bool = True) -> None:
    """Delete an ``Asset`` row, remove its on-disk file (if owned), and
    nudge the viewer to advance past it.

    Shared by the v1/v1.1/v1.2/v2 API delete endpoint and the HTML form
    delete route on the home page. Both must behave identically — GH
    #2908 was the case where the UI form-post handler dropped the row
    but left the binary in ``settings['assetdir']`` indefinitely.

    File removal is gated on ``asset.uri`` starting with
    ``settings['assetdir']`` so rows whose URI is a remote URL
    (webpage, RTSP, streaming video) are left untouched. Failures are
    logged and swallowed: the row is the operator's source of truth,
    and a stray file is eventually cleaned up by the periodic
    ``cleanup()`` orphan sweep — letting an unlink error block the DB
    delete would leave the operator unable to remove the row at all.

    ``nudge_viewer=False`` skips the per-row viewer reload so a bulk
    delete can fire a single reload after the whole batch instead of
    spamming the pub/sub channel once per asset (#3046).
    """
    if asset.uri and asset.uri.startswith(settings['assetdir']):
        try:
            remove(asset.uri)
        except OSError as exc:
            logger.warning(
                'Failed to remove asset file %s: %s', asset.uri, exc
            )

    asset.delete()

    # Wake the viewer so it skips a now-deleted asset that's still on
    # screen instead of finishing its remaining ``duration`` (#2430).
    # The viewer's reload handler checks whether the currently-shown
    # asset is still active and advances if not.
    if nudge_viewer:
        ViewerPublisher.get_instance().send_to_viewer('reload')


def reorder_playlist_items(playlist: Playlist, ordered_ids: list[int]) -> None:
    """Persist an item ordering for one playlist.

    ``ordered_ids`` is the desired order; every id must belong to this
    playlist (ValueError otherwise). Items not listed keep their
    relative order after the listed ones — so a partial list (e.g. a
    tbody that only contains asset rows) can't silently drop slots.
    Shared by the v2 ``POST /v2/playlists/<id>/order`` endpoint and
    the /playlists drag-reorder form endpoint.
    """
    items = {item.id: item for item in playlist.items.all()}
    unknown = [i for i in ordered_ids if i not in items]
    if unknown:
        raise ValueError(f'Unknown item ids for this playlist: {unknown}')

    remainder = [item for item in items.values() if item.id not in ordered_ids]
    reordered = [items[i] for i in ordered_ids] + sorted(
        remainder, key=lambda item: (item.position, item.id)
    )
    changed = []
    for position, item in enumerate(reordered):
        if item.position != position:
            item.position = position
            changed.append(item)
    if changed:
        PlaylistItem.objects.bulk_update(changed, ['position'])


def save_schedule_ordering(refs: list[str]) -> None:
    """Persist the home Schedule drag order.

    ``refs`` is the on-screen row sequence: bare asset ids mixed with
    ``playlist:<playlist_id>`` entries (the Default playlist's child
    playlists render as reorderable rows alongside assets). Two writes:

    - ``Asset.play_order`` follows the asset subsequence, keeping the
      legacy v1/v1.1/v1.2/v2 wire field truthful.
    - The Default playlist's items are renumbered to the full mixed
      sequence — an asset with several occurrences moves as a group at
      its row's position — with unlisted items (inactive rows) kept in
      their prior relative order after the listed ones.
    """
    asset_ids = [r for r in refs if not r.startswith('playlist:')]
    for i, asset_id in enumerate(asset_ids):
        Asset.objects.filter(asset_id=asset_id).update(play_order=i)

    default = get_default_playlist()
    items = list(default.items.order_by('position', 'id'))
    items_by_asset: dict[str, list[PlaylistItem]] = {}
    child_by_playlist: dict[str, PlaylistItem] = {}
    for item in items:
        if item.asset_id is not None:
            items_by_asset.setdefault(str(item.asset_id), []).append(item)
        elif item.child_playlist_id is not None:
            child_by_playlist[str(item.child_playlist_id)] = item

    sequence: list[PlaylistItem] = []
    seen: set[int] = set()
    for ref in refs:
        if ref.startswith('playlist:'):
            child_item = child_by_playlist.get(ref.removeprefix('playlist:'))
            if child_item is not None and child_item.id not in seen:
                sequence.append(child_item)
                seen.add(child_item.id)
        else:
            for item in items_by_asset.get(ref, []):
                if item.id not in seen:
                    sequence.append(item)
                    seen.add(item.id)
    sequence.extend(item for item in items if item.id not in seen)

    changed = []
    for position, item in enumerate(sequence):
        if item.position != position:
            item.position = position
            changed.append(item)
    if changed:
        PlaylistItem.objects.bulk_update(changed, ['position'])
