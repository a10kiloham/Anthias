// Drag-to-reorder for the /playlists page.
//
// Same vanilla pointer-events approach as home.ts (see the rationale
// there for why not SortableJS): the dragged <tr> is reinserted live
// as the cursor crosses row midpoints, and pointerup POSTs the
// resulting PlaylistItem-id sequence to the per-playlist order
// endpoint. Each playlist card's tbody is its own drag scope — rows
// never move between playlists (cross-playlist moves go through the
// remove + add controls, which re-validate nesting rules
// server-side).

// ---------------------------------------------------------------------------
// Playlist card expand/collapse state.
//
// The tree is swapped wholesale (hx-swap="outerHTML") by the 10 s
// background poll and after every write, which tears down the Alpine
// components and any x-data state with them. A card's open/closed
// state therefore lives in this module, keyed by playlist id, so the
// cards an operator expanded stay expanded across swaps. Every card
// starts collapsed: the header row (name, chips, item count, total
// runtime) is the overview, the item table is on demand.
const openCards = new Set<string>()

interface PlaylistCardData {
  open: boolean
  scheduleOpen: boolean
  addOpen: boolean
  q: string
  sel: number
  init(): void
}

function playlistCard(playlistId: string): PlaylistCardData {
  return {
    open: openCards.has(playlistId),
    scheduleOpen: false,
    addOpen: false,
    q: '',
    sel: 0,
    init(
      this: PlaylistCardData & {
        $watch: (key: string, cb: (value: boolean) => void) => void
      },
    ) {
      this.$watch('open', (value) => {
        if (value) {
          openCards.add(playlistId)
        } else {
          openCards.delete(playlistId)
        }
      })
    },
  }
}

// Gate for the background poll (the `[window.playlistTreeIdle()]`
// condition on #playlist-tree's hx-trigger): skip the refresh while
// the operator is interacting with the tree — an open schedule/add
// panel or focus inside the tree means the swap would wipe half-typed
// input. Event-driven refreshes (refresh-assets) are not gated; they
// fire after the tree's own writes, when losing panel state is the
// expected "done" transition.
function playlistTreeIdle(): boolean {
  const tree = document.getElementById('playlist-tree')
  if (!tree) return true
  const active = document.activeElement
  if (active && active !== document.body && tree.contains(active)) {
    return false
  }
  const panels = tree.querySelectorAll<HTMLElement>('.playlist-card__panel')
  // offsetParent is null while an x-show has the panel display:none.
  return !Array.from(panels).some((panel) => panel.offsetParent !== null)
}

declare global {
  interface Window {
    playlistCard: (playlistId: string) => PlaylistCardData
    playlistTreeIdle: () => boolean
  }
}

window.playlistCard = playlistCard
window.playlistTreeIdle = playlistTreeIdle

function csrfToken(): string {
  const fromForm = document.querySelector<HTMLInputElement>(
    'input[name=csrfmiddlewaretoken]',
  )
  if (fromForm) return fromForm.value
  const cookieMatch = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/)
  return cookieMatch ? decodeURIComponent(cookieMatch[1]) : ''
}

type HtmxLike = { trigger: (target: string, event: string) => void }

function postItemOrder(orderUrl: string, tbody: HTMLElement): void {
  const ids = Array.from(tbody.children)
    .map((tr) => (tr as HTMLElement).dataset.itemId)
    .filter(Boolean)
    .join(',')
  const fd = new FormData()
  fd.append('ids', ids)
  const refresh = () =>
    (window as unknown as { htmx: HtmxLike }).htmx.trigger(
      'body',
      'refresh-assets',
    )
  fetch(orderUrl, {
    method: 'POST',
    body: fd,
    headers: {
      'X-CSRFToken': csrfToken(),
      'HX-Request': 'true',
    },
  })
    .then((r) => {
      if (!r.ok) {
        console.error('playlist reorder POST failed:', r.status, r.statusText)
      }
      refresh()
    })
    .catch((err) => {
      console.error('playlist reorder POST errored:', err)
      refresh()
    })
}

function bindPlaylistRowsDrag(tbody: HTMLElement, orderUrl: string): void {
  if (tbody.dataset.dragBound === '1') return
  tbody.dataset.dragBound = '1'

  let dragRow: HTMLTableRowElement | null = null
  let pointerId = -1
  let moved = false

  const cleanup = (): void => {
    document.removeEventListener('pointermove', onMove)
    document.removeEventListener('pointerup', onUp)
    document.removeEventListener('pointercancel', onUp)
    if (dragRow) {
      dragRow.classList.remove('is-dragging')
      dragRow = null
    }
  }

  const onMove = (ev: PointerEvent): void => {
    if (!dragRow || ev.pointerId !== pointerId) return
    const overEl = document.elementFromPoint(ev.clientX, ev.clientY)
    const overRow = overEl?.closest('tr') as HTMLTableRowElement | null
    if (!overRow || overRow === dragRow) return
    if (overRow.parentElement !== tbody) return
    const rect = overRow.getBoundingClientRect()
    const before = ev.clientY < rect.top + rect.height / 2
    tbody.insertBefore(dragRow, before ? overRow : overRow.nextSibling)
    moved = true
  }

  const onUp = (ev: PointerEvent): void => {
    if (ev.pointerId !== pointerId) return
    const didMove = moved
    cleanup()
    if (didMove) postItemOrder(orderUrl, tbody)
  }

  tbody.addEventListener('pointerdown', (ev: PointerEvent) => {
    if (ev.button !== 0) return
    const handle = (ev.target as HTMLElement).closest('.drag-handle')
    if (!handle) return
    const row = handle.closest('tr') as HTMLTableRowElement | null
    if (!row || row.parentElement !== tbody) return

    ev.preventDefault()
    dragRow = row
    pointerId = ev.pointerId
    moved = false
    dragRow.classList.add('is-dragging')

    document.addEventListener('pointermove', onMove)
    document.addEventListener('pointerup', onUp)
    document.addEventListener('pointercancel', onUp)
  })
}

function setupPlaylistDrag(): void {
  const wrapper = document.getElementById('playlist-tree')
  if (!wrapper) return
  wrapper
    .querySelectorAll<HTMLElement>('tbody[data-order-url]')
    .forEach((tbody) => {
      const orderUrl = tbody.dataset.orderUrl
      if (orderUrl) bindPlaylistRowsDrag(tbody, orderUrl)
    })
}

function bootOnce(): void {
  setupPlaylistDrag()
  // Every write endpoint swaps #playlist-tree wholesale, discarding
  // the bound tbodies — re-bind on each settle.
  document.body.addEventListener('htmx:afterSwap', () => {
    setupPlaylistDrag()
  })
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bootOnce, { once: true })
} else {
  bootOnce()
}
