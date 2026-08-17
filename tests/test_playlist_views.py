"""Tests for the server-rendered /playlists/ page and its htmx write
endpoints (thin wrappers over the same model operations as the v2
API — these pin the form-side behaviour and the partial contract)."""

from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from anthias_server.app.models import (
    Asset,
    Playlist,
    PlaylistItem,
    get_default_playlist,
)


@pytest.fixture
def client() -> Client:
    return Client()


def _hx(client: Client, url: str, data: dict[str, Any]) -> Any:
    return client.post(url, data, headers={'HX-Request': 'true'})


def _make_asset(asset_id: str) -> Asset:
    now = timezone.now()
    return Asset.objects.create(
        asset_id=asset_id,
        name=asset_id,
        uri=f'https://example.com/{asset_id}.png',
        mimetype='image',
        duration=5,
        is_enabled=True,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=30),
    )


@pytest.mark.django_db
def test_playlists_page_renders_with_default(client: Client) -> None:
    response = client.get(reverse('anthias_app:playlists'))
    assert response.status_code == 200
    assert b'Default' in response.content


@pytest.mark.django_db
def test_create_and_delete_playlist(client: Client) -> None:
    response = _hx(
        client,
        reverse('anthias_app:playlists_create'),
        {'name': 'Lobby loop'},
    )
    assert response.status_code == 200
    playlist = Playlist.objects.get(name='Lobby loop')

    response = _hx(
        client,
        reverse('anthias_app:playlists_delete', args=[playlist.playlist_id]),
        {},
    )
    assert response.status_code == 200
    assert not Playlist.objects.filter(name='Lobby loop').exists()


@pytest.mark.django_db
def test_delete_default_playlist_is_refused(client: Client) -> None:
    default = get_default_playlist()
    _hx(
        client,
        reverse('anthias_app:playlists_delete', args=[default.playlist_id]),
        {},
    )
    assert Playlist.objects.filter(is_default=True).exists()


@pytest.mark.django_db
def test_toggle_enable_and_repeat(client: Client) -> None:
    playlist = Playlist.objects.create(name='p')
    _hx(
        client,
        reverse('anthias_app:playlists_toggle', args=[playlist.playlist_id]),
        {},
    )
    playlist.refresh_from_db()
    assert playlist.is_enabled is False

    _hx(
        client,
        reverse(
            'anthias_app:playlists_toggle_repeat',
            args=[playlist.playlist_id],
        ),
        {},
    )
    playlist.refresh_from_db()
    assert playlist.repeat is False


@pytest.mark.django_db
def test_add_asset_and_move_and_remove(client: Client) -> None:
    a, b = _make_asset('a'), _make_asset('b')
    playlist = Playlist.objects.create(name='p')
    for asset in (a, b):
        _hx(
            client,
            reverse(
                'anthias_app:playlist_add_asset',
                args=[playlist.playlist_id],
            ),
            {'asset_id': asset.asset_id},
        )
    items = list(playlist.items.order_by('position', 'id'))
    assert [item.asset_id for item in items] == ['a', 'b']

    _hx(
        client,
        reverse(
            'anthias_app:playlist_move_item',
            args=[playlist.playlist_id, items[1].id],
        ),
        {'direction': 'up'},
    )
    items = list(playlist.items.order_by('position', 'id'))
    assert [item.asset_id for item in items] == ['b', 'a']

    _hx(
        client,
        reverse(
            'anthias_app:playlist_remove_item',
            args=[playlist.playlist_id, items[0].id],
        ),
        {},
    )
    assert list(playlist.items.values_list('asset_id', flat=True)) == ['a']
    # Removing an occurrence never deletes the asset.
    assert Asset.objects.filter(asset_id='b').exists()


@pytest.mark.django_db
def test_nest_rejects_cycle_and_double_parent(client: Client) -> None:
    outer = Playlist.objects.create(name='outer')
    inner = Playlist.objects.create(name='inner')
    other = Playlist.objects.create(name='other')

    _hx(
        client,
        reverse('anthias_app:playlist_nest', args=[outer.playlist_id]),
        {'child_playlist_id': inner.playlist_id},
    )
    assert PlaylistItem.objects.filter(
        playlist=outer, child_playlist=inner
    ).exists()

    # Cycle: inner <- outer refused.
    _hx(
        client,
        reverse('anthias_app:playlist_nest', args=[inner.playlist_id]),
        {'child_playlist_id': outer.playlist_id},
    )
    assert not PlaylistItem.objects.filter(
        playlist=inner, child_playlist=outer
    ).exists()

    # Second parent refused.
    _hx(
        client,
        reverse('anthias_app:playlist_nest', args=[other.playlist_id]),
        {'child_playlist_id': inner.playlist_id},
    )
    assert PlaylistItem.objects.filter(child_playlist=inner).count() == 1


