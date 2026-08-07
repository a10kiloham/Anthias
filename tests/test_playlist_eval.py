"""Tests for first-class playlists: expansion to occurrences, window
ANDing, deadline generalisation, and the viewer Scheduler's
repeat=False (play-once-per-activation) semantics.

The flat-playlist behaviour (ordering, windows, deadlines without any
named playlists) stays pinned by tests/test_scheduler.py — everything
there runs against the backfilled Default playlist and must keep
passing unchanged. This file covers what only exists now that
playlists are entities.
"""

import itertools
from datetime import datetime, time, timedelta
from typing import Any

import pytest
import time_machine
from django.utils import timezone

from anthias_server.app.models import (
    Asset,
    Playlist,
    PlaylistItem,
    get_default_playlist,
)
from anthias_server.app.playlist_eval import (
    WINDOWED_DEADLINE_CAP_SECONDS,
    evaluate_playlist,
    expand_occurrences,
)
from anthias_server.settings import settings
from anthias_viewer.scheduling import Scheduler

_DEFAULT_PLAY_DAYS = '[1, 2, 3, 4, 5, 6, 7]'


def _make_asset(asset_id: str, **overrides: Any) -> Asset:
    now = timezone.now()
    payload: dict[str, Any] = {
        'asset_id': asset_id,
        'name': asset_id,
        'uri': f'https://example.com/{asset_id}.png',
        'mimetype': 'image',
        'duration': 5,
        'is_enabled': True,
        'is_processing': False,
        'nocache': False,
        'play_order': 0,
        'start_date': now - timedelta(days=1),
        'end_date': now + timedelta(days=30),
        'play_days': _DEFAULT_PLAY_DAYS,
    }
    payload.update(overrides)
    return Asset.objects.create(**payload)


def _make_playlist(name: str, **overrides: Any) -> Playlist:
    return Playlist.objects.create(name=name, **overrides)


def _add(
    playlist: Playlist,
    *,
    asset: Asset | None = None,
    child: Playlist | None = None,
    position: int | None = None,
) -> PlaylistItem:
    if position is None:
        last = playlist.items.order_by('-position').first()
        position = last.position + 1 if last else 0
    return PlaylistItem.objects.create(
        playlist=playlist,
        asset=asset,
        child_playlist=child,
        position=position,
    )


def _remove_from_default(asset: Asset) -> None:
    """Detach an asset from the Default playlist so a test can place
    it only where it intends to."""
    PlaylistItem.objects.filter(
        playlist=get_default_playlist(), asset=asset
    ).delete()


@pytest.fixture(autouse=True)
def _shuffle_off() -> Any:
    original = settings.get('shuffle_playlist', False)
    settings['shuffle_playlist'] = False
    try:
        yield
    finally:
        settings['shuffle_playlist'] = original


@pytest.mark.django_db
def test_new_assets_land_in_default_playlist() -> None:
    asset = _make_asset('a')
    default = get_default_playlist()
    assert list(default.items.values_list('asset_id', flat=True)) == [
        asset.asset_id
    ]


@pytest.mark.django_db
def test_asset_in_two_playlists_yields_two_occurrences() -> None:
    asset = _make_asset('a')
    _remove_from_default(asset)
    first = _make_playlist('first', position=1)
    second = _make_playlist('second', position=2)
    _add(first, asset=asset)
    _add(second, asset=asset)

    occurrences, _ = evaluate_playlist()
    assert [o.asset.asset_id for o in occurrences] == ['a', 'a']
    assert len({o.occurrence_id for o in occurrences}) == 2


@pytest.mark.django_db
def test_asset_twice_in_one_playlist_yields_two_occurrences() -> None:
    asset = _make_asset('a')
    _remove_from_default(asset)
    playlist = _make_playlist('p')
    _add(playlist, asset=asset)
    _add(playlist, asset=asset)

    occurrences, _ = evaluate_playlist()
    assert [o.asset.asset_id for o in occurrences] == ['a', 'a']
    assert len({o.occurrence_id for o in occurrences}) == 2


@pytest.mark.django_db
def test_nested_expansion_is_depth_first_in_item_order() -> None:
    a, b, c = _make_asset('a'), _make_asset('b'), _make_asset('c')
    for asset in (a, b, c):
        _remove_from_default(asset)
    parent = _make_playlist('parent')
    child = _make_playlist('child')
    _add(parent, asset=a, position=0)
    _add(parent, child=child, position=1)
    _add(parent, asset=c, position=2)
    _add(child, asset=b)

    occurrences, _ = evaluate_playlist()
    assert [o.asset.asset_id for o in occurrences] == ['a', 'b', 'c']


@pytest.mark.django_db
def test_disabling_a_playlist_gates_its_whole_subtree() -> None:
    asset = _make_asset('a')
    # Default playlist is the only container; disabling it empties the
    # screen (feasibility doc phase-2 acceptance test).
    default = get_default_playlist()
    default.is_enabled = False
    default.save()

    occurrences, _ = evaluate_playlist()
    assert occurrences == []
    # The asset itself is still active; only the container gates it.
    assert asset.is_active()


