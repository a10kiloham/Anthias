"""Template filters for the asset list page.

`to_json` serialises an Asset (or any model instance) to a JSON string
safe for inlining into an Alpine `@click` handler. The form-modal opens
edit mode by reading these inline blobs rather than refetching from the
API — keeps the row markup self-contained.

`playback_status` reduces a row's state to a {kind, primary,
secondary} descriptor: disabled, live, or off-window right now.
"""

import json
from datetime import date, datetime, time
from typing import Any

from django.template import Library
from django.utils.safestring import SafeString, mark_safe

from anthias_server.settings import settings

register = Library()


_DAY_LABELS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


@register.filter
def schedule_pills(asset: Any) -> list[dict[str, str]]:
    """Return the schedule window as a list of pill descriptors.

    Each pill is a {kind, label} dict the row template iterates over.
    `kind` is one of:
      - 'all'  — shorthand "Everyday" pill, emitted only when no
                 day-of-week filter narrows the schedule.
      - 'day'  — one per active weekday: 'Mon', 'Tue', ...
      - 'time' — the play_time_from/to window, formatted in the
                 device's configured 24h/12h clock.

    The list collapses to a single 'all' pill when the asset has no
    day filter and no time window — the row then renders a green-ish
    chip rather than a wall of seven Mon/Tue/Wed pills.
    """
    pills: list[dict[str, str]] = []
    days_set: list[int] = []
    if hasattr(asset, 'get_play_days'):
        days_set = asset.get_play_days()
    full_week = days_set == list(range(1, 8))

    pf = getattr(asset, 'play_time_from', None)
    pt = getattr(asset, 'play_time_to', None)

    if not full_week and days_set:
        for d in days_set:
            if 1 <= d <= 7:
                pills.append({'kind': 'day', 'label': _DAY_LABELS[d - 1]})
    elif not (pf and pt):
        # Full-week plays that also play all hours get the catch-all
        # "Everyday" pill so the row reads "Everyday" instead of
        # nothing — matches the tooltip we used to show.
        pills.append({'kind': 'all', 'label': 'Everyday'})

    if pf and pt:
        fmt = '%H:%M' if settings['use_24_hour_clock'] else '%I:%M %p'
        pills.append(
            {
                'kind': 'time',
                'label': (
                    f'{pf.strftime(fmt).lstrip("0")} – '
                    f'{pt.strftime(fmt).lstrip("0")}'
                ),
            }
        )
    return pills


def _to_dict(obj: Any) -> Any:
    if hasattr(obj, '_meta'):
        out: dict[str, Any] = {}
        for field in obj._meta.fields:
            value = getattr(obj, field.name)
            out[field.name] = _coerce(value)
        # Normalise play_days to a list[int] so the day-of-week
        # checkboxes can `.includes(day)` straight off Alpine state.
        # The TextField stores JSON; get_play_days() handles the parse
        # + clamp to 1-7.
        if hasattr(obj, 'get_play_days'):
            out['play_days_list'] = obj.get_play_days()
        # play_time_from / play_time_to are TimeFields that serialise
        # as ISO strings (HH:MM:SS); the <input type="time"> binding
        # wants HH:MM. Trim if present.
        for key in ('play_time_from', 'play_time_to'):
            v = out.get(key)
            if isinstance(v, str) and len(v) >= 5:
                out[key] = v[:5]
        # Sanitise metadata['refresh_interval_s'] before it reaches the
        # edit modal: a legacy / hand-edited row with an out-of-range
        # value would otherwise put the <input type="number" max="...">
        # in :invalid state and block form submission entirely (the
        # operator couldn't save *any* changes). Mirrors the v2 API's
        # response normalisation via the shared clamp helper.
        meta = out.get('metadata')
        if isinstance(meta, dict) and 'refresh_interval_s' in meta:
            from anthias_server.app.models import clamp_refresh_interval

            meta = dict(meta)
            meta['refresh_interval_s'] = clamp_refresh_interval(
                meta['refresh_interval_s']
            )
            out['metadata'] = meta
        # Same clamp for ``loops`` so a hand-edited value can't put the
        # edit modal's <input type="number" min/max> in :invalid state
        # and block the whole form.
        meta = out.get('metadata')
        if isinstance(meta, dict) and 'loops' in meta:
            from anthias_server.app.models import clamp_loops

            meta = dict(meta)
            meta['loops'] = clamp_loops(meta['loops'])
            out['metadata'] = meta
        # Sanitise metadata['headers'] the same way, so a legacy /
        # hand-edited row can't seed the edit modal's textarea with an
        # unsafe (CR/LF) value or a non-string blob (#2215).
        meta = out.get('metadata')
        if isinstance(meta, dict) and 'headers' in meta:
            from anthias_server.app.models import normalize_asset_headers

            meta = dict(meta)
            meta['headers'] = normalize_asset_headers(meta['headers'])
            out['metadata'] = meta
        return out
    return _coerce(obj)


