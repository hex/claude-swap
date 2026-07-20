"""Shared render widgets: usage bars, account cards, and the accounts panel.

``bar_cells``/``usage_bar`` are custom renderers rather than Textual's
``ProgressBar`` because the design needs three things the stock widget
doesn't do: a severity color ramp, an optional threshold tick mark (the
auto-switch trigger line), and stale-measurement dimming.
"""

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import ListItem, Static

from claude_swap import pace
from claude_swap.json_output import USAGE_API_KEY
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.usage_store import STALE_OK_S
from claude_swap.tui import data
from claude_swap.tui.theme import (
    ACCENT,
    FOREGROUND,
    MUTED,
    SEV_CRIT,
    SEV_OK,
    SEV_WARN,
    TRACK,
    severity_color,
)

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_BAR_FILLED = "━"
_BAR_HALF = "╸"
_BAR_EMPTY = "─"
_BAR_TICK = "┃"

_FLASH_S = 1.5  # how long a just-refreshed card's border stays highlighted


def bar_cells(
    pct: float | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
) -> Text:
    """Just the bar glyphs: severity-colored fill, track, optional tick."""
    text = Text()
    if pct is None:
        text.append(_BAR_EMPTY * width, style=TRACK)
        return text
    frac = min(max(pct, 0.0), 100.0) / 100.0
    cells = frac * width
    full = int(cells)
    half = (cells - full) >= 0.5 and full < width
    tick_at: int | None = None
    if threshold is not None:
        tick_at = min(width - 1, max(0, round(threshold / 100.0 * width)))
    color = severity_color(pct)
    fill_style = f"{color} dim" if stale else color
    for i in range(width):
        if tick_at is not None and i == tick_at:
            text.append(_BAR_TICK, style=SEV_WARN)
        elif i < full:
            text.append(_BAR_FILLED, style=fill_style)
        elif i == full and half:
            text.append(_BAR_HALF, style=fill_style)
        else:
            text.append(_BAR_EMPTY, style=TRACK)
    return text


_GRAD_STOPS = ((0.0, SEV_OK), (0.5, SEV_WARN), (1.0, SEV_CRIT))


def _hex_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def gradient_color(t: float) -> str:
    """Colour for a bar cell at height fraction ``t`` (0 bottom .. 1 top):
    interpolates SEV_OK → SEV_WARN → SEV_CRIT."""
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    for (t0, c0), (t1, c1) in zip(_GRAD_STOPS, _GRAD_STOPS[1:]):
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            r0, g0, b0 = _hex_rgb(c0)
            r1, g1, b1 = _hex_rgb(c1)
            return "#%02x%02x%02x" % (
                round(r0 + (r1 - r0) * f),
                round(g0 + (g1 - g0) * f),
                round(b0 + (b1 - b0) * f),
            )
    return SEV_CRIT


_V_EIGHTHS = " ▁▂▃▄▅▆▇█"  # index 0..8, empty → full


def bar_v(pct: float, height: int) -> list[str]:
    """A vertical bar ``height`` cells tall, glyphs top-row-first, filled from
    the bottom via eighth blocks."""
    eighths = max(0.0, min(100.0, pct)) / 100.0 * height * 8
    rows = []
    for r in range(height - 1, -1, -1):  # r = whole cells below this one
        cell = max(0, min(8, round(eighths - r * 8)))
        rows.append(_V_EIGHTHS[cell])
    return rows


def usage_bar(
    label: str,
    pct: float | None,
    suffix: str | None,
    width: int,
    *,
    stale: bool = False,
    threshold: float | None = None,
) -> Text:
    """One full bar line: ``5h ━━━━╸────┃──  47%  resets 2h 13m · 20:39``."""
    text = Text()
    text.append(f"{label} ", style=MUTED)
    text.append(bar_cells(pct, width, stale=stale, threshold=threshold))
    if pct is None:
        text.append("  usage unknown", style=MUTED)
    else:
        color = severity_color(pct)
        text.append(f" {pct:3.0f}%", style=f"{color} dim" if stale else color)
    if suffix:
        text.append(f"  {suffix}", style=MUTED)
    return text