@pytest.mark.django_db
def test_disabled_nested_playlist_gates_only_its_subtree() -> None:
    _make_asset('a')
    b = _make_asset('b')
    _remove_from_default(b)
    child = _make_playlist('child', is_enabled=False)
    _add(get_default_playlist(), child=child)
    _add(child, asset=b)

    occurrences, _ = evaluate_playlist()
    assert [o.asset.asset_id for o in occurrences] == ['a']


@pytest.mark.django_db
def test_playlist_date_window_is_anded_with_asset_window() -> None:
    now = timezone.now()
    asset = _make_asset('a')
    _remove_from_default(asset)
    future = _make_playlist('future', start_date=now + timedelta(days=2))
    _add(future, asset=asset)

    occurrences, deadline = evaluate_playlist(now)
    assert occurrences == []
    # Inactive because the container hasn't opened: the deadline is the
    # latest future open along the path — the playlist's start.
    assert deadline == future.start_date


@pytest.mark.django_db
def test_active_deadline_is_earliest_close_on_the_path() -> None:
    now = timezone.now()
    asset = _make_asset('a')  # asset closes in 30 days
    _remove_from_default(asset)
    closing = _make_playlist('closing', end_date=now + timedelta(hours=1))
    _add(closing, asset=asset)

    occurrences, deadline = evaluate_playlist(now)
    assert [o.asset.asset_id for o in occurrences] == ['a']
    assert deadline == closing.end_date


@pytest.mark.django_db
def test_expired_container_contributes_no_deadline() -> None:
    now = timezone.now()
    asset = _make_asset('a')
    _remove_from_default(asset)
    expired = _make_playlist('expired', end_date=now - timedelta(hours=1))
    _add(expired, asset=asset)

    occurrences, deadline = evaluate_playlist(now)
    assert occurrences == []
    # A window that already closed can never reopen; pinning the
    # deadline to a past boundary would make every tick "overdue".
    assert deadline is None


@pytest.mark.django_db
def test_playlist_time_window_forces_60s_cap() -> None:
    now = timezone.now()
    asset = _make_asset('a')
    _remove_from_default(asset)
    windowed = _make_playlist(
        'windowed',
        play_time_from=time(0, 0),
        play_time_to=time(23, 59),
    )
    _add(windowed, asset=asset)

    _, deadline = evaluate_playlist(now)
    assert deadline is not None
    assert deadline <= now + timedelta(seconds=WINDOWED_DEADLINE_CAP_SECONDS)


@pytest.mark.django_db
def test_container_window_blocks_but_out_of_date_asset_does_not_cap() -> None:
    """An asset outside its own dates inside a windowed container must
    not force 60s polling — the intersected range doesn't contain now."""
    now = timezone.now()
    asset = _make_asset(
        'a',
        start_date=now + timedelta(days=5),
        end_date=now + timedelta(days=6),
    )
    _remove_from_default(asset)
    windowed = _make_playlist(
        'windowed',
        play_time_from=time(0, 0),
        play_time_to=time(23, 59),
    )
    _add(windowed, asset=asset)

    _, deadline = evaluate_playlist(now)
    # Deadline must be the asset's future start, not the 60s cap.
    assert deadline == asset.start_date


@pytest.mark.django_db
def test_unreachable_two_node_cycle_is_dropped_not_hung() -> None:
    a = _make_asset('a')
    _remove_from_default(a)
    p1 = _make_playlist('p1')
    p2 = _make_playlist('p2')
    _add(p1, child=p2)
    _add(p2, asset=a)
    # Hand-create the back edge (the API refuses this): p2 -> p1.
    _add(p2, child=p1)

    # Neither p1 nor p2 is a root any more, so the cycle is simply
    # unreachable; expansion returns without hanging.
    occurrences = expand_occurrences()
    assert [o.asset.asset_id for o in occurrences] == []


@pytest.mark.django_db
def test_nesting_deeper_than_cap_drops_the_tail() -> None:
    chain = [_make_playlist(f'p{i}') for i in range(10)]
    for parent, child in itertools.pairwise(chain):
        _add(parent, child=child)
    deep_asset = _make_asset('deep')
    _remove_from_default(deep_asset)
    _add(chain[-1], asset=deep_asset)
    shallow_asset = _make_asset('shallow')
    _remove_from_default(shallow_asset)
    _add(chain[0], asset=shallow_asset)

    occurrences = expand_occurrences()
    assert [o.asset.asset_id for o in occurrences] == ['shallow']


@pytest.mark.django_db
def test_no_repeat_playlist_plays_each_occurrence_once() -> None:
    a, b = _make_asset('a'), _make_asset('b')
    for asset in (a, b):
        _remove_from_default(asset)
    once = _make_playlist('once', repeat=False)
    _add(once, asset=a)
    _add(once, asset=b)

    scheduler = Scheduler()
    first = scheduler.get_next_asset()
    second = scheduler.get_next_asset()
    assert first is not None and second is not None
    assert {first['asset_id'], second['asset_id']} == {'a', 'b'}
    # Play-through complete: nothing else is scheduled, so the screen
    # goes empty rather than looping.
    assert scheduler.get_next_asset() is None
    assert scheduler.get_next_asset() is None


