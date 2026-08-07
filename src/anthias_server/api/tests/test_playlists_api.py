"""Tests for the v2 playlist endpoints.

Additive API surface only — the flat v1/v1.1/v1.2/v2 asset endpoints
are pinned by their own suites and must not change shape.
"""

from datetime import timedelta
from typing import Any

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from anthias_server.app.models import (
    Asset,
    Playlist,
    get_default_playlist,
)


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


def _create_asset(asset_id: str) -> Asset:
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


def _create_playlist(
    api_client: APIClient, name: str, **extra: Any
) -> dict[str, Any]:
    response = api_client.post(
        reverse('api:playlist_list_v2'),
        data={'name': name, **extra},
        format='json',
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data
    result: dict[str, Any] = response.data
    return result


def _add_item(api_client: APIClient, playlist_id: str, **body: Any) -> Any:
    return api_client.post(
        reverse('api:playlist_items_v2', args=[playlist_id]),
        data=body,
        format='json',
    )


@pytest.mark.django_db
def test_list_includes_backfilled_default_playlist(
    api_client: APIClient,
) -> None:
    response = api_client.get(reverse('api:playlist_list_v2'))
    assert response.status_code == status.HTTP_200_OK
    defaults = [p for p in response.data if p['is_default']]
    assert len(defaults) == 1
    assert defaults[0]['repeat'] is True
    assert defaults[0]['parent_id'] is None


@pytest.mark.django_db
def test_create_playlist_defaults(api_client: APIClient) -> None:
    created = _create_playlist(api_client, 'My playlist')
    assert created['repeat'] is True
    assert created['is_enabled'] is True
    assert created['is_default'] is False
    assert created['items'] == []
    assert created['play_days'] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.django_db
def test_patch_playlist_schedule_and_repeat(
    api_client: APIClient,
) -> None:
    created = _create_playlist(api_client, 'p')
    response = api_client.patch(
        reverse('api:playlist_detail_v2', args=[created['playlist_id']]),
        data={
            'repeat': False,
            'play_days': [6, 7],
            'play_time_from': '09:00',
            'play_time_to': '17:00',
        },
        format='json',
    )
    assert response.status_code == status.HTTP_200_OK, response.data
    assert response.data['repeat'] is False
    assert response.data['play_days'] == [6, 7]

    playlist = Playlist.objects.get(playlist_id=created['playlist_id'])
    assert playlist.repeat is False
    assert playlist.get_play_days() == [6, 7]


@pytest.mark.django_db
def test_partial_time_window_is_rejected(api_client: APIClient) -> None:
    created = _create_playlist(api_client, 'p')
    response = api_client.patch(
        reverse('api:playlist_detail_v2', args=[created['playlist_id']]),
        data={'play_time_from': '09:00'},
        format='json',
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_inverted_date_range_is_rejected(api_client: APIClient) -> None:
    now = timezone.now()
    response = api_client.post(
        reverse('api:playlist_list_v2'),
        data={
            'name': 'p',
            'start_date': now.isoformat(),
            'end_date': (now - timedelta(days=1)).isoformat(),
        },
        format='json',
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_default_playlist_cannot_be_deleted(
    api_client: APIClient,
) -> None:
    default = get_default_playlist()
    response = api_client.delete(
        reverse('api:playlist_detail_v2', args=[default.playlist_id])
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert Playlist.objects.filter(is_default=True).exists()


@pytest.mark.django_db
def test_add_same_asset_twice_creates_two_items(
    api_client: APIClient,
) -> None:
    asset = _create_asset('a')
    created = _create_playlist(api_client, 'p')
    for _ in range(2):
        response = _add_item(
            api_client, created['playlist_id'], asset_id=asset.asset_id
        )
        assert response.status_code == status.HTTP_201_CREATED

    items = response.data['items']
    assert [item['asset_id'] for item in items] == ['a', 'a']
    assert items[0]['id'] != items[1]['id']


@pytest.mark.django_db
def test_nest_playlist_and_reject_second_parent(
    api_client: APIClient,
) -> None:
    parent_a = _create_playlist(api_client, 'a')
    parent_b = _create_playlist(api_client, 'b')
    child = _create_playlist(api_client, 'child')

    ok = _add_item(
        api_client,
        parent_a['playlist_id'],
        child_playlist_id=child['playlist_id'],
    )
    assert ok.status_code == status.HTTP_201_CREATED

    refused = _add_item(
        api_client,
        parent_b['playlist_id'],
        child_playlist_id=child['playlist_id'],
    )
    assert refused.status_code == status.HTTP_400_BAD_REQUEST

    detail = api_client.get(
        reverse('api:playlist_detail_v2', args=[child['playlist_id']])
    )
    assert detail.data['parent_id'] == parent_a['playlist_id']


@pytest.mark.django_db
def test_nesting_cycle_is_rejected(api_client: APIClient) -> None:
    outer = _create_playlist(api_client, 'outer')
    inner = _create_playlist(api_client, 'inner')
    assert (
        _add_item(
            api_client,
            outer['playlist_id'],
            child_playlist_id=inner['playlist_id'],
        ).status_code
        == status.HTTP_201_CREATED
    )

    # inner -> outer would close the loop.
    refused = _add_item(
        api_client,
        inner['playlist_id'],
        child_playlist_id=outer['playlist_id'],
    )
    assert refused.status_code == status.HTTP_400_BAD_REQUEST

    # Self-nesting is the 1-cycle.
    refused = _add_item(
        api_client,
        outer['playlist_id'],
        child_playlist_id=outer['playlist_id'],
    )
    assert refused.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_default_playlist_cannot_be_nested(
    api_client: APIClient,
) -> None:
    parent = _create_playlist(api_client, 'p')
    default = get_default_playlist()
    refused = _add_item(
        api_client,
        parent['playlist_id'],
        child_playlist_id=default.playlist_id,
    )
    assert refused.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_item_requires_exactly_one_target(api_client: APIClient) -> None:
    asset = _create_asset('a')
    other = _create_playlist(api_client, 'other')
    playlist = _create_playlist(api_client, 'p')

    neither = _add_item(api_client, playlist['playlist_id'])
    assert neither.status_code == status.HTTP_400_BAD_REQUEST

    both = _add_item(
        api_client,
        playlist['playlist_id'],
        asset_id=asset.asset_id,
        child_playlist_id=other['playlist_id'],
    )
    assert both.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_remove_item_unnests_child_without_deleting_it(
    api_client: APIClient,
) -> None:
    parent = _create_playlist(api_client, 'parent')
    child = _create_playlist(api_client, 'child')
    added = _add_item(
        api_client,
        parent['playlist_id'],
        child_playlist_id=child['playlist_id'],
    )
    item_id = added.data['items'][0]['id']

    response = api_client.delete(
        reverse(
            'api:playlist_item_v2',
            args=[parent['playlist_id'], item_id],
        )
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    # The child playlist survives as a root again.
    detail = api_client.get(
        reverse('api:playlist_detail_v2', args=[child['playlist_id']])
    )
    assert detail.status_code == status.HTTP_200_OK
    assert detail.data['parent_id'] is None


@pytest.mark.django_db
def test_delete_playlist_never_deletes_assets(
    api_client: APIClient,
) -> None:
    asset = _create_asset('a')
    playlist = _create_playlist(api_client, 'p')
    _add_item(api_client, playlist['playlist_id'], asset_id=asset.asset_id)

    response = api_client.delete(
        reverse('api:playlist_detail_v2', args=[playlist['playlist_id']])
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    assert Asset.objects.filter(asset_id='a').exists()


@pytest.mark.django_db
def test_reorder_playlist_items(api_client: APIClient) -> None:
    a, b, c = (_create_asset(i) for i in ('a', 'b', 'c'))
    playlist = _create_playlist(api_client, 'p')
    for asset in (a, b, c):
        response = _add_item(
            api_client, playlist['playlist_id'], asset_id=asset.asset_id
        )
    item_ids = [item['id'] for item in response.data['items']]

    reordered = list(reversed(item_ids))
    response = api_client.post(
        reverse('api:playlist_items_order_v2', args=[playlist['playlist_id']]),
        data={'ids': ','.join(str(i) for i in reordered)},
        format='json',
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT

    detail = api_client.get(
        reverse('api:playlist_detail_v2', args=[playlist['playlist_id']])
    )
    assert [item['asset_id'] for item in detail.data['items']] == [
        'c',
        'b',
        'a',
    ]


@pytest.mark.django_db
def test_reorder_rejects_foreign_item_ids(api_client: APIClient) -> None:
    asset = _create_asset('a')
    mine = _create_playlist(api_client, 'mine')
    other = _create_playlist(api_client, 'other')
    added = _add_item(
        api_client, other['playlist_id'], asset_id=asset.asset_id
    )
    foreign_item_id = added.data['items'][0]['id']

    response = api_client.post(
        reverse('api:playlist_items_order_v2', args=[mine['playlist_id']]),
        data={'ids': str(foreign_item_id)},
        format='json',
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
def test_insert_item_at_position(api_client: APIClient) -> None:
    a, b, c = (_create_asset(i) for i in ('a', 'b', 'c'))
    playlist = _create_playlist(api_client, 'p')
    _add_item(api_client, playlist['playlist_id'], asset_id=a.asset_id)
    _add_item(api_client, playlist['playlist_id'], asset_id=b.asset_id)
    response = _add_item(
        api_client, playlist['playlist_id'], asset_id=c.asset_id, position=1
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert [item['asset_id'] for item in response.data['items']] == [
        'a',
        'c',
        'b',
    ]
