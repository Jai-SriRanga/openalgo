"""
Option Safety Check -- pure math.

Answers the question a trader should ask before opening an option position:
"If the underlying moves the way my chart says, will this option actually make
money once implied volatility and time decay have had their say?"

Everything here is broker-free and deterministic so it can be unit tested.
The service layer (option_safety_service.py) fetches quotes and history and
feeds the numbers in.

Conventions
-----------
* Volatility in and out of this module is in PERCENT (25.0 == 25 %), matching
  calculate_greeks(). It is converted to a decimal only at the pricing call.
* Time is in years (days / 365), matching option_greeks_service.
* Moves are signed fractions of the underlying (0.02 == +2 %).
* Pricing is Black-76 via opengreeks, the same model the rest of OpenAlgo uses,
  so the numbers here agree with the Option Chain and Strategy Builder.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

MIN_T = 1.0 / (365.0 * 24.0)  # one hour: floor so pricing never sees t <= 0
MIN_IV = 0.5  # percent: floor so a deep crush scenario never prices at sigma 0


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def option_price(
    flag: str, forward: float, strike: float, t: float, r_pct: float, iv_pct: float
) -> float:
    """Black-76 price. flag is 'CE'/'PE' (or 'c'/'p')."""
    f = "c" if flag.upper().startswith("C") else "p"
    if t <= 0:
        return max(0.0, forward - strike) if f == "c" else max(0.0, strike - forward)
    from opengreeks import black76

    sigma = max(iv_pct, MIN_IV) / 100.0
    return float(black76.black(f, forward, strike, max(t, MIN_T), r_pct / 100.0, sigma))


def position_pnl(
    side: str,
    flag: str,
    entry_price: float,
    forward: float,
    strike: float,
    t: float,
    r_pct: float,
    iv_pct: float,
) -> float:
    """P&L per unit of underlying for a long (BUY) or short (SELL) option."""
    value = option_price(flag, forward, strike, t, r_pct, iv_pct)
    pnl = value - entry_price
    return pnl if side.upper() == "BUY" else -pnl


# ---------------------------------------------------------------------------
# Volatility statistics
# ---------------------------------------------------------------------------


def log_returns(closes: Sequence[float]) -> list[float]:
    out = []
    for prev, cur in zip(closes, closes[1:], strict=False):
        if prev and cur and prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def realized_vol(closes: Sequence[float], window: int, periods_per_year: int = 252) -> float | None:
    """Annualised close-to-close volatility (percent) over the last `window` returns."""
    rets = log_returns(closes)[-window:]
    if len(rets) < max(5, window // 2):
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    return math.sqrt(var * periods_per_year) * 100.0


def rolling_realized_vol(
    closes: Sequence[float], window: int, periods_per_year: int = 252
) -> list[float]:
    """Realised vol (percent) for every window ending on each day -- a one-year 'normal range'."""
    rets = log_returns(closes)
    out = []
    for end in range(window, len(rets) + 1):
        chunk = rets[end - window : end]
        mean = sum(chunk) / window
        var = sum((x - mean) ** 2 for x in chunk) / (window - 1)
        out.append(math.sqrt(var * periods_per_year) * 100.0)
    return out


def percentile_of(value: float, series: Sequence[float]) -> float | None:
    """Share of `series` (0-100) that is below `value`."""
    if not series:
        return None
    below = sum(1 for x in series if x < value)
    return 100.0 * below / len(series)


def move_frequency(closes: Sequence[float], horizon_days: int, threshold: float) -> float | None:
    """
    How often (percent of windows) the underlying moved at least `threshold`
    over `horizon_days` trading days. Signed: a positive threshold counts up
    moves, a negative one counts down moves.
    """
    h = max(1, int(round(horizon_days)))
    if len(closes) <= h:
        return None
    hits = total = 0
    for i in range(len(closes) - h):
        a, b = closes[i], closes[i + h]
        if not a or a <= 0 or not b:
            continue
        move = b / a - 1.0
        total += 1
        if (threshold >= 0 and move >= threshold) or (threshold < 0 and move <= threshold):
            hits += 1
    return 100.0 * hits / total if total else None


# ---------------------------------------------------------------------------
# Event / term structure
# ---------------------------------------------------------------------------


@dataclass
class EventEstimate:
    base_iv: float  # percent: IV the near expiry should fall to once the event is gone
    event_move_pct: float  # percent: one-day move the event alone is priced for


def event_from_term_structure(
    iv_near: float, t_near: float, iv_next: float, t_next: float
) -> EventEstimate | None:
    """
    Split near-expiry variance into 'normal' variance and a one-off event jump.

    Model: total variance = base_vol^2 * t + event_variance, with the same base
    vol and the same event in both expiries (the event happens before the near
    expiry). Two equations, two unknowns:

        iv_near^2 * t_near = base^2 * t_near + E
        iv_next^2 * t_next = base^2 * t_next + E

    Returns None when the curve is not inverted enough to imply an event
    (no kink -> nothing to crush), or the inputs are unusable.
    """
    if not (iv_near and iv_next and t_near > 0 and t_next > t_near):
        return None
    s1, s2 = iv_near / 100.0, iv_next / 100.0
    base_var = (s2 * s2 * t_next - s1 * s1 * t_near) / (t_next - t_near)
    if base_var <= 0:
        return None
    event_var = (s1 * s1 - base_var) * t_near
    if event_var <= 0:
        return None
    return EventEstimate(
        base_iv=math.sqrt(base_var) * 100.0, event_move_pct=math.sqrt(event_var) * 100.0
    )


def is_material_event(est: EventEstimate | None, ratio: float = 1.5) -> bool:
    """
    True when the event jump is clearly bigger than an ordinary day's move.

    Very short-dated options often trade a few IV points above the next expiry
    for no event at all (expiry-day pricing). That still solves to a small
    "event", so only a jump of at least `ratio` ordinary daily moves counts.
    """
    if est is None:
        return False
    daily = est.base_iv / math.sqrt(365.0)
    return est.event_move_pct >= ratio * daily


def priced_move_over_hold(
    implied_move: float | None,
    dte: float,
    holding_days: float,
    event: EventEstimate | None = None,
    event_flagged: bool = False,
) -> float | None:
    """
    The part of the expiry's priced move that falls inside the holding period.

    With no event, variance accrues evenly in time, so the move scales with the
    square root of the share of time held. With an estimated event inside the
    holding period, the whole event jump lands in it plus the ordinary
    variance for the days held. A flagged event without an estimate cannot be
    split out, so the full expiry move is used (the conservative choice).
    """
    if not implied_move or dte <= 0:
        return implied_move
    h = min(max(holding_days, 1.0), dte)
    if event is not None:
        var = (event.base_iv / 100.0) ** 2 * h / 365.0 + (event.event_move_pct / 100.0) ** 2
        return min(implied_move, math.sqrt(var) * 100.0)
    if event_flagged:
        return implied_move
    return implied_move * math.sqrt(h / dte)


def synthetic_forward(strike: float, call: float | None, put: float | None) -> float | None:
    """Put-call parity forward F = K + C - P, as the Option Chain tool uses (rate term ignored)."""
    if call and put and call > 0 and put > 0:
        return strike + call - put
    return None


def implied_move_pct(straddle_price: float, forward: float) -> float | None:
    """ATM straddle as a percent of the underlying: the move the market has paid for by expiry."""
    if not straddle_price or not forward or forward <= 0:
        return None
    return 100.0 * straddle_price / forward


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def breakeven_move(
    side: str,
    flag: str,
    entry_price: float,
    forward: float,
    strike: float,
    t_after: float,
    r_pct: float,
    iv_after: float,
    limit: float = 0.5,
) -> float | None:
    """
    Signed underlying move at which P&L crosses zero, searched in the option's
    favourable direction for a buyer (up for CE, down for PE) and its adverse
    direction for a seller (the same direction).

    Buyer: the move needed just to get the premium back.
    Seller: the move the position can absorb before it starts losing.

    0.0 means the position is already profitable with no move (a buyer whose
    IV went up, or a seller after the crush). None means not reached within
    +/- `limit`.
    """
    direction = 1.0 if flag.upper().startswith("C") else -1.0

    def pnl(m: float) -> float:
        return position_pnl(
            side, flag, entry_price, forward * (1 + m), strike, t_after, r_pct, iv_after
        )

    p0 = pnl(0.0)
    if side.upper() == "BUY":
        if p0 >= 0:
            return 0.0
        hi = direction * limit
        if pnl(hi) < 0:
            return None
        lo = 0.0
    else:
        if p0 < 0:
            return 0.0
        hi = direction * limit
        if pnl(hi) >= 0:
            return None
        lo = 0.0
    for _ in range(60):
        mid = (lo + hi) / 2
        better = pnl(mid) >= 0
        # buyer: P&L rises along `direction`; seller: P&L falls along it
        if (side.upper() == "BUY") == better:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def move_axis(span_pct: float, points: int = 13) -> list[float]:
    """Symmetric list of signed moves (fractions) from -span to +span."""
    span = max(span_pct, 1.0) / 100.0
    half = points // 2
    return [round(span * i / half, 6) for i in range(-half, half + 1)]


def iv_axis(iv_now: float, iv_after: float | None) -> list[float]:
    """IV levels (percent) for the heatmap rows: a little higher than now down past the expected crush."""
    floor = min(iv_after if iv_after else iv_now, iv_now) * 0.7
    top = iv_now + 5.0
    levels = []
    lvl = top
    step = max(2.0, round((top - floor) / 8.0))
    while lvl > max(floor, MIN_IV * 2) and len(levels) < 12:
        levels.append(round(lvl, 2))
        lvl -= step
    if iv_now not in levels:
        levels.append(round(iv_now, 2))
    if iv_after and round(iv_after, 2) not in levels:
        levels.append(round(iv_after, 2))
    return sorted(set(levels), reverse=True)


def scenario_grid(
    side: str,
    flag: str,
    entry_price: float,
    forward: float,
    strike: float,
    t_after: float,
    r_pct: float,
    moves: Sequence[float],
    ivs: Sequence[float],
) -> list[list[float]]:
    """P&L (per unit) for every (IV level row, underlying move column)."""
    return [
        [
            round(
                position_pnl(
                    side, flag, entry_price, forward * (1 + m), strike, t_after, r_pct, iv
                ),
                4,
            )
            for m in moves
        ]
        for iv in ivs
    ]


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

GREEN, AMBER, RED = "green", "amber", "red"


@dataclass
class Signal:
    key: str
    level: str
    title: str
    detail: str
    critical: bool = False


@dataclass
class Verdict:
    level: str
    headline: str
    signals: list[Signal] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)


def _fmt(x: float | None, nd: int = 1, suffix: str = "") -> str:
    return "n/a" if x is None else f"{x:.{nd}f}{suffix}"


def build_verdict(
    *,
    side: str,
    flag: str,
    entry_price: float,
    iv_now: float,
    iv_after: float,
    hv20: float | None,
    iv_vs_hv_percentile: float | None,
    term_gap: float | None,
    event_detected: bool,
    event_flagged: bool,
    implied_move: float | None,
    target_move_pct: float,
    pnl_at_target: float,
    pnl_at_target_no_crush: float,
    breakeven_pct: float | None,
    theta_per_day: float,
    vega: float,
    holding_days: float,
    hit_rate_pct: float | None,
    priced_move_hold: float | None = None,
    quote_stale: bool = False,
    spread_pct: float | None = None,
    decay_cost_hold: float | None = None,
    held_to_expiry: bool = False,
    history_horizon_days: int | None = None,
) -> Verdict:
    """Turn the numbers into plain-language traffic lights.

    `implied_move` is the move the ATM straddle prices by expiry;
    `priced_move_hold` is the part of it that falls inside the holding period
    (event included), which is what a target over that period competes with.
    `decay_cost_hold` is what time decay alone takes from the option (per unit,
    model price) over the holding period if nothing moves; `history_horizon_days`
    is the span the history check actually measured, in trading days.
    """
    buyer = side.upper() == "BUY"
    signals: list[Signal] = []
    crush = iv_now - iv_after
    event = event_detected or event_flagged

    # 0. Can the price be trusted at all?
    if quote_stale:
        signals.append(
            Signal(
                "liquidity",
                RED,
                "Price may be stale",
                "This contract has not traded today and has no live bid or ask, so its last price may be days old. "
                "The IV and every number below are computed from that price and cannot be trusted.",
                critical=True,
            )
        )
    elif spread_pct is not None:
        lvl = RED if spread_pct >= 10 else AMBER if spread_pct >= 4 else GREEN
        signals.append(
            Signal(
                "liquidity",
                lvl,
                "Liquidity (bid-ask spread)",
                f"The bid-ask spread is {spread_pct:.1f}% of the price. "
                + (
                    "Getting in and out costs that much before the market moves at all."
                    if lvl != GREEN
                    else "Tight enough to enter and exit near the quoted price."
                ),
            )
        )

    # 1. Is the option expensive against how much the underlying really moves?
    if hv20:
        ratio = iv_now / hv20
        if buyer:
            lvl = RED if ratio >= 1.5 else AMBER if ratio >= 1.2 else GREEN
            msg = f"IV {iv_now:.1f}% vs realised {hv20:.1f}% (x{ratio:.2f}). " + (
                "You are paying for much more movement than the underlying usually makes."
                if lvl != GREEN
                else "Premium is in line with how the underlying actually moves."
            )
        else:
            lvl = GREEN if ratio >= 1.2 else AMBER if ratio >= 0.9 else RED
            msg = f"IV {iv_now:.1f}% vs realised {hv20:.1f}% (x{ratio:.2f}). " + (
                "Premium is rich -- the seller's edge."
                if lvl == GREEN
                else "You are selling cheap volatility: little cushion if it moves."
            )
        signals.append(Signal("iv_hv", lvl, "IV vs realised volatility", msg))

    # 2. Is today's IV high for this underlying?
    if iv_vs_hv_percentile is not None:
        p = iv_vs_hv_percentile
        if buyer:
            lvl = RED if p >= 85 else AMBER if p >= 65 else GREEN
        else:
            lvl = GREEN if p >= 65 else AMBER if p >= 35 else RED
        signals.append(
            Signal(
                "iv_rank",
                lvl,
                "IV percentile (1-year)",
                f"Current IV is above {p:.0f}% of the underlying's 20-day realised volatility readings over the "
                "last year. High readings tend to fall back.",
            )
        )

    # 3. Is an event priced into this expiry?
    if event:
        why = []
        if event_detected and term_gap is not None:
            why.append(f"near expiry IV is {term_gap:.1f} pts above the next expiry (event kink)")
        if event_flagged:
            why.append("you marked an event before expiry")
        lvl = RED if buyer and crush >= 8 else AMBER if buyer else GREEN
        signals.append(
            Signal(
                "event",
                lvl,
                "Event / IV crush risk",
                f"{'; '.join(why).capitalize()}. Expected IV after the event: {iv_after:.1f}% "
                f"(a {crush:.1f} pt drop, costing about {abs(vega) * crush:.2f} per unit through Vega).",
            )
        )
    else:
        signals.append(
            Signal(
                "event",
                GREEN,
                "Event / IV crush risk",
                "No event kink in the term structure and none flagged. IV crush risk is low "
                "(check the results calendar yourself if this is a stock).",
            )
        )

    # 4. Does your target beat what the market already prices?
    priced = priced_move_hold or implied_move
    if priced:
        tgt = abs(target_move_pct)
        span = (
            f"+/-{priced:.2f}% over your {holding_days:g} day(s) (+/-{implied_move:.2f}% by expiry)"
            if priced_move_hold and implied_move and abs(priced_move_hold - implied_move) > 0.01
            else f"+/-{priced:.2f}% by expiry"
        )
        if buyer:
            lvl = RED if tgt < 0.8 * priced else AMBER if tgt < 1.1 * priced else GREEN
            msg = f"Option prices imply a move of {span}; your target is {tgt:.2f}%. " + (
                "A buyer only wins when the move is bigger than what is already priced."
                if lvl != GREEN
                else "Your target is larger than the priced move."
            )
        else:
            lvl = GREEN if tgt < 0.8 * priced else AMBER if tgt < 1.1 * priced else RED
            msg = f"Option prices imply a move of {span}; your expected move is {tgt:.2f}%. " + (
                "The market is paying you for more movement than you expect."
                if lvl == GREEN
                else "You expect a move as big as (or bigger than) the premium covers."
            )
        signals.append(Signal("implied_move", lvl, "Your target vs priced-in move", msg))

    # 5. The bottom line: P&L at your target after the crush and decay.
    pct = 100.0 * pnl_at_target / entry_price if entry_price else 0.0
    pct_nc = 100.0 * pnl_at_target_no_crush / entry_price if entry_price else 0.0
    be_txt = "not reachable within +/-50%" if breakeven_pct is None else f"{breakeven_pct:+.2f}%"
    if pnl_at_target < 0:
        lvl = RED
    elif pct < 15:
        lvl = AMBER
    else:
        lvl = GREEN
    pnl_level = lvl
    crush_note = (
        f" Without the IV drop it would be {pct_nc:+.1f}% -- the difference is the crush."
        if abs(pct - pct_nc) >= 1
        else ""
    )
    if buyer:
        detail = (
            f"If the underlying moves {target_move_pct:+.2f}% in {holding_days:g} day(s), this option returns "
            f"{pct:+.1f}% ({pnl_at_target:+.2f} per unit).{crush_note} Move needed just to break even: {be_txt}."
        )
    else:
        detail = (
            f"If the underlying moves {target_move_pct:+.2f}% in {holding_days:g} day(s), this short option returns "
            f"{pct:+.1f}% of premium ({pnl_at_target:+.2f} per unit).{crush_note} Adverse move it can absorb before "
            f"losing: {be_txt}."
        )
    signals.append(Signal("pnl_target", lvl, "P&L at your target", detail, critical=True))

    # 6. How often has the underlying actually made the breakeven move?
    if hit_rate_pct is not None and breakeven_pct is not None:
        span = history_horizon_days or max(1, round(holding_days))
        within = f"within {span} trading day{'s' if span != 1 else ''}"
        if holding_days < span:
            within += (
                " (daily closes can only measure whole days, so a move needed within hours "
                "happens less often than this)"
                if buyer
                else " (daily closes can only measure whole days, so over a shorter hold this "
                "move happens less often)"
            )
        if buyer:
            lvl = RED if hit_rate_pct < 15 else AMBER if hit_rate_pct < 30 else GREEN
            detail = (
                f"Over the last year the underlying moved {breakeven_pct:+.2f}% or more {within} on "
                f"{hit_rate_pct:.0f}% of occasions."
            )
        else:
            lvl = GREEN if hit_rate_pct < 10 else AMBER if hit_rate_pct < 25 else RED
            detail = (
                f"Over the last year the underlying moved {breakeven_pct:+.2f}% or more against this position "
                f"{within} on {hit_rate_pct:.0f}% of occasions."
            )
        signals.append(Signal("history", lvl, "How often has that move happened?", detail))

    # 7. Time decay, over the time you actually hold.
    hold_decay_pct = None
    if entry_price > 0:
        tpct = 100.0 * abs(theta_per_day) / entry_price
        if decay_cost_hold is not None:
            cost = max(0.0, decay_cost_hold)
        else:
            cost = abs(theta_per_day) * max(holding_days, 0.0)
        hold_decay_pct = 100.0 * cost / entry_price
        span = "into expiry" if held_to_expiry else f"over your {holding_days:g} day(s)"
        full_day = f" Theta for a full day is {abs(theta_per_day):.2f} ({tpct:.1f}%)."
        if buyer:
            lvl = RED if hold_decay_pct >= 25 else AMBER if hold_decay_pct >= 10 else GREEN
            detail = (
                f"If the underlying does not move, time decay takes {cost:.2f} per unit {span} "
                f"({hold_decay_pct:.1f}% of the premium).{full_day}"
            )
            if held_to_expiry:
                detail += " Held into expiry, all the time value left in the premium is gone."
        else:
            lvl = GREEN
            detail = (
                f"If the underlying does not move, time decay earns {cost:.2f} per unit {span} "
                f"({hold_decay_pct:.1f}% of the premium).{full_day}"
            )
        signals.append(Signal("theta", lvl, "Time decay (Theta)", detail))

    # Overall
    reds = [s for s in signals if s.level == RED]
    ambers = [s for s in signals if s.level == AMBER]
    if any(s.critical for s in reds):
        level = RED
    elif len(reds) >= 2 and pnl_level != GREEN:
        level = RED
    elif reds or len(ambers) >= 2:
        # A position that pays well at the target is never "avoid" on the
        # strength of side warnings alone: those are about how the move has to
        # come (soon, large enough), not about the view being wrong.
        level = AMBER
    else:
        level = GREEN

    action = "Buying" if buyer else "Selling"
    headline = {
        GREEN: f"{action} this option looks reasonable for your target.",
        AMBER: f"{action} this option can work for your target, but check the warnings below first.",
        RED: (
            f"{action} this option is likely to disappoint even if your direction is right."
            if buyer
            else "Selling this option carries more risk than the premium pays for."
        ),
    }[level]
    if level == AMBER and pnl_level == GREEN and reds:
        headline = (
            f"{action} this option pays well if your move comes, but the red flags below can still cost you "
            "if it comes late or smaller."
        )
    if quote_stale:
        headline = "This option's price looks stale, so the check cannot be trusted. Pick a traded strike or expiry."

    suggestions: list[str] = []
    if buyer and level != GREEN:
        if event:
            suggestions.append(
                "Wait until after the event and buy once IV has dropped, or trade the underlying/futures instead."
            )
            suggestions.append(
                "Use a debit spread (buy this strike, sell a further OTM one): the short leg loses IV too, so most "
                "of the crush cancels out."
            )
            suggestions.append(
                "A later expiry is hurt less: the event is a smaller part of its total volatility."
            )
        if hv20 and iv_now / hv20 >= 1.2:
            suggestions.append(
                "Options are expensive versus realised movement -- consider a spread instead of a naked buy."
            )
        if hold_decay_pct is not None and hold_decay_pct >= 10:
            suggestions.append(
                "Time decay is heavy over your holding period, so the move has to come soon. A later expiry "
                "or a closer-to-the-money strike decays more slowly."
            )
        if breakeven_pct is not None and abs(target_move_pct) < abs(breakeven_pct):
            suggestions.append(
                f"Your target ({target_move_pct:+.2f}%) is smaller than the break-even move ({breakeven_pct:+.2f}%)."
            )
    if not buyer and level != GREEN:
        suggestions.append(
            "Define the risk: sell a spread (add a further OTM long) instead of a naked short."
        )
        if priced and abs(target_move_pct) >= priced:
            suggestions.append(
                "Your expected move exceeds what the premium pays for -- this is a buyer's setup."
            )

    return Verdict(level=level, headline=headline, signals=signals, suggestions=suggestions)
