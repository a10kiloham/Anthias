"""Tests for the server-rendered /playlists/ page and its htmx write
endpoints (thin wrappers over the same model operations as the v2
API — these pin the form-side behaviour and the partial contract)."""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest import mock

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


@pytest.fixture(autouse=True)
def _mock_ws_notify() -> Iterator[None]:
    """Skip the Channels group_send fan-out: it's not under test here,
    and on a host without the Docker Redis each call eats a ~20s
    connection timeout before the swallow-and-continue path runs."""
    with mock.patch('anthias_server.app.consumers.notify_asset_update'):
        yield


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
