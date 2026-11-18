"""Tests for the Option Safety Check math (services/option_safety_math.py)."""

import math

import pytest

pytest.importorskip("opengreeks")

from services.option_safety_math import (  # noqa: E402
    RED,
    breakeven_move,
    build_verdict,
    event_from_term_structure,
    implied_move_pct,
    is_material_event,
    iv_axis,
    move_axis,
    move_frequency,
    option_price,
    percentile_of,
    position_pnl,
    priced_move_over_hold,
    realized_vol,
    rolling_realized_vol,
    scenario_grid,
    synthetic_forward,
)

DAY = 1 / 365


def test_iv_crush_wipes_out_correct_direction():
    """The textbook case: stock up 2 %, call still loses because IV fell 60 -> 35."""
    entry = option_price("CE", 1000, 1000, 14 * DAY, 0, 60)
    after = option_price("CE", 1020, 1000, 13 * DAY, 0, 35)
    assert entry == pytest.approx(46.85, abs=0.1)
    assert after < entry
    pnl = position_pnl("BUY", "CE", entry, 1020, 1000, 13 * DAY, 0, 35)
    assert pnl == pytest.approx(after - entry)
    assert pnl / entry == pytest.approx(-0.19, abs=0.02)


def test_breakeven_move_buyer_call_and_put():
    entry = option_price("CE", 1000, 1000, 14 * DAY, 0, 60)
    be = breakeven_move("BUY", "CE", entry, 1000, 1000, 13 * DAY, 0, 35)
    assert be == pytest.approx(0.0336, abs=0.001)
    # at the breakeven the position is flat
    assert position_pnl(
        "BUY", "CE", entry, 1000 * (1 + be), 1000, 13 * DAY, 0, 35
    ) == pytest.approx(0, abs=1e-3)

    pe_entry = option_price("PE", 1000, 1000, 14 * DAY, 0, 60)
    be_pe = breakeven_move("BUY", "PE", pe_entry, 1000, 1000, 13 * DAY, 0, 35)
    assert be_pe < 0


def test_breakeven_zero_when_already_profitable():
    entry = option_price("CE", 1000, 1000, 14 * DAY, 0, 30)
    # IV doubles: buyer is in profit without any move
    assert breakeven_move("BUY", "CE", entry, 1000, 1000, 13 * DAY, 0, 60) == 0.0


def test_breakeven_seller_is_adverse_move():
    entry = option_price("CE", 1000, 1000, 14 * DAY, 0, 60)
    be = breakeven_move("SELL", "CE", entry, 1000, 1000, 13 * DAY, 0, 35)
    assert be > 0
    assert position_pnl("SELL", "CE", entry, 1000 * (1 + be * 0.9), 1000, 13 * DAY, 0, 35) > 0
    assert position_pnl("SELL", "CE", entry, 1000 * (1 + be * 1.1), 1000, 13 * DAY, 0, 35) < 0


def test_event_from_term_structure():
    est = event_from_term_structure(60, 7 * DAY, 30, 35 * DAY)
    assert est is not None
    assert est.base_iv == pytest.approx(15.0, abs=0.01)
    # rebuild near variance from the pieces
    near_var = (est.base_iv / 100) ** 2 * 7 * DAY + (est.event_move_pct / 100) ** 2
    assert near_var == pytest.approx(0.60**2 * 7 * DAY, rel=1e-9)


def test_no_event_when_curve_is_normal():
    assert event_from_term_structure(20, 7 * DAY, 22, 35 * DAY) is None
    assert event_from_term_structure(20, 7 * DAY, 20, 35 * DAY) is None
    assert event_from_term_structure(20, 35 * DAY, 22, 7 * DAY) is None


def test_realized_vol_matches_constant_returns():
    # alternating +1 % / -1 % log returns -> stdev ~= 1 % daily
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * math.exp(0.01 if i % 2 == 0 else -0.01))
    hv = realized_vol(closes, 20)
    assert hv == pytest.approx(math.sqrt(252) * 1.0, rel=0.05)
    assert len(rolling_realized_vol(closes, 20)) == 60 - 20 + 1
    assert realized_vol(closes[:5], 20) is None


