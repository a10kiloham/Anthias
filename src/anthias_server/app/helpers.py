import logging
import shutil
import uuid
from os import getenv, link, path, remove
from typing import Any

import yaml
from django.conf import settings as django_settings
from django.db.models import F
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from anthias_server.app.models import Asset
from anthias_server.app.page_context import navbar as _navbar_context
from anthias_common.utils import get_video_duration
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

    asset_id = 'default_{}'.format(uuid.uuid4().hex)
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
    """Raised when an asset can't be cloned (still processing, or its
    on-disk file couldn't be linked/copied)."""


def duplicate_asset(asset: Asset) -> Asset:
    """Clone ``asset`` as an independent playlist entry placed right
    after the source, and return the new row.

    The playlist has no item entity — the ``Asset`` row *is* the
    playlist slot — so scheduling the same media twice means cloning
    the row under a fresh ``asset_id``. Copies are fully independent
    afterwards: each can carry its own duration, play window, or date
    range, and editing one never touches the other.

    When the source's file lives in ``assetdir``, the copy gets its
    own directory entry via a hardlink (same inode, ~zero disk cost;
    falls back to a real copy on filesystems without hardlinks). Every
    row keeps exclusive ownership of its ``uri`` path, so
    ``delete_asset_with_file()`` and the ``cleanup()`` orphan sweep
    need no refcounting — the data is freed when the last link goes.

    Shared by the v2 API duplicate endpoint and the HTML row action.
    """
    if asset.is_processing:
        raise AssetDuplicationError('Asset is still processing')

    new_uri = asset.uri
    if asset.uri and asset.uri.startswith(settings['assetdir']):
        ext = path.splitext(asset.uri)[1]
        new_uri = path.join(
            settings['assetdir'], f'{uuid.uuid4().hex}{ext}'
        )
        try:
            link(asset.uri, new_uri)
        except OSError:
            try:
                shutil.copy2(asset.uri, new_uri)
            except OSError as exc:
                logger.warning(
                    'Failed to clone asset file %s: %s', asset.uri, exc
                )
                raise AssetDuplicationError(
                    'Could not copy the asset file'
                ) from exc

    # Slot the copy directly after the source: shift everything below
    # it down one. Only enabled rows carry a meaningful play_order (the
    # home page and the viewer both order the active list by it), so a
    # disabled source just reuses its play_order verbatim.
    if asset.is_enabled:
        Asset.objects.filter(
            is_enabled=True, play_order__gt=asset.play_order
        ).update(play_order=F('play_order') + 1)

    duplicate = Asset.objects.create(
        name=f'{asset.name} (copy)' if asset.name else None,
        uri=new_uri,
        md5=asset.md5,
        start_date=asset.start_date,
        end_date=asset.end_date,
        duration=asset.duration,
        mimetype=asset.mimetype,
        is_enabled=asset.is_enabled,
        is_processing=False,
        nocache=asset.nocache,
        play_order=asset.play_order + 1,
        skip_asset_check=asset.skip_asset_check,
        play_days=asset.play_days,
        play_time_from=asset.play_time_from,
        play_time_to=asset.play_time_to,
        is_reachable=asset.is_reachable,
        last_reachability_check=asset.last_reachability_check,
        metadata=dict(asset.metadata or {}),
    )

    # Wake the viewer so an active copy joins the rotation now rather
    # than on the next DB-mtime poll.
    ViewerPublisher.get_instance().send_to_viewer('reload')

    return duplicate


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