@pytest.mark.django_db
def test_schedule_save_roundtrip(client: Client) -> None:
    playlist = Playlist.objects.create(name='p')
    response = _hx(
        client,
        reverse('anthias_app:playlists_schedule', args=[playlist.playlist_id]),
        {
            'start_date': '2026-09-01T09:00',
            'end_date': '2026-12-01T17:00',
            'play_time_from': '09:00',
            'play_time_to': '17:00',
            'play_days': ['6', '7'],
        },
    )
    assert response.status_code == 200
    playlist.refresh_from_db()
    assert playlist.start_date is not None
    assert playlist.end_date is not None
    assert playlist.get_play_days() == [6, 7]
    assert playlist.play_time_from is not None

    # Blank dates clear the bounds (unbounded again).
    _hx(
        client,
        reverse('anthias_app:playlists_schedule', args=[playlist.playlist_id]),
        {
            'start_date': '',
            'end_date': '',
            'play_time_from': '',
            'play_time_to': '',
            'play_days': ['1', '2', '3', '4', '5', '6', '7'],
        },
    )
    playlist.refresh_from_db()
    assert playlist.start_date is None
    assert playlist.end_date is None
    assert playlist.play_time_from is None


@pytest.mark.django_db
def test_schedule_rejects_partial_time_window(client: Client) -> None:
    playlist = Playlist.objects.create(name='p')
    _hx(
        client,
        reverse('anthias_app:playlists_schedule', args=[playlist.playlist_id]),
        {
            'play_time_from': '09:00',
            'play_time_to': '',
            'play_days': ['1'],
        },
    )
    playlist.refresh_from_db()
    assert playlist.play_time_from is None


@pytest.mark.django_db
def test_nested_playlist_renders_recursively(client: Client) -> None:
    asset = _make_asset('deep-asset')
    outer = Playlist.objects.create(name='OuterList')
    inner = Playlist.objects.create(name='InnerList')
    PlaylistItem.objects.create(
        playlist=outer, child_playlist=inner, position=0
    )
    PlaylistItem.objects.create(playlist=inner, asset=asset, position=0)

    response = client.get(reverse('anthias_app:playlists'))
    assert response.status_code == 200
    assert b'OuterList' in response.content
    assert b'InnerList' in response.content
    assert b'deep-asset' in response.content


@pytest.mark.django_db
def test_playlist_order_endpoint_reorders_items(client: Client) -> None:
    """The drag handler POSTs the tbody's item-id sequence as a comma
    CSV — same contract as the home page's assets_order."""
    a, b, c = (_make_asset(i) for i in ('a', 'b', 'c'))
    playlist = Playlist.objects.create(name='p')
    items = [
        PlaylistItem.objects.create(
            playlist=playlist, asset=asset, position=index
        )
        for index, asset in enumerate((a, b, c))
    ]

    response = _hx(
        client,
        reverse('anthias_app:playlist_order', args=[playlist.playlist_id]),
        {'ids': f'{items[2].id},{items[0].id},{items[1].id}'},
    )
    assert response.status_code == 200
    ordered = list(
        playlist.items.order_by('position', 'id').values_list(
            'asset_id', flat=True
        )
    )
    assert ordered == ['c', 'a', 'b']


@pytest.mark.django_db
def test_playlist_order_endpoint_rejects_foreign_ids(
    client: Client,
) -> None:
    asset = _make_asset('a')
    mine = Playlist.objects.create(name='mine')
    other = Playlist.objects.create(name='other')
    foreign = PlaylistItem.objects.create(
        playlist=other, asset=asset, position=0
    )
    own = PlaylistItem.objects.create(playlist=mine, asset=asset, position=0)

    _hx(
        client,
        reverse('anthias_app:playlist_order', args=[mine.playlist_id]),
        {'ids': str(foreign.id)},
    )
    own.refresh_from_db()
    foreign.refresh_from_db()
    assert own.position == 0
    assert foreign.position == 0