def _reset_parts(window: dict, now: float) -> tuple[str | None, str | None]:
    """Countdown suffix and its clock-extended variant for one window.

    ``("resets 2h 13m", "resets 2h 13m · 20:39")`` — the second form is what
    a row shows when it has the width for it. Equal when no clock is known.
    """
    reset = data.reset_text(window, now)
    if not reset:
        return None, None
    clock = data.reset_clock(window, now)
    return reset, f"{reset} · {clock}" if clock else reset


def _pace_suffix(window: dict, fetched_at: float | None) -> str:
    """"(ahead of pace)" when a weekly window is meaningfully ahead, else ""."""
    result = pace.compute_pace(window, fetched_at=fetched_at)
    return "(ahead of pace)" if result and result.ahead else ""


def usage_rows(
    last_good: dict | None, now: float, fetched_at: float | None = None
) -> list[tuple[str, float, str, str]]:
    """(label, pct, suffix, suffix_full) rows mirroring the CLI's
    ``_format_usage_lines``.

    ``suffix_full`` extends the reset countdown with the absolute clock time
    (``resets 2h 13m · 20:39``) for rows that have room; otherwise it equals
    ``suffix``. Only windows the account actually has produce a row — an
    annual plan without a 7-day window simply has no 7d line. Order matches
    the CLI: spend, 5h, 7d, then per-model scoped windows (e.g. "Fable"),
    the latter marked ``(!)`` at/over their limit. The weekly (7d) and scoped
    rows also carry a "(ahead of pace)" marker when meaningfully ahead of the
    week's expected usage (issue #125) — never the 5h row.
    """
    if not isinstance(last_good, dict):
        return []
    rows: list[tuple[str, float, str, str]] = []
    spend = last_good.get("spend")
    if spend:
        amounts = f"${spend['used']:,.2f} / ${spend['limit']:,.2f}"
        reset, reset_full = _reset_parts(spend, now)
        suffix = f"{reset}  {amounts}" if reset else amounts
        suffix_full = f"{reset_full}  {amounts}" if reset_full else amounts
        rows.append(("$$", float(spend["pct"]), suffix, suffix_full))
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key)
        if window:
            reset, reset_full = _reset_parts(window, now)
            suffix, suffix_full = reset or "", reset_full or ""
            if key == "seven_day":
                marker = _pace_suffix(window, fetched_at)
                if marker:
                    suffix = f"{suffix}  {marker}" if suffix else marker
                    suffix_full = f"{suffix_full}  {marker}" if suffix_full else marker
            rows.append((label, float(window["pct"]), suffix, suffix_full))
    for window in last_good.get("scoped") or []:
        pct = float(window["pct"])
        suffix, suffix_full = _reset_parts(window, now)
        suffix, suffix_full = suffix or "", suffix_full or ""
        if pct >= 100:
            suffix = f"{suffix}  (!)" if suffix else "(!)"
            suffix_full = f"{suffix_full}  (!)" if suffix_full else "(!)"
        else:
            marker = _pace_suffix(window, fetched_at)
            if marker:
                suffix = f"{suffix}  {marker}" if suffix else marker
                suffix_full = f"{suffix_full}  {marker}" if suffix_full else marker
        rows.append((window["name"], pct, suffix, suffix_full))
    return rows


def _short_reset(window: dict, now: float) -> str | None:
    """Largest-unit reset, e.g. 'resets 2h 13m' -> '2h'."""
    full = data.reset_text(window, now)
    if not full:
        return None
    body = full.replace("resets ", "").strip()
    return body.split()[0] if body else None


