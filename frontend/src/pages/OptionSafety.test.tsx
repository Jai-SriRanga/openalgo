/**
 * What this pins: the page sends the trade the trader set up (including the
 * next expiry, which the backend needs to detect an event kink) and renders
 * the verdict, the IV-crush numbers and all three charts from the response.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SafetyCheckData } from '@/api/option-safety'
import OptionSafety from './OptionSafety'

const mocks = vi.hoisted(() => ({
  check: vi.fn(),
  plots: [] as unknown[],
}))

vi.mock('@/lib/Plot2D', () => ({
  default: (props: { data: unknown }) => {
    mocks.plots.push(props.data)
    return <div data-testid="plot" />
  },
}))

vi.mock('@/hooks/useSupportedExchanges', () => {
  // Stable identity, as the real hook memoises it: a fresh object per render
  // would re-run the page's exchange effect forever.
  const value = {
    toolsFnoExchanges: [{ value: 'NFO', label: 'NFO' }],
    defaultToolsFnoExchange: 'NFO',
    defaultUnderlyings: { NFO: ['NIFTY'] },
  }
  return { useSupportedExchanges: () => value }
})

vi.mock('@/api/iv-smile', () => ({
  ivSmileApi: {
    getUnderlyings: vi.fn().mockResolvedValue({ status: 'success', underlyings: ['NIFTY'] }),
    getExpiries: vi
      .fn()
      .mockResolvedValue({ status: 'success', expiries: ['25-NOV-26', '30-DEC-26'] }),
  },
}))

vi.mock('@/api/option-safety', () => ({
  optionSafetyApi: { check: mocks.check },
}))

const data: SafetyCheckData = {
  underlying: 'NIFTY',
  underlying_symbol: 'NIFTY',
  underlying_ltp: 1000,
  forward: 1001,
  option_symbol: 'NIFTY25NOV261000CE',
  exchange: 'NFO',
  option_type: 'CE',
  side: 'BUY',
  strike: 1000,
  atm_strike: 1000,
  expiry_date: '25NOV26',
  days_to_expiry: 7,
  lot_size: 75,
  entry_price: 46.85,
  interest_rate: 0,
  greeks: { delta: 0.52, gamma: 0.001, theta: -1.7, vega: 0.78 },
  volatility: {
    iv: 60,
    atm_iv: 60,
    next_expiry_iv: 30,
    next_expiry_date: '30DEC26',
    term_gap: 30,
    term_note: null,
    hv20: 30,
    hv60: 28,
    iv_percentile: 95,
    history_days: 300,
    hv20_series: [25, 28, 30],
  },
  event: { detected: true, flagged: false, base_iv: 15, event_move_pct: 8 },
  implied_move_pct: 9.4,
  priced_move_hold_pct: 8.5,
  liquidity: { stale: false, spread_pct: 0.5, volume: 1000 },
  straddle_price: 94,
  scenario: {
    target_move_pct: 2,
    holding_days: 1,
    iv_after: 35,
    iv_after_source: 'term structure (event variance removed)',
    pnl_at_target: -9,
    pnl_at_target_pct: -19.2,
    pnl_at_target_no_crush: 8,
    pnl_at_target_no_crush_pct: 17.1,
    breakeven_move_pct: 3.36,
    breakeven_move_no_crush_pct: 0.4,
    historical_hit_rate_pct: 8,
    history_horizon_days: 1,
    decay_cost_hold: 1.7,
    decay_cost_hold_pct: 3.6,
    held_to_expiry: false,
  },
  heatmap: {
    moves_pct: [-2, 0, 2],
    ivs: [60, 35],
    pnl: [
      [-20, -2, 8],
      [-30, -15, -9],
    ],
  },
  decay_path: [
    { day: 0, pnl_flat_iv: 10, pnl_crushed_iv: -8 },
    { day: 1, pnl_flat_iv: 8, pnl_crushed_iv: -9 },
  ],
  verdict: {
    level: 'red',
    headline: 'Buying this option is likely to disappoint even if your direction is right.',
    signals: [
      {
        key: 'pnl_target',
        level: 'red',
        title: 'P&L at your target',
        detail: 'detail text',
        critical: true,
      },
    ],
    suggestions: ['Use a debit spread'],
  },
}

describe('OptionSafety', () => {
  beforeEach(() => {
    mocks.check.mockReset()
    mocks.plots.length = 0
  })

  it('sends the trade with the next expiry and renders the verdict and charts', async () => {
    mocks.check.mockResolvedValue({ status: 'success', data })
    render(<OptionSafety />)

    const run = await screen.findByRole('button', { name: 'Run check' })
    await waitFor(() => expect(run).not.toBeDisabled())
    fireEvent.click(run)

    await screen.findByText('AVOID')
    expect(mocks.check).toHaveBeenCalledWith(
      expect.objectContaining({
        underlying: 'NIFTY',
        exchange: 'NFO',
        expiry_date: '25NOV26',
        next_expiry_date: '30DEC26',
        option_type: 'CE',
        side: 'BUY',
        target_move_pct: 2,
        holding_days: 1,
        strike: null,
      })
    )
    expect(screen.getByText(data.verdict.headline)).toBeInTheDocument()
    expect(screen.getByText('-19.2%')).toBeInTheDocument()
    expect(screen.getByText('Use a debit spread')).toBeInTheDocument()
    // decay path and IV history are charts; the P&L map is a table
    expect(screen.getAllByTestId('plot')).toHaveLength(2)
    expect(screen.getByText('after event')).toBeInTheDocument()
    // cells are % of premium: -30 per unit on a 46.85 premium
    expect(screen.getByText('-64%')).toBeInTheDocument()
  })
})