@pytest.mark.django_db
def test_created_playlist_is_a_schedule_row(client: Client) -> None:
    """A new playlist lands as a child item of the Default playlist —
    i.e. a row in the Schedule list — and renders on the home page."""
    _hx(
        client,
        reverse('anthias_app:playlists_create'),
        {'name': 'Lobby loop'},
    )
    playlist = Playlist.objects.get(name='Lobby loop')
    parent_item = playlist.parent_item
    assert parent_item is not None
    assert parent_item.playlist.is_default

    response = client.get(reverse('anthias_app:home'))
    assert response.status_code == 200
    assert b'Lobby loop' in response.content
    assert f'playlist:{playlist.playlist_id}'.encode() in response.content


@pytest.mark.django_db
def test_disabled_playlist_renders_in_inactive_section(
    client: Client,
) -> None:
    playlist = Playlist.objects.create(name='Off air', is_enabled=False)
    from anthias_server.app.models import append_playlist_to_default

    append_playlist_to_default(playlist)

    from anthias_server.app.page_context import assets as assets_context

    context = assets_context()
    active_names = [
        row['playlist'].name
        for row in context['active_rows']
        if row['kind'] == 'playlist'
    ]
    inactive_names = [
        row['playlist'].name
        for row in context['inactive_rows']
        if row['kind'] == 'playlist'
    ]
    assert 'Off air' not in active_names
    assert 'Off air' in inactive_names


@pytest.mark.django_db
def test_schedule_order_interleaves_assets_and_playlists(
    client: Client,
) -> None:
    """The home drag POSTs a mixed sequence; the Default playlist's
    items land in exactly that order and the viewer plays it."""
    from anthias_server.app.models import (
        append_playlist_to_default,
        get_default_playlist,
    )
    from anthias_server.app.playlist_eval import expand_occurrences

    a, b = _make_asset('a'), _make_asset('b')
    playlist = Playlist.objects.create(name='p')
    append_playlist_to_default(playlist)
    inner = _make_asset('inner')
    PlaylistItem.objects.filter(
        playlist=get_default_playlist(), asset=inner
    ).delete()
    PlaylistItem.objects.create(playlist=playlist, asset=inner, position=0)

    response = _hx(
        client,
        reverse('anthias_app:assets_order'),
        {'ids': f'{b.asset_id},playlist:{playlist.playlist_id},{a.asset_id}'},
    )
    assert response.status_code == 200

    ordered = [
        item.asset_id or f'playlist:{item.child_playlist_id}'
        for item in get_default_playlist().items.order_by('position', 'id')
    ]
    assert ordered == ['b', f'playlist:{playlist.playlist_id}', 'a']
    # And the flattened play order follows: b, then the playlist's
    # content, then a.
    play_order = [o.asset.asset_id for o in expand_occurrences()]
    assert play_order == ['b', 'inner', 'a']


@pytest.mark.django_db
def test_playlist_toggle_from_schedule_returns_asset_table(
    client: Client,
) -> None:
    from anthias_server.app.models import append_playlist_to_default

    playlist = Playlist.objects.create(name='p')
    append_playlist_to_default(playlist)
    response = _hx(
        client,
        reverse('anthias_app:playlists_toggle', args=[playlist.playlist_id]),
        {'return': 'schedule'},
    )
    assert response.status_code == 200
    assert b'id="asset-table"' in response.content
    playlist.refresh_from_db()
    assert playlist.is_enabled is False


@pytest.mark.django_db
def test_deleting_parent_rehomes_children_to_schedule(
    client: Client,
) -> None:
    from anthias_server.app.models import append_playlist_to_default

    parent = Playlist.objects.create(name='parent')
    child = Playlist.objects.create(name='child')
    append_playlist_to_default(parent)
    PlaylistItem.objects.create(
        playlist=parent, child_playlist=child, position=0
    )

    _hx(
        client,
        reverse('anthias_app:playlists_delete', args=[parent.playlist_id]),
        {},
    )
    assert not Playlist.objects.filter(name='parent').exists()
    child.refresh_from_db()
    parent_item = child.parent_item
    assert parent_item is not None
    assert parent_item.playlist.is_default


