"""
Option Safety Check Service

Before a trader buys or sells an option, answer: "if my directional view is
right, does this option still pay?" -- accounting for IV crush after an event,
theta while waiting, and how much movement the premium already prices in.

Works for every F&O segment OpenAlgo supports (NFO, BFO, MCX, CDS, BCD, NCDEX,
crypto), resolving the underlying the same way the IV Chart and Vol Surface
tools do. All the number crunching lives in option_safety_math.py.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from database.token_db_enhanced import fno_search_symbols
from services.history_service import get_history
from services.option_greeks_service import calculate_greeks
from services.option_safety_math import (
    MIN_T,
    breakeven_move,
    build_verdict,
    event_from_term_structure,
    implied_move_pct,
    is_material_event,
    iv_axis,
    move_axis,
    move_frequency,
    percentile_of,
    position_pnl,
    priced_move_over_hold,
    realized_vol,
    rolling_realized_vol,
    scenario_grid,
    synthetic_forward,
)
from services.option_symbol_service import (
    NO_SPOT_EXCHANGES,
    construct_crypto_option_symbol,
    construct_option_symbol,
    find_atm_strike_from_actual,
    find_option_in_database,
    get_available_strikes,
    get_option_exchange,
    resolve_underlying_quote,
)
from services.quotes_service import get_multiquotes, get_quotes
from utils.constants import CRYPTO_EXCHANGES, INSTRUMENT_PERPFUT
from utils.logging import get_logger

logger = get_logger(__name__)

NSE_INDEX_SYMBOLS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "NIFTYIT",
    "NIFTYPHARMA",
    "NIFTYBANK",
}
BSE_INDEX_SYMBOLS = {"SENSEX", "BANKEX", "SENSEX50"}

# A kink smaller than this (IV points, near minus next) is ordinary noise.
EVENT_KINK_POINTS = 3.0


def _err(message: str, code: int) -> tuple[bool, dict[str, Any], int]:
    return False, {"status": "error", "message": message}, code


def _quote_exchange(base_symbol: str, exchange: str) -> str:
    if base_symbol in NSE_INDEX_SYMBOLS:
        return "NSE_INDEX"
    if base_symbol in BSE_INDEX_SYMBOLS:
        return "BSE_INDEX"
    ex = exchange.upper()
    if ex == "NFO":
        return "NSE"
    if ex == "BFO":
        return "BSE"
    return ex


def _resolve_underlying(base_symbol: str, exchange: str) -> tuple[str, str] | str:
    """(quote_symbol, quote_exchange) or an error message."""
    ex = exchange.upper()
    quote_exchange = _quote_exchange(base_symbol, exchange)
    if ex in CRYPTO_EXCHANGES:
        perp = fno_search_symbols(
            query=f"{base_symbol}USDFUT",
            exchange=exchange,
            instrumenttype=INSTRUMENT_PERPFUT,
            limit=1,
        )
        if not perp:
            return f"No perpetual futures found for {base_symbol} on {exchange}"
        return perp[0]["symbol"], quote_exchange
    if ex in NO_SPOT_EXCHANGES:
        resolved = resolve_underlying_quote(base_symbol, ex)
        if resolved is None:
            return f"No unexpired futures found for {base_symbol} on {ex}"
        return resolved
    return base_symbol, quote_exchange


def _quote_map(symbols: list[str], exchange: str, api_key: str) -> dict[str, dict[str, float]]:
    """symbol -> {ltp, bid, ask, volume} for every symbol that has a last price."""
    ok, resp, _ = get_multiquotes(
        symbols=[{"symbol": s, "exchange": exchange} for s in symbols], api_key=api_key
    )
    out: dict[str, dict[str, float]] = {}
    if ok and "results" in resp:
        for r in resp["results"]:
            sym = r.get("symbol")
            data = r.get("data", r) or {}
            if sym and data.get("ltp"):
                out[sym] = {k: float(data.get(k) or 0) for k in ("ltp", "bid", "ask", "volume")}
    return out


def _is_stale(q: dict[str, float] | None) -> bool:
    """No trade today and no live two-sided quote: the last price may be days old."""
    if not q:
        return True
    return q.get("volume", 0) <= 0 and not (q.get("bid", 0) > 0 and q.get("ask", 0) > 0)


def _spread_pct(q: dict[str, float] | None) -> float | None:
    if not q:
        return None
    bid, ask = q.get("bid", 0), q.get("ask", 0)
    if bid > 0 and ask >= bid:
        return 100.0 * (ask - bid) / ((ask + bid) / 2)
    return None


def _greeks(symbol: str, exchange: str, spot: float, price: float) -> dict[str, Any] | None:
    if not price or price <= 0:
        return None
    try:
        ok, resp, _ = calculate_greeks(
            option_symbol=symbol, exchange=exchange, spot_price=spot, option_price=price
        )
    except Exception:
        logger.exception(f"Greeks failed for {symbol}")
        return None
    if ok and resp.get("status") == "success" and resp.get("implied_volatility"):
        return resp
    return None


def _daily_closes(symbol: str, exchange: str, api_key: str) -> list[float]:
    end = datetime.now()
    start = end - timedelta(days=420)
    try:
        ok, resp, _ = get_history(
            symbol=symbol,
            exchange=exchange,
            interval="D",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
            api_key=api_key,
        )
    except Exception:
        logger.exception(f"History failed for {symbol}")
        return []
    if not ok:
        return []
    rows = resp.get("data") or []
    closes = []
    for row in rows:
        c = row.get("close") if isinstance(row, dict) else None
        if c:
            closes.append(float(c))
    return closes


def get_option_safety_check(
    *,
    underlying: str,
    exchange: str,
    expiry_date: str,
    option_type: str,
    side: str,
    target_move_pct: float,
    holding_days: float,
    api_key: str,
    strike: float | None = None,
    next_expiry_date: str | None = None,
    event_expected: bool = False,
    iv_after_override: float | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """
    Args:
        underlying: Base symbol, e.g. NIFTY, RELIANCE, CRUDEOIL, USDINR, BTC
        exchange: Options exchange from the tools selector (NFO, BFO, MCX, CDS, ...)
        expiry_date: DDMMMYY, the expiry you intend to trade
        option_type: CE or PE
        side: BUY or SELL
        target_move_pct: Signed underlying move you expect (+2.0 == up 2 %)
        holding_days: Calendar days you expect to hold
        strike: Strike to check; ATM when omitted
        next_expiry_date: DDMMMYY of the following expiry, for the term-structure event test
        event_expected: Trader knows of an event (results, policy, ...) before expiry
        iv_after_override: Trader's own estimate of IV (percent) after the event
    """
    option_type = option_type.upper()
    side = side.upper()
    if option_type not in ("CE", "PE"):
        return _err("option_type must be CE or PE", 400)
    if side not in ("BUY", "SELL"):
        return _err("side must be BUY or SELL", 400)
    holding_days = max(0.0, float(holding_days))

    base = underlying.upper()
    expiry = expiry_date.upper()
    resolved = _resolve_underlying(base, exchange)
    if isinstance(resolved, str):
        return _err(resolved, 404)
    quote_symbol, quote_exchange = resolved
    options_exchange = get_option_exchange(_quote_exchange(base, exchange))
    build = (
        construct_crypto_option_symbol
        if exchange.upper() in CRYPTO_EXCHANGES
        else construct_option_symbol
    )

    ok, q, code = get_quotes(symbol=quote_symbol, exchange=quote_exchange, api_key=api_key)
    if not ok:
        return _err(f"Failed to fetch {base} quote: {q.get('message', '')}", code)
    spot = (q.get("data") or {}).get("ltp")
    if not spot:
        return _err(f"No LTP for {base}", 502)
    spot = float(spot)

    strikes = get_available_strikes(base, expiry, "CE", options_exchange)
    if not strikes:
        return _err(f"No strikes found for {base} {expiry} on {options_exchange}", 404)
    atm = find_atm_strike_from_actual(spot, strikes)
    if strike is None:
        strike = atm
    elif strike not in strikes:
        strike = min(strikes, key=lambda s: abs(s - strike))

    opt_sym = build(base, expiry, strike, option_type)
    atm_ce, atm_pe = build(base, expiry, atm, "CE"), build(base, expiry, atm, "PE")
    syms = {opt_sym, atm_ce, atm_pe}

    next_ce = next_pe = None
    natm = None
    if next_expiry_date:
        nexp = next_expiry_date.upper()
        nstrikes = get_available_strikes(base, nexp, "CE", options_exchange)
        if nstrikes:
            natm = find_atm_strike_from_actual(spot, nstrikes)
            next_ce, next_pe = build(base, nexp, natm, "CE"), build(base, nexp, natm, "PE")
            syms.update({next_ce, next_pe})

    quotes = _quote_map(sorted(syms), options_exchange, api_key)
    ltps = {s: q["ltp"] for s, q in quotes.items()}
    if opt_sym not in ltps:
        return _err(
            f"No live price for {opt_sym}. Is the market open and does this contract trade?", 404
        )

    # Each expiry is priced off its own put-call-parity forward, as the Option
    # Chain does. Index futures carry a premium over spot, and on MCX and the
    # currency segments every expiry sits on a different futures contract, so
    # one shared reference price would bias the IVs and fake a term-structure kink.
    fwd = synthetic_forward(atm, ltps.get(atm_ce), ltps.get(atm_pe)) or spot

    g = _greeks(opt_sym, options_exchange, fwd, ltps[opt_sym])
    if not g:
        return _err(
            f"Could not work out the implied volatility of {opt_sym} at {ltps[opt_sym]}. "
            "The price may be stale or the strike too deep in the money.",
            422,
        )

    quote_stale = _is_stale(quotes.get(opt_sym))
    spread_pct = _spread_pct(quotes.get(opt_sym))
    entry = float(ltps[opt_sym])
    iv_now = float(g["implied_volatility"])
    r_pct = float(g.get("interest_rate") or 0)
    dte = float(g.get("days_to_expiry") or 0)
    t_now = max(dte / 365.0, MIN_T)
    greeks = g.get("greeks", {})
    vega = float(greeks.get("vega") or 0)
    theta = float(greeks.get("theta") or 0)

    # ATM IV of this expiry (average CE/PE) and the next one -> term structure
    g_ce = _greeks(atm_ce, options_exchange, fwd, ltps.get(atm_ce, 0))
    g_pe = _greeks(atm_pe, options_exchange, fwd, ltps.get(atm_pe, 0))
    atm_ivs = [x["implied_volatility"] for x in (g_ce, g_pe) if x]
    atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else iv_now
    straddle = (ltps.get(atm_ce) or 0) + (ltps.get(atm_pe) or 0)
    implied_move = (
        implied_move_pct(straddle, fwd) if ltps.get(atm_ce) and ltps.get(atm_pe) else None
    )

    # Next expiry ATM IV, only from live (traded or quoted) legs on its own forward
    next_iv = next_dte = None
    term_note = None
    if next_ce and next_pe:
        if _is_stale(quotes.get(next_ce)) or _is_stale(quotes.get(next_pe)):
            term_note = "next expiry is not trading, so the term structure cannot be read"
        else:
            nfwd = synthetic_forward(natm, ltps.get(next_ce), ltps.get(next_pe))
            if nfwd:
                n_ivs = [
                    x
                    for x in (
                        _greeks(next_ce, options_exchange, nfwd, ltps.get(next_ce, 0)),
                        _greeks(next_pe, options_exchange, nfwd, ltps.get(next_pe, 0)),
                    )
                    if x
                ]
                if n_ivs:
                    next_iv = sum(float(x["implied_volatility"]) for x in n_ivs) / len(n_ivs)
                    next_dte = float(n_ivs[0].get("days_to_expiry") or 0)

    term_gap = (atm_iv - next_iv) if next_iv is not None else None
    event_est = None
    if next_iv is not None and next_dte and term_gap is not None and term_gap >= EVENT_KINK_POINTS:
        event_est = event_from_term_structure(atm_iv, t_now, next_iv, next_dte / 365.0)
        if not is_material_event(event_est):
            event_est = None
    event_detected = event_est is not None
    priced_hold = priced_move_over_hold(
        implied_move, dte, holding_days, event_est, bool(event_expected)
    )

    # Realised volatility from daily history. A series of identical closes
    # (an untraded contract) gives zero and is treated as no data.
    periods = 365 if exchange.upper() in CRYPTO_EXCHANGES else 252
    closes = _daily_closes(quote_symbol, quote_exchange, api_key)
    hv20 = realized_vol(closes, 20, periods) or None
    hv60 = realized_vol(closes, 60, periods) or None
    hv_series = [x for x in rolling_realized_vol(closes, 20, periods)[-periods:] if x > 0]
    iv_pct_rank = percentile_of(atm_iv, hv_series) if len(hv_series) >= 30 else None

    # Expected IV after the holding period / event
    if iv_after_override is not None and iv_after_override > 0:
        iv_after, iv_after_source = float(iv_after_override), "your estimate"
    elif event_est is not None:
        # Scale the strike's own IV by the ATM crush ratio to keep the smile shape
        iv_after = iv_now * event_est.base_iv / atm_iv if atm_iv else event_est.base_iv
        iv_after_source = "term structure (event variance removed)"
    elif event_expected:
        candidates = [x for x in (next_iv, hv20 * 1.1 if hv20 else None) if x]
        iv_after = min([iv_now] + candidates) if candidates else iv_now * 0.8
        iv_after_source = "next expiry IV / realised vol" if candidates else "assumed 20% drop"
    else:
        iv_after, iv_after_source = iv_now, "no event -- IV held flat"

    t_after = max(t_now - holding_days / 365.0, MIN_T)
    target = float(target_move_pct) / 100.0
    pnl_target = position_pnl(
        side, option_type, entry, fwd * (1 + target), strike, t_after, r_pct, iv_after
    )
    pnl_target_nc = position_pnl(
        side, option_type, entry, fwd * (1 + target), strike, t_after, r_pct, iv_now
    )
    be = breakeven_move(side, option_type, entry, fwd, strike, t_after, r_pct, iv_after)
    be_nc = breakeven_move(side, option_type, entry, fwd, strike, t_after, r_pct, iv_now)
    trading_days = (
        max(1, round(holding_days * 5 / 7)) if periods == 252 else max(1, round(holding_days))
    )
    hit_rate = move_frequency(closes, trading_days, be) if be not in (None, 0.0) else None

    verdict = build_verdict(
        side=side,
        flag=option_type,
        entry_price=entry,
        iv_now=iv_now,
        iv_after=iv_after,
        hv20=hv20,
        iv_vs_hv_percentile=iv_pct_rank,
        term_gap=term_gap,
        event_detected=event_detected,
        event_flagged=bool(event_expected),
        implied_move=implied_move,
        target_move_pct=float(target_move_pct),
        pnl_at_target=pnl_target,
        pnl_at_target_no_crush=pnl_target_nc,
        breakeven_pct=None if be is None else be * 100,
        theta_per_day=theta,
        vega=vega,
        holding_days=holding_days,
        hit_rate_pct=hit_rate,
        priced_move_hold=priced_hold,
        quote_stale=quote_stale,
        spread_pct=spread_pct,
    )

    span = max(
        abs(target_move_pct) * 1.5,
        (implied_move or 0) * 1.2,
        (abs(be) * 100 * 1.3) if be else 0,
        2.0,
    )
    moves = move_axis(span)
    ivs = iv_axis(iv_now, iv_after)
    grid = scenario_grid(side, option_type, entry, fwd, strike, t_after, r_pct, moves, ivs)

    # Decay path at the target move: P&L each day until expiry (or holding end + a bit)
    path_days = max(1, min(int(dte) if dte >= 1 else 1, 60))
    decay = []
    for d in range(0, path_days + 1):
        t_d = max(t_now - d / 365.0, MIN_T)
        decay.append(
            {
                "day": d,
                "pnl_flat_iv": round(
                    position_pnl(
                        side, option_type, entry, fwd * (1 + target), strike, t_d, r_pct, iv_now
                    ),
                    4,
                ),
                "pnl_crushed_iv": round(
                    position_pnl(
                        side, option_type, entry, fwd * (1 + target), strike, t_d, r_pct, iv_after
                    ),
                    4,
                ),
            }
        )

    lot = None
    info = find_option_in_database(opt_sym, options_exchange)
    if info and info.get("lotsize"):
        try:
            lot = int(float(info["lotsize"]))
        except (TypeError, ValueError):
            lot = None

    data = {
        "underlying": base,
        "underlying_symbol": quote_symbol,
        "underlying_ltp": round(spot, 4),
        "forward": round(fwd, 4),
        "option_symbol": opt_sym,
        "exchange": options_exchange,
        "option_type": option_type,
        "side": side,
        "strike": strike,
        "atm_strike": atm,
        "expiry_date": expiry,
        "days_to_expiry": round(dte, 2),
        "lot_size": lot,
        "entry_price": round(entry, 4),
        "interest_rate": r_pct,
        "greeks": {
            "delta": greeks.get("delta"),
            "gamma": greeks.get("gamma"),
            "theta": theta,
            "vega": vega,
        },
        "volatility": {
            "iv": round(iv_now, 2),
            "atm_iv": round(atm_iv, 2),
            "next_expiry_iv": None if next_iv is None else round(next_iv, 2),
            "next_expiry_date": next_expiry_date.upper() if next_expiry_date else None,
            "term_gap": None if term_gap is None else round(term_gap, 2),
            "term_note": term_note,
            "hv20": None if hv20 is None else round(hv20, 2),
            "hv60": None if hv60 is None else round(hv60, 2),
            "iv_percentile": None if iv_pct_rank is None else round(iv_pct_rank, 1),
            "history_days": len(closes),
            "hv20_series": [round(x, 2) for x in hv_series],
        },
        "event": {
            "detected": event_detected,
            "flagged": bool(event_expected),
            "base_iv": None if event_est is None else round(event_est.base_iv, 2),
            "event_move_pct": None if event_est is None else round(event_est.event_move_pct, 2),
        },
        "implied_move_pct": None if implied_move is None else round(implied_move, 2),
        "priced_move_hold_pct": None if priced_hold is None else round(priced_hold, 2),
        "liquidity": {
            "stale": quote_stale,
            "spread_pct": None if spread_pct is None else round(spread_pct, 2),
            "volume": (quotes.get(opt_sym) or {}).get("volume"),
        },
        "straddle_price": round(straddle, 4) if straddle else None,
        "scenario": {
            "target_move_pct": float(target_move_pct),
            "holding_days": holding_days,
            "iv_after": round(iv_after, 2),
            "iv_after_source": iv_after_source,
            "pnl_at_target": round(pnl_target, 4),
            "pnl_at_target_pct": round(100 * pnl_target / entry, 2) if entry else None,
            "pnl_at_target_no_crush": round(pnl_target_nc, 4),
            "pnl_at_target_no_crush_pct": round(100 * pnl_target_nc / entry, 2) if entry else None,
            "breakeven_move_pct": None if be is None else round(be * 100, 3),
            "breakeven_move_no_crush_pct": None if be_nc is None else round(be_nc * 100, 3),
            "historical_hit_rate_pct": None if hit_rate is None else round(hit_rate, 1),
        },
        "heatmap": {
            "moves_pct": [round(m * 100, 3) for m in moves],
            "ivs": ivs,
            "pnl": grid,
        },
        "decay_path": decay,
        "verdict": asdict(verdict),
    }
    return True, {"status": "success", "data": data}, 200