def _coerce(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


@register.filter
def to_json(value: Any) -> SafeString:
    """Render an Asset (or any model) as a JSON literal for Alpine."""
    encoded = json.dumps(
        _to_dict(value),
        default=str,
        separators=(',', ':'),
    )
    # mark_safe lets `'` survive HTML autoescaping; we still hex-encode
    # the apostrophe and ampersand below to keep the literal valid as
    # a JS string inside an `x-on:click="openEdit(...)"` attribute.
    safe = (
        encoded.replace('&', '\\u0026')
        .replace("'", '\\u0027')
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
    )
    return mark_safe(safe)


@register.filter
def asset_ids(assets: Any) -> str:
    """Render a list of assets as a JSON array of their asset_ids.

    Inlined into the asset-table partial's ``x-init`` so Alpine knows
    which rows are on screen after every HTMX swap (drives the bulk
    select-all + selection pruning, #3046).

    Unlike ``to_json`` (which mark_safe's its output because it's
    embedded in a *single*-quoted Alpine attribute and hand-escapes the
    apostrophe), this value lands in a *double*-quoted ``x-init="…"``
    attribute. The JSON's own ``"`` would prematurely close that
    attribute, so we deliberately return a plain ``str`` and let
    Django's template autoescaping HTML-encode ``"``/``&``/``<``/``>``
    to entities — the browser decodes them back to valid JSON before
    Alpine evaluates the expression. Returning a SafeString here would
    suppress that escaping and break the markup.
    """
    ids = [getattr(a, 'asset_id', '') for a in (assets or [])]
    return json.dumps(ids, separators=(',', ':'))


@register.filter
def playback_status(asset: Any) -> dict[str, str]:
    """Return a structured status descriptor for an asset or playlist.

    There is no date-based expiration, so the states collapse to:
    disabled, live (in rotation right now), or scheduled (enabled but
    outside its day-of-week / time-of-day window at this moment).
    `kind` ∈ {'live', 'scheduled', 'disabled'} keys the dot colour.
    Works for Playlist rows too via their ``admits`` predicate.
    """
    if not getattr(asset, 'is_enabled', True):
        return {'kind': 'disabled', 'primary': 'Disabled', 'secondary': ''}

    if hasattr(asset, 'is_active'):
        in_window = bool(asset.is_active())
    elif hasattr(asset, 'admits'):
        in_window = bool(asset.admits())
    else:
        in_window = True
    if not in_window:
        return {
            'kind': 'scheduled',
            'primary': 'Off-window now',
            'secondary': 'Plays inside its weekday / time window',
        }
    return {'kind': 'live', 'primary': 'Active', 'secondary': ''}


@register.filter
def asset_loops(asset: Any) -> int:
    """Clamped ``metadata['loops']`` for the table's Loops column —
    defaults to 1 (play once) when unset or junk."""
    from anthias_server.app.models import clamp_loops

    metadata = getattr(asset, 'metadata', None) or {}
    if not isinstance(metadata, dict):
        return 1
    return clamp_loops(metadata.get('loops', 1))


@register.filter
def humanize_duration(value: Any) -> str:
    """Format a duration in seconds as 'Xh Ym', 'Xm Ys', or 'Xs'.

    Asset.duration is stored as integer seconds. The schedule table
    used to render '42 sec' / '3600 sec' which scans poorly for long
    streams; this filter renders the same value as '42s', '1m 30s',
    '1h 5m'. Drops the seconds component once we're into hours so
    a 1h05m02s stream doesn't read like a stopwatch.
    """
    try:
        total = int(value)
    except (TypeError, ValueError):
        return ''
    if total <= 0:
        return '0s'
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f'{hours}h')
        if minutes:
            parts.append(f'{minutes}m')
        return ' '.join(parts)
    if minutes:
        parts.append(f'{minutes}m')
        if seconds:
            parts.append(f'{seconds}s')
        return ' '.join(parts)
    return f'{seconds}s'