def meter_windows(
    last_good: dict | None, now: float
) -> list[tuple[str, float, str | None, bool]]:
    """(label, pct, short_reset, maxed) per window — spend, 5h, 7d, scoped."""
    out: list[tuple[str, float, str | None, bool]] = []
    if not isinstance(last_good, dict):
        return out
    spend = last_good.get("spend")
    if spend:
        pct = float(spend["pct"])
        out.append(("$$", pct, _short_reset(spend, now), pct >= 100))
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = last_good.get(key)
        if w:
            pct = float(w["pct"])
            out.append((label, pct, _short_reset(w, now), pct >= 100))
    for w in last_good.get("scoped") or []:
        pct = float(w["pct"])
        out.append((w["name"], pct, _short_reset(w, now), pct >= 100))
    return out


def meter_grid_dims(
    width: int,
    height: int,
    n_accounts: int,
    *,
    min_card_w: int = 20,
    bar_min: int = 3,
    card_chrome: int = 6,
) -> tuple[int, int, int]:
    """(ncols, card_width, bar_height) for the meter grid at this terminal size."""
    n = max(1, n_accounts)
    ncols = max(1, min((width + 1) // min_card_w, n))
    card_width = (width - (ncols - 1)) // ncols
    rows_of_cards = -(-n // ncols)  # ceil
    bar_height = max(bar_min, height // rows_of_cards - card_chrome)
    return ncols, card_width, bar_height


def _cell_widths(interior_width: int, n_windows: int) -> list[int]:
    """Split ``interior_width`` into ``n_windows`` cells, remainder spread
    across the leftmost cells so the widths sum to ``interior_width``."""
    base, remainder = divmod(interior_width, n_windows)
    return [base + (1 if i < remainder else 0) for i in range(n_windows)]


def _meter_bar_width(cell_width: int) -> int:
    return max(1, min(cell_width - 2, 5))


def _fit_center(s: str, width: int) -> str:
    """Center ``s`` in ``width`` columns, truncating first so the result is
    always exactly ``width`` wide even when ``s`` is longer than the cell."""
    return s[:width].center(width)


def _meter_header(acc: AccountSnapshot, card_width: int) -> Text:
    """``╭─┤ {number} {name} {● if active}├────╮``.

    Degrades under a tight ``card_width``: the name is hard-truncated first,
    then dropped along with the active dot, and if even the bare number
    can't be framed, this falls back to a plain ``╭──…──╮`` border. Always
    exactly ``card_width`` columns.
    """
    number = str(acc.number)
    name = acc.email.split("@", 1)[0]
    active_suffix = " ●" if acc.is_active else ""
    prefix = "╭─┤ "

    # Minimal frame: prefix + number + "├" + "╮", no name, no active dot,
    # no fill. If even this doesn't fit, there's no room for a framed title.
    base_len = len(prefix) + len(number) + 1 + 1
    if base_len > card_width:
        return Text("╭" + "─" * max(0, card_width - 2) + "╮", style=MUTED)

    available = card_width - base_len
    active_part = active_suffix if len(active_suffix) <= available else ""
    name_budget = max(0, available - len(active_part) - (1 if name else 0))
    name = name[:name_budget]

    text = Text()
    text.append(prefix, style=MUTED)
    text.append(number, style=ACCENT)
    if name:
        text.append(" ", style=MUTED)
        text.append(name, style=FOREGROUND)
    if active_part:
        text.append(" ", style=MUTED)
        text.append("●", style=ACCENT)
    text.append("├", style=MUTED)
    fill_len = max(0, card_width - len(text.plain) - 1)
    text.append("─" * fill_len, style=MUTED)
    text.append("╮", style=MUTED)

    # Final safety net: guarantee exact width regardless of the arithmetic
    # above, since callers rely on every meter_card line being card_width.
    plain_len = len(text.plain)
    if plain_len < card_width:
        text.append("─" * (card_width - plain_len), style=MUTED)
    elif plain_len > card_width:
        text = text[:card_width]
    return text


def meter_card(
    acc: AccountSnapshot,
    card_width: int,
    bar_height: int,
    *,
    now: float,
    flash: bool = False,
) -> Text:
    """A framed vertical-meter card: header, one bar column per window, and
    baseline/label/percent/reset rows beneath. Always ``bar_height + 6``
    lines of exactly ``card_width`` columns — used by the watch screen's
    meter grid. ``flash`` highlights the top border to signal a just-refreshed
    measurement."""
    interior_width = card_width - 2
    bottom_border = "╰" + "─" * (card_width - 2) + "╯"
    text = Text()
    header = _meter_header(acc, card_width)
    if flash:
        header = Text(header.plain, style=f"bold {ACCENT}")
    text.append(header)

    if acc.usage.sentinel is not None:
        windows = []
    else:
        windows = meter_windows(acc.usage.last_good, now)
    if not windows:
        n_blank = bar_height + 4
        label = (
            data.sentinel_label(acc.usage.sentinel)
            if acc.usage.sentinel is not None
            else "usage unavailable"
        )
        text_line = _fit_center(label, interior_width)
        text_row = n_blank // 2
        for i in range(n_blank):
            text.append("\n")
            text.append("│", style=MUTED)
            if i == text_row:
                text.append(text_line, style=MUTED)
            else:
                text.append(" " * interior_width)
            text.append("│", style=MUTED)
        text.append("\n")
        text.append(bottom_border, style=MUTED)
        return text

    widths = _cell_widths(interior_width, len(windows))
    # Each window's full bar column + its bar glyph width, computed once.
    bars = [
        (bar_v(pct, bar_height), _meter_bar_width(w))
        for w, (_label, pct, _reset, _maxed) in zip(widths, windows)
    ]

    for r in range(bar_height):
        frac = (bar_height - 1 - r) / (bar_height - 1) if bar_height > 1 else 0.0
        color = gradient_color(frac)
        text.append("\n")
        text.append("│", style=MUTED)
        for w, (glyphs, bar_w) in zip(widths, bars):
            glyph = glyphs[r]
            run = _fit_center(glyph * bar_w, w)
            if glyph == " ":
                text.append(run)
            else:
                text.append(run, style=color)
        text.append("│", style=MUTED)

    text.append("\n")
    text.append("│", style=MUTED)
    for w, (_glyphs, bar_w) in zip(widths, bars):
        text.append(_fit_center("─" * bar_w, w), style=TRACK)
    text.append("│", style=MUTED)

    text.append("\n")
    text.append("│", style=MUTED)
    for w, (label, _pct, _reset, _maxed) in zip(widths, windows):
        text.append(_fit_center(label, w), style=MUTED)
    text.append("│", style=MUTED)

    text.append("\n")
    text.append("│", style=MUTED)
    for w, (_label, pct, _reset, _maxed) in zip(widths, windows):
        text.append(_fit_center(f"{round(pct)}%", w), style=severity_color(pct))
    text.append("│", style=MUTED)

    text.append("\n")
    text.append("│", style=MUTED)
    for w, (_label, _pct, reset, maxed) in zip(widths, windows):
        text.append(_fit_center(reset or "", w), style=SEV_CRIT if maxed else MUTED)
    text.append("│", style=MUTED)

    text.append("\n")
    text.append(bottom_border, style=MUTED)
    return text


def grid_move(cursor: int, dx: int, dy: int, ncols: int, n: int) -> int:
    """New row-major index after moving ``(dx, dy)`` across an ``ncols``-wide
    grid of ``n`` items, clamped to the grid's bounds — never wraps, and a
    short last row clamps to its own last column rather than the grid's."""
    ncols = max(1, ncols)
    n = max(0, n)
    if n == 0:
        return cursor
    cursor = max(0, min(n - 1, cursor))
    row, col = divmod(cursor, ncols)
    nrows = -(-n // ncols)
    new_row = max(0, min(nrows - 1, row + dy))
    row_start = new_row * ncols
    row_len = min(ncols, n - row_start)
    new_col = max(0, min(row_len - 1, col + dx))
    return row_start + new_col


def _line_spans(card: Text) -> list[tuple[int, int]]:
    """``[(start, end), ...]`` character offsets of each line in ``card``,
    excluding the ``\\n`` separators — for slicing a line back out of the
    multi-line :class:`Text` while keeping its per-character styles."""
    spans = []
    start = 0
    for line in card.plain.split("\n"):
        end = start + len(line)
        spans.append((start, end))
        start = end + 1
    return spans


def _mark_cursor(card: Text, card_width: int) -> Text:
    """Recolor a card's left/right border edges to ACCENT to mark it selected."""
    marked = card.copy()
    offset = 0
    for line in card.plain.split("\n"):
        marked.stylize(ACCENT, offset, offset + 1)
        marked.stylize(ACCENT, offset + card_width - 1, offset + card_width)
        offset += len(line) + 1
    return marked


def meters_grid_text(
    accounts: list[AccountSnapshot],
    width: int,
    height: int,
    *,
    cursor: int | None = None,
    now: float,
    flashed: set[str] | None = None,
) -> Text:
    """The watch screen's tiled meter grid: every account as a
    :func:`meter_card`, ``ncols`` per row with a one-space gutter, rows
    separated by a blank line. ``cursor`` marks that account's card as
    selected; ``flashed`` account numbers get their card's top border
    highlighted.
    """
    if not accounts:
        return Text("no accounts", style=MUTED)

    flashed = flashed or set()
    ncols, card_width, bar_height = meter_grid_dims(width, height, len(accounts))
    cards = [
        meter_card(acc, card_width, bar_height, now=now, flash=acc.number in flashed)
        for acc in accounts
    ]
    if cursor is not None and 0 <= cursor < len(cards):
        cards[cursor] = _mark_cursor(cards[cursor], card_width)

    text = Text()
    for row_start in range(0, len(cards), ncols):
        row_cards = cards[row_start : row_start + ncols]
        if row_start:
            text.append("\n\n")
        row_line_spans = [_line_spans(card) for card in row_cards]
        for line_idx in range(len(row_line_spans[0])):
            if line_idx:
                text.append("\n")
            for col_idx, card in enumerate(row_cards):
                if col_idx:
                    text.append(" ")
                start, end = row_line_spans[col_idx][line_idx]
                text.append(card[start:end])
    return text


def account_card_text(
    acc: AccountSnapshot,
    width: int,
    *,
    threshold: float | None = None,
    now: float | None = None,
) -> Text:
    """The full account card: header line + per-window bar rows."""
    now = now if now is not None else time.time()

    text = Text()
    text.append(f"{acc.number:>2}  ", style=f"bold {FOREGROUND}")
    if acc.alias:
        text.append(acc.alias, style=f"bold {ACCENT}")
        text.append(f" ({acc.email})", style=FOREGROUND)
    else:
        text.append(acc.email, style=FOREGROUND)
    text.append(f"  [{acc.display_tag}]", style=MUTED)
    if acc.is_active:
        text.append("   ● active", style=f"bold {ACCENT}")
    if acc.disabled:
        text.append("   (disabled)", style=MUTED)
    age = data.format_age(acc.usage.age_s)
    if age:
        text.append(f"   {age}", style=MUTED)

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        text.append("\n    ")
        style = MUTED if sentinel == USAGE_API_KEY else SEV_WARN
        marker = "·" if sentinel == USAGE_API_KEY else "⚠"
        text.append(f"{marker} {data.sentinel_label(sentinel)}", style=style)
        # Same supplementary line `cswap list` prints: the last good
        # measurement behind the sentinel (API-key accounts have no quota to
        # have "seen").
        if sentinel != USAGE_API_KEY:
            last_seen = data.last_seen_note(acc.usage)
            if last_seen is not None:
                text.append("\n    ")
                text.append(f"└ {last_seen}", style=MUTED)
        return text

    rows = usage_rows(acc.usage.last_good, now, acc.usage.fetched_at)
    if not rows:
        text.append("\n    ")
        text.append("usage unavailable", style=MUTED)
        if acc.usage.last_error:
            text.append(f" · {acc.usage.last_error}", style=MUTED)
        return text

    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    label_width = max(len(label) for label, _pct, _suffix, _full in rows)
    bar_width = max(12, min(30, width - 42 - label_width))
    # everything on a row except the suffix: indent, label, bar, " NNN%", gap
    row_overhead = 4 + label_width + 1 + bar_width + 5 + 2
    for label, pct, suffix, suffix_full in rows:
        # per-row: show the absolute clock only where it fits, so a long
        # spend row degrading doesn't cost the 5h/7d rows their clocks
        if suffix_full != suffix and row_overhead + len(suffix_full) <= width:
            suffix = suffix_full
        text.append("\n    ")
        text.append(
            usage_bar(
                f"{label:<{label_width}}",
                pct,
                suffix or None,
                bar_width,
                stale=stale,
                threshold=threshold,
            )
        )
    return text


def mini_account_text(acc: AccountSnapshot, now: float) -> Text:
    """One minimized line for an inactive account.

    ``2  work@acme.dev [personal]   5h 92% · 7d 63%`` — pcts only, severity
    colored; a window at/over 100% brings its reset countdown along, and a
    maxed per-model window shows as ``Fable (!)``. Sentinel states show
    their label instead.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{acc.number:>2}  ", style=f"bold {MUTED}")
    if acc.alias:
        text.append(acc.alias, style=f"bold {ACCENT}")
        text.append(f" ({acc.email})", style=FOREGROUND)
    else:
        text.append(acc.email, style=FOREGROUND)
    text.append(f"  [{acc.display_tag}]", style=MUTED)
    if acc.disabled:
        text.append("  (disabled)", style=MUTED)
    text.append("   ")

    sentinel = acc.usage.sentinel
    if sentinel is not None:
        style = MUTED if sentinel == USAGE_API_KEY else SEV_WARN
        text.append(data.sentinel_label(sentinel), style=style)
        return text

    last_good = acc.usage.last_good
    fetched_at = acc.usage.fetched_at
    stale = acc.usage.age_s is not None and acc.usage.age_s > STALE_OK_S
    parts = 0
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = last_good.get(key) if isinstance(last_good, dict) else None
        if not window:
            continue
        pct = float(window["pct"])
        if parts:
            text.append(" · ", style=TRACK)
        color = severity_color(pct)
        text.append(f"{label} ", style=MUTED)
        text.append(f"{pct:.0f}%", style=f"{color} dim" if stale else color)
        if pct >= 100:
            reset = data.reset_text(window, now)
            if reset:
                text.append(f" ({reset})", style=MUTED)
        elif key == "seven_day":
            result = pace.compute_pace(window, fetched_at=fetched_at)
            if result and result.ahead:
                text.append(" (ahead)", style=SEV_WARN)
        parts += 1
    maxed = [
        w["name"]
        for w in (last_good.get("scoped") or [] if isinstance(last_good, dict) else [])
        if float(w["pct"]) >= 100
    ]
    for name in maxed:
        if parts:
            text.append(" · ", style=TRACK)
        text.append(f"{name} (!)", style=SEV_CRIT)
        parts += 1
    if not parts:
        text.append("usage unknown", style=MUTED)
    return text


class AccountsPanel(Static):
    """Static account overview: the active account full-size, others as
    one-line minis (in slot order, expanded in place). The dashboard's — and
    with ``show_minis=False`` the auto screen's — always-visible monitor."""

    def __init__(self, *, show_minis: bool = True, id: str | None = None) -> None:
        super().__init__(id=id)
        self._show_minis = show_minis

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", lambda _snap: self.refresh(layout=True))

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        snap = app.snapshot
        if snap is None:
            return Text("loading…", style=MUTED)
        if not snap.accounts:
            return Text(
                "No managed accounts yet.\n"
                "Use the menu below: Add account — from your current "
                "Claude Code login, or from a setup-token / API key.",
                style=MUTED,
            )
        now = time.time()
        width = (self.size.width or 80) - 2
        blocks: list[Text] = []
        for acc in snap.accounts:
            if acc.is_active:
                blocks.append(
                    account_card_text(
                        acc, width, threshold=app.threshold_pct, now=now
                    )
                )
            elif self._show_minis:
                blocks.append(mini_account_text(acc, now))
        if not blocks:
            return Text("no active managed login", style=MUTED)
        text = Text()
        previous_multiline = False
        for i, block in enumerate(blocks):
            multiline = "\n" in block.plain
            if i:
                # breathe around the expanded active card
                text.append("\n\n" if (multiline or previous_multiline) else "\n")
            text.append(block)
            previous_multiline = multiline
        return text


class MetersGrid(Static):
    """The watch screen's tiled vertical-meter grid, with a keyboard-navigable
    cursor over the accounts."""

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.cursor: int | None = None
        self._stamps: dict[str, float | None] = {}
        self._flash: set[str] = set()

    def on_mount(self) -> None:
        self.watch(self.app, "snapshot", self._on_snapshot)

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is not None:
            self._flash_updated(snap)
        self.refresh(layout=True)

    def _flash_updated(self, snap: AccountsSnapshot) -> None:
        """Briefly highlight cards whose stored measurement just advanced."""
        new_stamps = {acc.number: acc.usage.fetched_at for acc in snap.accounts}
        if self._stamps:
            changed = {
                num
                for num, ts in new_stamps.items()
                if ts is not None and ts != self._stamps.get(num)
            } - self._flash
            if changed:
                self._flash |= changed
                self.set_timer(_FLASH_S, partial(self._clear_flash, changed))
        self._stamps = new_stamps

    def _clear_flash(self, numbers: set[str]) -> None:
        self._flash -= numbers
        self.refresh(layout=True)

    def render(self) -> Text:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        snap = app.snapshot
        if snap is None:
            return Text("loading…", style=MUTED)
        if not snap.accounts:
            return Text("No managed accounts yet.", style=MUTED)
        return meters_grid_text(
            snap.accounts,
            self.size.width - 2,
            self.size.height,
            cursor=self.cursor,
            now=time.time(),
            flashed=self._flash,
        )

    def _ncols(self) -> int:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        snap = app.snapshot
        n = len(snap.accounts) if snap else 0
        if n == 0:
            return 1
        ncols, _card_width, _bar_height = meter_grid_dims(
            self.size.width - 2, self.size.height, n
        )
        return ncols

    def move_cursor(self, dx: int, dy: int) -> None:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        snap = app.snapshot
        n = len(snap.accounts) if snap else 0
        if n == 0:
            return
        self.cursor = grid_move(self.cursor or 0, dx, dy, self._ncols(), n)
        self.refresh(layout=True)

    def selected_number(self) -> str | None:
        app: "CswapApp" = self.app  # type: ignore[assignment]
        snap = app.snapshot
        if snap is None or self.cursor is None:
            return None
        if not 0 <= self.cursor < len(snap.accounts):
            return None
        return snap.accounts[self.cursor].number


class AccountCard(Static):
    """One account rendered full-size (used by the switch screen's list)."""

    def __init__(self, acc: AccountSnapshot, *, threshold: float | None = None) -> None:
        super().__init__()
        self._acc = acc
        self._threshold = threshold

    def set_account(self, acc: AccountSnapshot) -> None:
        self._acc = acc
        self.refresh(layout=True)

    def render(self) -> Text:
        return account_card_text(
            self._acc, self.size.width or 80, threshold=self._threshold
        )


class AccountItem(ListItem):
    """ListView row wrapping an :class:`AccountCard`; remembers its slot."""

    def __init__(self, acc: AccountSnapshot) -> None:
        super().__init__(AccountCard(acc))
        self.number = acc.number
        self.email = acc.email

    def set_account(self, acc: AccountSnapshot) -> None:
        self.number = acc.number
        self.email = acc.email
        self.query_one(AccountCard).set_account(acc)


class MenuItem(ListItem):
    """One menu row: a label plus an action id the screen dispatches on."""

    def __init__(self, label: str, action_id: str, *, muted: bool = False) -> None:
        style = MUTED if muted else FOREGROUND
        super().__init__(Static(Text(label, style=style)))
        self.action_id = action_id
