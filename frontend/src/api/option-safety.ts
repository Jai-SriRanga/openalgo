import { webClient } from './client'

export type SignalLevel = 'green' | 'amber' | 'red'

export interface SafetySignal {
  key: string
  level: SignalLevel
  title: string
  detail: string
  critical: boolean
}

export interface SafetyCheckData {
  underlying: string
  underlying_symbol: string
  underlying_ltp: number
  forward: number
  option_symbol: string
  exchange: string
  option_type: 'CE' | 'PE'
  side: 'BUY' | 'SELL'
  strike: number
  atm_strike: number
  expiry_date: string
  days_to_expiry: number
  lot_size: number | null
  entry_price: number
  interest_rate: number
  greeks: { delta: number; gamma: number; theta: number; vega: number }
  volatility: {
    iv: number
    atm_iv: number
    next_expiry_iv: number | null
    next_expiry_date: string | null
    term_gap: number | null
    term_note: string | null
    hv20: number | null
    hv60: number | null
    iv_percentile: number | null
    history_days: number
    hv20_series: number[]
  }
  event: {
    detected: boolean
    flagged: boolean
    base_iv: number | null
    event_move_pct: number | null
  }
  implied_move_pct: number | null
  priced_move_hold_pct: number | null
  liquidity: { stale: boolean; spread_pct: number | null; volume: number | null }
  straddle_price: number | null
  scenario: {
    target_move_pct: number
    holding_days: number
    iv_after: number
    iv_after_source: string
    pnl_at_target: number
    pnl_at_target_pct: number | null
    pnl_at_target_no_crush: number
    pnl_at_target_no_crush_pct: number | null
    breakeven_move_pct: number | null
    breakeven_move_no_crush_pct: number | null
    historical_hit_rate_pct: number | null
  }
  heatmap: { moves_pct: number[]; ivs: number[]; pnl: number[][] }
  decay_path: { day: number; pnl_flat_iv: number; pnl_crushed_iv: number }[]
  verdict: {
    level: SignalLevel
    headline: string
    signals: SafetySignal[]
    suggestions: string[]
  }
}

export interface SafetyCheckResponse {
  status: 'success' | 'error'
  message?: string
  data?: SafetyCheckData
}

export interface SafetyCheckParams {
  underlying: string
  exchange: string
  expiry_date: string
  next_expiry_date?: string
  option_type: 'CE' | 'PE'
  side: 'BUY' | 'SELL'
  strike?: number | null
  target_move_pct: number
  holding_days: number
  event_expected: boolean
  iv_after_override?: number | null
}

export const optionSafetyApi = {
  check: async (params: SafetyCheckParams): Promise<SafetyCheckResponse> => {
    const response = await webClient.post<SafetyCheckResponse>('/optionsafety/api/check', params)
    return response.data
  },
}