@pytest.mark.django_db
def test_nesting_a_schedule_row_moves_it_off_the_schedule(
    client: Client,
) -> None:
    from anthias_server.app.models import (
        append_playlist_to_default,
        get_default_playlist,
    )

    outer = Playlist.objects.create(name='outer')
    inner = Playlist.objects.create(name='inner')
    append_playlist_to_default(outer)
    append_playlist_to_default(inner)

    _hx(
        client,
        reverse('anthias_app:playlist_nest', args=[outer.playlist_id]),
        {'child_playlist_id': inner.playlist_id},
    )
    inner.refresh_from_db()
    parent_item = inner.parent_item
    assert parent_item is not None
    assert parent_item.playlist_id == outer.playlist_id
    assert not PlaylistItem.objects.filter(
        playlist=get_default_playlist(), child_playlist=inner
    ).exists()


@pytest.mark.django_db
def test_playlist_schedule_lifecycle(client: Client) -> None:
    """The full operator journey from the Schedule tab: create a
    playlist, fill it with existing assets, schedule it for right now
    (it becomes an Active row and its occurrences play), push its
    window into the past and disable it (it stops and moves to
    Inactive), then delete it — the underlying assets must survive and
    keep playing through the Default playlist."""
    from anthias_server.app.page_context import assets as assets_context
    from anthias_server.app.playlist_eval import evaluate_playlist

    def playing_via(playlist_id: str) -> list[str]:
        active, _ = evaluate_playlist()
        return [
            occurrence.asset.asset_id
            for occurrence in active
            if any(p.playlist_id == playlist_id for p in occurrence.path)
        ]

    def schedule_rows(section: str) -> list[str]:
        return [
            row['playlist'].name
            for row in assets_context()[section]
            if row['kind'] == 'playlist'
        ]

    first, second = _make_asset('first'), _make_asset('second')

    # Create — the new playlist is a Schedule row (child of Default).
    _hx(
        client,
        reverse('anthias_app:playlists_create'),
        {'name': 'Lunch loop'},
    )
    playlist = Playlist.objects.get(name='Lunch loop')
    assert playlist.parent_item is not None
    assert playlist.parent_item.playlist.is_default

    for asset in (first, second):
        _hx(
            client,
            reverse(
                'anthias_app:playlist_add_asset',
                args=[playlist.playlist_id],
            ),
            {'asset_id': asset.asset_id},
        )

    # Schedule for now: a date window around the current moment.
    local_now = timezone.localtime()
    fmt = '%Y-%m-%dT%H:%M'
    all_days = [str(day) for day in range(1, 8)]
    response = _hx(
        client,
        reverse('anthias_app:playlists_schedule', args=[playlist.playlist_id]),
        {
            'start_date': (local_now - timedelta(hours=1)).strftime(fmt),
            'end_date': (local_now + timedelta(hours=1)).strftime(fmt),
            'play_time_from': '',
            'play_time_to': '',
            'play_days': all_days,
            'return': 'schedule',
        },
    )
    assert response.status_code == 200
    playlist.refresh_from_db()
    assert playlist.admits()
    assert 'Lunch loop' in schedule_rows('active_rows')
    assert playing_via(playlist.playlist_id) == ['first', 'second']

    # Unschedule — a window wholly in the past stops playback...
    _hx(
        client,
        reverse('anthias_app:playlists_schedule', args=[playlist.playlist_id]),
        {
            'start_date': (local_now - timedelta(days=2)).strftime(fmt),
            'end_date': (local_now - timedelta(days=1)).strftime(fmt),
            'play_days': all_days,
            'return': 'schedule',
        },
    )
    playlist.refresh_from_db()
    assert not playlist.admits()
    assert playing_via(playlist.playlist_id) == []

    # ...and disabling moves the row to the Inactive section.
    _hx(
        client,
        reverse('anthias_app:playlists_toggle', args=[playlist.playlist_id]),
        {'return': 'schedule'},
    )
    playlist.refresh_from_db()
    assert playlist.is_enabled is False
    assert 'Lunch loop' not in schedule_rows('active_rows')
    assert 'Lunch loop' in schedule_rows('inactive_rows')

    # Delete — the playlist and its items go; the assets stay and keep
    # playing through the Default playlist.
    _hx(
        client,
        reverse('anthias_app:playlists_delete', args=[playlist.playlist_id]),
        {'return': 'schedule'},
    )
    assert not Playlist.objects.filter(
        playlist_id=playlist.playlist_id
    ).exists()
    assert not PlaylistItem.objects.filter(
        playlist_id=playlist.playlist_id
    ).exists()
    assert Asset.objects.filter(asset_id__in=['first', 'second']).count() == 2
    active, _ = evaluate_playlist()
    assert {'first', 'second'} <= {o.asset.asset_id for o in active}