@pytest.mark.django_db
def test_repeat_playlist_loops_forever_by_default() -> None:
    _make_asset('a')

    scheduler = Scheduler()
    played = [scheduler.get_next_asset() for _ in range(5)]
    assert all(p is not None and p['asset_id'] == 'a' for p in played)


@pytest.mark.django_db
def test_no_repeat_subtree_exhausts_while_siblings_keep_looping() -> None:
    _make_asset('looping')
    one_shot = _make_asset('one-shot')
    _remove_from_default(one_shot)
    once = _make_playlist('once', repeat=False)
    _add(get_default_playlist(), child=once)
    _add(once, asset=one_shot)

    scheduler = Scheduler()
    seen = [
        asset['asset_id']
        for _ in range(6)
        if (asset := scheduler.get_next_asset()) is not None
    ]
    assert seen.count('one-shot') == 1
    assert seen.count('looping') == 5


@pytest.mark.django_db
def test_no_repeat_state_resets_when_membership_changes() -> None:
    a = _make_asset('a')
    _remove_from_default(a)
    once = _make_playlist('once', repeat=False)
    _add(once, asset=a)

    scheduler = Scheduler()
    first = scheduler.get_next_asset()
    assert first is not None and first['asset_id'] == 'a'
    assert scheduler.get_next_asset() is None

    # Operator edits the playlist (adds another occurrence): a fresh
    # play-through starts for the new membership.
    b = _make_asset('b')
    _remove_from_default(b)
    _add(once, asset=b)
    scheduler.update_playlist()

    replay = {
        asset['asset_id']
        for _ in range(2)
        if (asset := scheduler.get_next_asset()) is not None
    }
    assert replay == {'a', 'b'}
    assert scheduler.get_next_asset() is None


@pytest.mark.django_db
def test_no_repeat_state_resets_after_activation_window_flip() -> None:
    """Close the playlist's window, reopen it: the play-through
    restarts (once per activation window)."""
    a = _make_asset('a')
    _remove_from_default(a)
    now = timezone.now()
    once = _make_playlist(
        'once', repeat=False, end_date=now + timedelta(hours=1)
    )
    _add(once, asset=a)

    scheduler = Scheduler()
    first = scheduler.get_next_asset()
    assert first is not None and first['asset_id'] == 'a'
    assert scheduler.get_next_asset() is None

    # Travel past the window's close (membership -> empty, state
    # resets), then reopen the window by extending end_date.
    with time_machine.travel(now + timedelta(hours=2)):
        assert scheduler.get_next_asset() is None
        Playlist.objects.filter(pk=once.pk).update(
            end_date=timezone.now() + timedelta(hours=1)
        )
        scheduler.update_playlist()
        replay = scheduler.get_next_asset()
        assert replay is not None and replay['asset_id'] == 'a'


@pytest.mark.django_db
def test_duplicate_occurrences_survive_cap_driven_refresh() -> None:
    """The shuffle membership guard keys on occurrence ids, so a
    playlist holding the same asset twice must not be treated as
    changed membership on every cap-driven refresh."""
    asset = _make_asset('a')
    _remove_from_default(asset)
    playlist = _make_playlist(
        'p',
        play_time_from=time(0, 0),
        play_time_to=time(23, 59),
    )
    _add(playlist, asset=asset)
    _add(playlist, asset=asset)

    settings['shuffle_playlist'] = True
    scheduler = Scheduler()
    order_before = [a['occurrence_id'] for a in scheduler.assets]
    assert len(order_before) == 2
    # Simulate the windowed 60s cap refresh: membership unchanged, so
    # the play-through order must be preserved exactly.
    scheduler.update_playlist()
    assert [a['occurrence_id'] for a in scheduler.assets] == order_before


def _aware(
    year: int, month: int, day: int, hour: int, minute: int = 0
) -> datetime:
    return timezone.make_aware(
        datetime(year, month, day, hour, minute),  # noqa: DTZ001
        timezone.get_current_timezone(),
    )


@pytest.mark.django_db
def test_playlist_play_days_gate_the_subtree() -> None:
    # 2026-01-05 is a Monday.
    asset_kwargs = {
        'start_date': _aware(2025, 1, 1, 0),
        'end_date': _aware(2027, 1, 1, 0),
    }
    asset = _make_asset('a', **asset_kwargs)
    _remove_from_default(asset)
    weekend_only = _make_playlist('weekends', play_days='[6, 7]')
    _add(weekend_only, asset=asset)

    with time_machine.travel(_aware(2026, 1, 5, 12)):  # Monday
        occurrences, _ = evaluate_playlist()
        assert occurrences == []
    with time_machine.travel(_aware(2026, 1, 10, 12)):  # Saturday
        occurrences, _ = evaluate_playlist()
        assert [o.asset.asset_id for o in occurrences] == ['a']