def test_percentile_and_move_frequency():
    assert percentile_of(5, [1, 2, 3, 4, 6, 7]) == pytest.approx(100 * 4 / 6)
    assert percentile_of(5, []) is None
    closes = [100, 102, 101, 104, 100]
    assert move_frequency(closes, 1, 0.015) == pytest.approx(50.0)  # +2 %, +2.97 % of 4 moves
    assert move_frequency(closes, 1, -0.03) == pytest.approx(25.0)  # -3.85 %


def test_implied_move_and_axes():
    assert implied_move_pct(94, 1000) == pytest.approx(9.4)
    assert implied_move_pct(0, 1000) is None
    moves = move_axis(10, 13)
    assert moves[0] == -0.1 and moves[-1] == 0.1 and 0.0 in moves and len(moves) == 13
    ivs = iv_axis(60, 35)
    assert 60 in ivs and 35 in ivs and ivs == sorted(ivs, reverse=True)


def test_scenario_grid_shape_and_sign():
    entry = option_price("CE", 1000, 1000, 14 * DAY, 0, 60)
    grid = scenario_grid("BUY", "CE", entry, 1000, 1000, 13 * DAY, 0, [-0.05, 0, 0.05], [60, 35])
    assert len(grid) == 2 and len(grid[0]) == 3
    assert grid[0][2] > grid[0][0]  # call gains as the underlying rises
    assert grid[1][1] < grid[0][1]  # lower IV row is worse for the buyer


def _verdict(**over):
    base = {
        "side": "BUY",
        "flag": "CE",
        "entry_price": 46.85,
        "iv_now": 60,
        "iv_after": 35,
        "hv20": 30,
        "iv_vs_hv_percentile": 95,
        "term_gap": 25,
        "event_detected": True,
        "event_flagged": False,
        "implied_move": 9.4,
        "target_move_pct": 2,
        "pnl_at_target": -9.0,
        "pnl_at_target_no_crush": 8.0,
        "breakeven_pct": 3.36,
        "theta_per_day": -1.7,
        "vega": 0.78,
        "holding_days": 1,
        "hit_rate_pct": 8,
    }
    base.update(over)
    return build_verdict(**base)


def test_verdict_red_for_pre_event_buyer():
    v = _verdict()
    assert v.level == RED
    keys = {s.key for s in v.signals}
    assert {"iv_hv", "event", "implied_move", "pnl_target", "theta"} <= keys
    assert any("debit spread" in s for s in v.suggestions)


def test_verdict_green_for_cheap_option_big_target():
    v = _verdict(
        iv_now=15,
        iv_after=15,
        hv20=16,
        iv_vs_hv_percentile=30,
        term_gap=-1,
        event_detected=False,
        implied_move=2.0,
        target_move_pct=4,
        pnl_at_target=20,
        pnl_at_target_no_crush=20,
        breakeven_pct=0.8,
        theta_per_day=-0.5,
        hit_rate_pct=45,
    )
    assert v.level == "green"
    assert v.suggestions == []


def test_seller_reads_rich_iv_as_favourable():
    v = _verdict(
        side="SELL", pnl_at_target=15, pnl_at_target_no_crush=-2, target_move_pct=1, hit_rate_pct=5
    )
    by_key = {s.key: s.level for s in v.signals}
    assert by_key["iv_hv"] == "green"
    assert by_key["implied_move"] == "green"
    assert by_key["event"] == "green"


# ---------------------------------------------------------------------------
# Service end-to-end with the broker mocked
# ---------------------------------------------------------------------------


def _expiry_code(days_ahead):
    from datetime import datetime, timedelta

    return (datetime.now() + timedelta(days=days_ahead)).strftime("%d%b%y").upper()


def test_service_end_to_end_detects_event_and_crush(monkeypatch):
    import services.option_safety_service as svc

    near, nxt = _expiry_code(7), _expiry_code(35)
    spot = 1000.0
    strikes = [900.0 + 20 * i for i in range(11)]  # 900..1100

    def price(sym):
        exp = near if near in sym else nxt
        days = 7 if exp == near else 35
        iv = 60 if exp == near else 30
        k = float(sym[len("NIFTY") + 7 : -2])
        return option_price(sym[-2:], spot, k, days / 365 + 15.5 / 24 / 365, 0, iv)

    monkeypatch.setattr(svc, "_resolve_underlying", lambda b, e: ("NIFTY", "NSE_INDEX"))
    monkeypatch.setattr(svc, "get_quotes", lambda **k: (True, {"data": {"ltp": spot}}, 200))
    monkeypatch.setattr(svc, "get_available_strikes", lambda *a, **k: strikes)
    monkeypatch.setattr(
        svc,
        "_quote_map",
        lambda syms, ex, key: {
            s: {"ltp": round(price(s), 2), "bid": 0.0, "ask": 0.0, "volume": 1000.0} for s in syms
        },
    )
    closes = [1000 * math.exp(0.012 * ((-1) ** i)) for i in range(300)]
    monkeypatch.setattr(svc, "_daily_closes", lambda *a: closes)
    monkeypatch.setattr(svc, "find_option_in_database", lambda *a: {"lotsize": 75})

    ok, resp, code = svc.get_option_safety_check(
        underlying="NIFTY",
        exchange="NFO",
        expiry_date=near,
        next_expiry_date=nxt,
        option_type="CE",
        side="BUY",
        target_move_pct=2,
        holding_days=1,
        api_key="x",
    )
    assert ok, resp
    d = resp["data"]
    assert d["option_symbol"] == f"NIFTY{near}1000CE"
    assert d["lot_size"] == 75
    assert d["event"]["detected"] is True
    assert d["volatility"]["iv"] == pytest.approx(60, abs=5)  # mock t is approximate
    assert d["scenario"]["iv_after"] < 30  # crushed toward the ~15 % base vol
    assert d["scenario"]["pnl_at_target"] < 0 < d["scenario"]["pnl_at_target_no_crush"]
    assert d["verdict"]["level"] == "red"
    assert len(d["heatmap"]["pnl"]) == len(d["heatmap"]["ivs"])
    assert len(d["heatmap"]["pnl"][0]) == len(d["heatmap"]["moves_pct"])


def test_priced_move_scales_to_holding_period():
    # no event: sqrt-time share of the expiry move
    assert priced_move_over_hold(2.0, 4, 1) == pytest.approx(1.0)
    assert priced_move_over_hold(2.0, 4, 10) == pytest.approx(2.0)  # capped at expiry
    # flagged event with no estimate: whole expiry move (conservative)
    assert priced_move_over_hold(2.0, 4, 1, event_flagged=True) == 2.0
    # estimated event: the jump lands inside the hold
    est = event_from_term_structure(60, 7 * DAY, 30, 35 * DAY)
    hold = priced_move_over_hold(9.0, 7, 1, est)
    assert est.event_move_pct < hold <= 9.0


def test_synthetic_forward():
    assert synthetic_forward(1000, 30, 20) == 1010
    assert synthetic_forward(1000, 0, 20) is None


def test_stale_quote_forces_red_and_says_why():
    v = _verdict(
        quote_stale=True,
        iv_now=15,
        iv_after=15,
        hv20=16,
        iv_vs_hv_percentile=30,
        event_detected=False,
        pnl_at_target=20,
        pnl_at_target_no_crush=20,
    )
    assert v.level == RED
    assert "stale" in v.headline
    assert v.signals[0].key == "liquidity"


def test_target_compared_with_holding_period_move():
    # 1-day target of 1 % against 1.34 % by expiry but 0.6 % over the day: not red
    v = _verdict(
        iv_now=15,
        iv_after=15,
        hv20=14,
        iv_vs_hv_percentile=40,
        event_detected=False,
        implied_move=1.34,
        priced_move_hold=0.6,
        target_move_pct=1.0,
        pnl_at_target=14,
        pnl_at_target_no_crush=14,
        breakeven_pct=0.14,
        theta_per_day=-0.5,
        hit_rate_pct=40,
    )
    by_key = {s.key: s for s in v.signals}
    assert by_key["implied_move"].level == "green"
    assert "over your 1 day(s)" in by_key["implied_move"].detail


def test_expiry_day_kink_is_not_an_event():
    # live case: 1-day expiry 18.2 % vs 14.5 % next week -> a jump smaller than a normal day
    small = event_from_term_structure(18.23, 1.07 * DAY, 14.47, 8.07 * DAY)
    assert small is not None and not is_material_event(small)
    # results-style kink: clearly an event
    assert is_material_event(event_from_term_structure(60, 7 * DAY, 30, 35 * DAY))
    assert not is_material_event(None)
