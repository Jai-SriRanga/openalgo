import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronsUpDown,
  ShieldAlert,
  XCircle,
} from 'lucide-react'
import type * as PlotlyTypes from 'plotly.js'
import { useEffect, useMemo, useRef, useState } from 'react'
import { ivSmileApi } from '@/api/iv-smile'
import { optionSafetyApi, type SafetyCheckData, type SignalLevel } from '@/api/option-safety'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useSupportedExchanges } from '@/hooks/useSupportedExchanges'
import Plot from '@/lib/Plot2D'
import { serverSentence } from '@/lib/serverSentence'
import { useThemeStore } from '@/stores/themeStore'
import { showToast } from '@/utils/toast'

function convertExpiryForAPI(expiry: string): string {
  if (!expiry) return ''
  const parts = expiry.split('-')
  if (parts.length === 3) {
    return `${parts[0]}${parts[1].toUpperCase()}${parts[2].slice(-2)}`
  }
  return expiry.replace(/-/g, '').toUpperCase()
}

const LEVEL_STYLE: Record<SignalLevel, { box: string; text: string; label: string }> = {
  green: {
    box: 'border-emerald-500/40 bg-emerald-500/10',
    text: 'text-emerald-600 dark:text-emerald-400',
    label: 'GO',
  },
  amber: {
    box: 'border-amber-500/40 bg-amber-500/10',
    text: 'text-amber-600 dark:text-amber-400',
    label: 'CAUTION',
  },
  red: {
    box: 'border-red-500/40 bg-red-500/10',
    text: 'text-red-600 dark:text-red-400',
    label: 'AVOID',
  },
}

function LevelIcon({ level, className }: { level: SignalLevel; className?: string }) {
  if (level === 'green') return <CheckCircle2 className={className} />
  if (level === 'amber') return <AlertTriangle className={className} />
  return <XCircle className={className} />
}

const fmt = (v: number | null | undefined, nd = 2, suffix = '') =>
  v === null || v === undefined || Number.isNaN(v) ? 'n/a' : `${v.toFixed(nd)}${suffix}`
const signed = (v: number | null | undefined, nd = 2, suffix = '') =>
  v === null || v === undefined ? 'n/a' : `${v >= 0 ? '+' : ''}${v.toFixed(nd)}${suffix}`

export default function OptionSafety() {
  const { mode, appMode } = useThemeStore()
  const { toolsFnoExchanges, defaultToolsFnoExchange, defaultUnderlyings } = useSupportedExchanges()
  const isAnalyzer = appMode === 'analyzer'
  const isDark = mode === 'dark' || isAnalyzer

  const [exchange, setExchange] = useState(defaultToolsFnoExchange)
  const [underlyings, setUnderlyings] = useState<string[]>(
    defaultUnderlyings[defaultToolsFnoExchange] || []
  )
  const [underlyingOpen, setUnderlyingOpen] = useState(false)
  const [underlying, setUnderlying] = useState(
    defaultUnderlyings[defaultToolsFnoExchange]?.[0] || ''
  )
  const [expiries, setExpiries] = useState<string[]>([])
  const [expiry, setExpiry] = useState('')

  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [optionType, setOptionType] = useState<'CE' | 'PE'>('CE')
  const [strike, setStrike] = useState('')
  const [targetMove, setTargetMove] = useState('2')
  const [holdingDays, setHoldingDays] = useState('1')
  const [eventExpected, setEventExpected] = useState(false)
  const [ivAfter, setIvAfter] = useState('')

  const [result, setResult] = useState<SafetyCheckData | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const requestIdRef = useRef(0)

  useEffect(() => {
    setExchange((prev) =>
      prev && toolsFnoExchanges.some((ex) => ex.value === prev) ? prev : defaultToolsFnoExchange
    )
  }, [defaultToolsFnoExchange, toolsFnoExchanges])

  useEffect(() => {
    const defaults = defaultUnderlyings[exchange] || []
    setUnderlyings(defaults)
    setUnderlying(defaults[0] || '')
    setExpiries([])
    setExpiry('')
    setResult(null)
    let cancelled = false
    ivSmileApi
      .getUnderlyings(exchange)
      .then((r) => {
        if (cancelled || r.status !== 'success' || r.underlyings.length === 0) return
        setUnderlyings(r.underlyings)
        if (!r.underlyings.includes(defaults[0])) setUnderlying(r.underlyings[0])
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [exchange, defaultUnderlyings])

  useEffect(() => {
    if (!underlying) return
    setExpiries([])
    setExpiry('')
    setResult(null)
    let cancelled = false
    ivSmileApi
      .getExpiries(exchange, underlying)
      .then((r) => {
        if (cancelled) return
        if (r.status === 'success' && r.expiries.length > 0) {
          setExpiries(r.expiries)
          setExpiry(r.expiries[0])
        }
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [underlying, exchange])

  // A direction hint so the target sign matches the trade the trader has in mind
  const directionHint =
    side === 'BUY'
      ? optionType === 'CE'
        ? 'Positive = you expect the underlying to rise'
        : 'Negative = you expect the underlying to fall'
      : optionType === 'CE'
        ? 'The move you expect (selling a call wants it below the strike)'
        : 'The move you expect (selling a put wants it above the strike)'

  const runCheck = async () => {
    if (!underlying || !expiry) {
      showToast.error('Pick an underlying and expiry first')
      return
    }
    const target = Number.parseFloat(targetMove)
    const days = Number.parseFloat(holdingDays)
    if (Number.isNaN(target) || Number.isNaN(days)) {
      showToast.error('Target move and holding days must be numbers')
      return
    }
    const idx = expiries.indexOf(expiry)
    const nextExpiry = idx >= 0 && idx + 1 < expiries.length ? expiries[idx + 1] : ''
    const requestId = ++requestIdRef.current
    setIsLoading(true)
    try {
      const response = await optionSafetyApi.check({
        underlying,
        exchange,
        expiry_date: convertExpiryForAPI(expiry),
        next_expiry_date: nextExpiry ? convertExpiryForAPI(nextExpiry) : undefined,
        option_type: optionType,
        side,
        strike: strike ? Number.parseFloat(strike) : null,
        target_move_pct: target,
        holding_days: days,
        event_expected: eventExpected,
        iv_after_override: ivAfter ? Number.parseFloat(ivAfter) : null,
      })
      if (requestIdRef.current !== requestId) return
      if (response.status === 'success' && response.data) {
        setResult(response.data)
      } else {
        showToast.error(response.message || 'Safety check failed')
      }
    } catch (error) {
      if (requestIdRef.current !== requestId) return
      showToast.error(serverSentence(error, 'Safety check failed'))
    } finally {
      if (requestIdRef.current === requestId) setIsLoading(false)
    }
  }

  const theme = useMemo(
    () => ({
      text: isDark ? '#e0e0e0' : '#333333',
      grid: isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.08)',
      marker: isDark ? 'rgba(255,255,255,0.85)' : 'rgba(0,0,0,0.7)',
    }),
    [isDark]
  )

  const plotConfig: Partial<PlotlyTypes.Config> = useMemo(
    () => ({ displaylogo: false, responsive: true, displayModeBar: false }),
    []
  )

  const baseLayout = useMemo(
    (): Partial<PlotlyTypes.Layout> => ({
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      font: { color: theme.text, family: 'system-ui, sans-serif', size: 11 },
      margin: { l: 60, r: 20, t: 40, b: 50 },
    }),
    [theme]
  )

  const decayPlot = useMemo(() => {
    if (!result) return null
    const entry = result.entry_price || 1
    const days = result.decay_path.map((d) => d.day)
    const ivChanges = Math.abs(result.scenario.iv_after - result.volatility.iv) >= 0.05
    const data: PlotlyTypes.Data[] = [
      {
        x: days,
        y: result.decay_path.map((d) => (100 * d.pnl_flat_iv) / entry),
        type: 'scatter',
        mode: 'lines',
        name: `IV stays ${result.volatility.iv.toFixed(1)}%`,
        line: { color: '#3b82f6', width: 2 },
      },
    ]
    // Without an expected IV change both lines coincide; a second "drops to"
    // line at the same level would only mislead.
    if (ivChanges) {
      data.push({
        x: days,
        y: result.decay_path.map((d) => (100 * d.pnl_crushed_iv) / entry),
        type: 'scatter',
        mode: 'lines',
        name: `IV ${result.scenario.iv_after < result.volatility.iv ? 'drops' : 'rises'} to ${result.scenario.iv_after.toFixed(1)}%`,
        line: { color: '#ef4444', width: 2 },
      })
    }
    const layout: Partial<PlotlyTypes.Layout> = {
      ...baseLayout,
      xaxis: { title: { text: 'Days held' }, gridcolor: theme.grid },
      yaxis: { title: { text: 'P&L % of premium' }, gridcolor: theme.grid, zeroline: true },
      legend: { orientation: 'h', y: -0.25 },
      shapes: [
        {
          type: 'line',
          x0: result.scenario.holding_days,
          x1: result.scenario.holding_days,
          y0: 0,
          y1: 1,
          yref: 'paper',
          line: { color: theme.marker, dash: 'dot', width: 1 },
        },
      ],
    }
    return { data, layout }
  }, [result, baseLayout, theme])

  const volPlot = useMemo(() => {
    if (!result || result.volatility.hv20_series.length === 0) return null
    const s = result.volatility.hv20_series
    const x = s.map((_, i) => i - s.length + 1)
    const data: PlotlyTypes.Data[] = [
      {
        x,
        y: s,
        type: 'scatter',
        mode: 'lines',
        name: '20-day realised vol',
        line: { color: '#8b5cf6', width: 1.5 },
      },
      {
        x: [x[0], 0],
        y: [result.volatility.atm_iv, result.volatility.atm_iv],
        type: 'scatter',
        mode: 'lines',
        name: `ATM IV now ${result.volatility.atm_iv.toFixed(1)}%`,
        line: { color: '#f59e0b', width: 2, dash: 'dash' },
      },
    ]
    const layout: Partial<PlotlyTypes.Layout> = {
      ...baseLayout,
      xaxis: { title: { text: 'Trading days ago' }, gridcolor: theme.grid },
      yaxis: { title: { text: 'Volatility %' }, gridcolor: theme.grid },
      legend: { orientation: 'h', y: -0.25 },
    }
    return { data, layout }
  }, [result, baseLayout, theme])

  const v = result?.verdict
  const style = v ? LEVEL_STYLE[v.level] : null

  return (
    <div className="py-6 space-y-4">
      <div>
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <ShieldAlert className="h-6 w-6" /> Option Safety Check
        </h1>
        <p className="text-muted-foreground mt-1 text-sm">
          Before you buy or sell: if the underlying moves the way your chart says, will this option
          still pay after IV crush and time decay?
        </p>
      </div>

      {/* Inputs */}
      <Card>
        <CardContent className="p-4 grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4 items-end">
          <div className="space-y-1.5">
            <Label>Exchange</Label>
            <Select value={exchange} onValueChange={setExchange}>
              <SelectTrigger>
                <SelectValue placeholder="Exchange" />
              </SelectTrigger>
              <SelectContent>
                {toolsFnoExchanges.map((ex) => (
                  <SelectItem key={ex.value} value={ex.value}>
                    {ex.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Underlying</Label>
            <Popover open={underlyingOpen} onOpenChange={setUnderlyingOpen}>
              <PopoverTrigger asChild>
                <Button
                  variant="outline"
                  role="combobox"
                  aria-expanded={underlyingOpen}
                  className="w-full justify-between"
                >
                  {underlying || 'Underlying'}
                  <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-48 p-0" align="start">
                <Command>
                  <CommandInput placeholder="Search underlying..." />
                  <CommandList>
                    <CommandEmpty>No underlying found</CommandEmpty>
                    <CommandGroup>
                      {underlyings.map((u) => (
                        <CommandItem
                          key={u}
                          value={u}
                          onSelect={() => {
                            setUnderlying(u)
                            setUnderlyingOpen(false)
                          }}
                        >
                          <Check
                            className={`mr-2 h-4 w-4 ${underlying === u ? 'opacity-100' : 'opacity-0'}`}
                          />
                          {u}
                        </CommandItem>
                      ))}
                    </CommandGroup>
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </div>

          <div className="space-y-1.5">
            <Label>Expiry</Label>
            <Select value={expiry} onValueChange={setExpiry} disabled={expiries.length === 0}>
              <SelectTrigger>
                <SelectValue placeholder="Expiry" />
              </SelectTrigger>
              <SelectContent>
                {expiries.map((e) => (
                  <SelectItem key={e} value={e}>
                    {e}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label>Trade</Label>
            <div className="flex gap-1">
              {(['BUY', 'SELL'] as const).map((s) => (
                <Button
                  key={s}
                  size="sm"
                  className="flex-1"
                  variant={side === s ? 'default' : 'outline'}
                  onClick={() => setSide(s)}
                >
                  {s}
                </Button>
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <Label>Option</Label>
            <div className="flex gap-1">
              {(['CE', 'PE'] as const).map((t) => (
                <Button
                  key={t}
                  size="sm"
                  className="flex-1"
                  variant={optionType === t ? 'default' : 'outline'}
                  onClick={() => {
                    setOptionType(t)
                    const n = Number.parseFloat(targetMove)
                    if (!Number.isNaN(n) && side === 'BUY') {
                      setTargetMove(String(t === 'CE' ? Math.abs(n) : -Math.abs(n)))
                    }
                  }}
                >
                  {t}
                </Button>
              ))}
            </div>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="os-strike">Strike</Label>
            <Input
              id="os-strike"
              inputMode="decimal"
              placeholder="ATM"
              value={strike}
              onChange={(e) => setStrike(e.target.value)}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="os-target" title={directionHint}>
              Expected move %
            </Label>
            <Input
              id="os-target"
              inputMode="decimal"
              value={targetMove}
              onChange={(e) => setTargetMove(e.target.value)}
              title={directionHint}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="os-days">Holding days</Label>
            <Input
              id="os-days"
              inputMode="decimal"
              value={holdingDays}
              onChange={(e) => setHoldingDays(e.target.value)}
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="os-ivafter">IV after (optional)</Label>
            <Input
              id="os-ivafter"
              inputMode="decimal"
              placeholder="auto"
              value={ivAfter}
              onChange={(e) => setIvAfter(e.target.value)}
            />
          </div>

          <div className="flex items-center gap-2 col-span-2 md:col-span-2 pb-2">
            <Checkbox
              id="os-event"
              checked={eventExpected}
              onCheckedChange={(c) => setEventExpected(c === true)}
            />
            <Label htmlFor="os-event" className="font-normal">
              Event before expiry (results, policy, budget, data release)
            </Label>
          </div>

          <Button onClick={runCheck} disabled={isLoading || !expiry}>
            {isLoading ? 'Checking...' : 'Run check'}
          </Button>
        </CardContent>
      </Card>

      {!result && !isLoading && (
        <Card>
          <CardContent className="p-8 text-center text-muted-foreground text-sm">
            Pick the option you plan to trade, enter the move your chart analysis expects, and run
            the check. You will see whether IV crush and theta can wipe out a correct call.
          </CardContent>
        </Card>
      )}

      {result && v && style && (
        <>
          {/* Verdict */}
          <div className={`rounded-lg border p-4 ${style.box}`}>
            <div className="flex items-start gap-3">
              <LevelIcon level={v.level} className={`h-8 w-8 shrink-0 ${style.text}`} />
              <div className="space-y-1">
                <div className={`text-sm font-bold tracking-wide ${style.text}`}>{style.label}</div>
                <div className="text-lg font-semibold">{v.headline}</div>
                <div className="text-sm text-muted-foreground">
                  {result.side} {result.option_symbol} @ {fmt(result.entry_price)} · underlying{' '}
                  {fmt(result.underlying_ltp)} · {fmt(result.days_to_expiry, 1)} days to expiry
                  {result.lot_size ? ` · lot ${result.lot_size}` : ''}
                </div>
              </div>
            </div>
            {v.suggestions.length > 0 && (
              <ul className="mt-3 ml-11 list-disc space-y-1 text-sm">
                {v.suggestions.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            )}
          </div>

          {/* Key numbers */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            <Stat
              label="P&L at your target"
              value={signed(result.scenario.pnl_at_target_pct, 1, '%')}
              sub={`without IV drop ${signed(result.scenario.pnl_at_target_no_crush_pct, 1, '%')}`}
              tone={result.scenario.pnl_at_target >= 0 ? 'pos' : 'neg'}
            />
            <Stat
              label={result.side === 'BUY' ? 'Move to break even' : 'Move it can absorb'}
              value={signed(result.scenario.breakeven_move_pct, 2, '%')}
              sub={`without IV drop ${signed(result.scenario.breakeven_move_no_crush_pct, 2, '%')}`}
            />
            <Stat
              label="IV now → after"
              value={`${fmt(result.volatility.iv, 1)} → ${fmt(result.scenario.iv_after, 1)}%`}
              sub={result.scenario.iv_after_source}
            />
            <Stat
              label={`Priced-in move (${result.scenario.holding_days} day)`}
              value={`±${fmt(result.priced_move_hold_pct ?? result.implied_move_pct, 2, '%')}`}
              sub={`by expiry ±${fmt(result.implied_move_pct, 2)}%${
                result.event.event_move_pct
                  ? ` · event alone ±${fmt(result.event.event_move_pct, 2)}%`
                  : ''
              }`}
            />
            <Stat
              label="IV vs realised (20d)"
              value={
                result.volatility.hv20
                  ? `x${(result.volatility.iv / result.volatility.hv20).toFixed(2)}`
                  : 'n/a'
              }
              sub={`HV20 ${fmt(result.volatility.hv20, 1)}% · pctile ${fmt(result.volatility.iv_percentile, 0)}`}
            />
            <Stat
              label="Greeks (per unit)"
              value={`Δ ${fmt(result.greeks.delta, 2)}`}
              sub={`Vega ${fmt(result.greeks.vega, 2)} · Theta ${fmt(result.greeks.theta, 2)}/day`}
            />
          </div>

          {/* Signals */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {v.signals.map((s) => {
              const st = LEVEL_STYLE[s.level]
              return (
                <div key={s.key} className={`rounded-lg border p-3 ${st.box}`}>
                  <div className="flex items-center gap-2 font-medium text-sm">
                    <LevelIcon level={s.level} className={`h-4 w-4 ${st.text}`} />
                    {s.title}
                  </div>
                  <p className="text-sm text-muted-foreground mt-1">{s.detail}</p>
                </div>
              )
            })}
          </div>

          {/* Charts */}
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            <Card className="xl:col-span-2">
              <CardHeader className="pb-0">
                <CardTitle className="text-base">
                  P&L map after {result.scenario.holding_days} day(s): underlying move × IV level
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                  Each cell is P&L as a % of the premium. The outlined column is your target move;
                  read along the "after event" row to see what the trade earns once IV has dropped.
                </p>
              </CardHeader>
              <CardContent className="p-2">
                <PnlMap result={result} />
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-0">
                <CardTitle className="text-base">
                  Waiting cost at your target move (
                  {signed(result.scenario.target_move_pct, 2, '%')})
                </CardTitle>
              </CardHeader>
              <CardContent className="p-2">
                {decayPlot && (
                  <Plot
                    data={decayPlot.data}
                    layout={decayPlot.layout}
                    config={plotConfig}
                    useResizeHandler
                    style={{ width: '100%', height: '320px' }}
                  />
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-0">
                <CardTitle className="text-base">
                  Is IV high? ATM IV vs 1-year realised vol
                </CardTitle>
              </CardHeader>
              <CardContent className="p-2">
                {volPlot ? (
                  <Plot
                    data={volPlot.data}
                    layout={volPlot.layout}
                    config={plotConfig}
                    useResizeHandler
                    style={{ width: '100%', height: '320px' }}
                  />
                ) : (
                  <div className="h-[320px] flex items-center justify-center text-sm text-muted-foreground">
                    Not enough daily history for this underlying
                  </div>
                )}
              </CardContent>
            </Card>
          </div>

          <div className="flex flex-wrap gap-2 text-xs">
            <Badge variant="secondary">ATM {result.atm_strike}</Badge>
            <Badge variant="secondary">ATM IV {fmt(result.volatility.atm_iv, 1)}%</Badge>
            {result.volatility.next_expiry_iv !== null && (
              <Badge variant="secondary">
                Next expiry ({result.volatility.next_expiry_date}) IV{' '}
                {fmt(result.volatility.next_expiry_iv, 1)}%
              </Badge>
            )}
            {result.volatility.term_gap !== null && (
              <Badge variant={result.event.detected ? 'destructive' : 'secondary'}>
                Term gap {signed(result.volatility.term_gap, 1)} pts
                {result.event.detected ? ' · event priced' : ''}
              </Badge>
            )}
            {result.volatility.term_note && (
              <Badge variant="secondary">{result.volatility.term_note}</Badge>
            )}
            <Badge variant="secondary">Forward {fmt(result.forward)}</Badge>
            {result.liquidity.spread_pct !== null && (
              <Badge variant="secondary">Spread {fmt(result.liquidity.spread_pct, 1)}%</Badge>
            )}
            <Badge variant="secondary">History {result.volatility.history_days} days</Badge>
          </div>
          <p className="text-xs text-muted-foreground">
            Black-76 scenario model using live quotes. Estimates, not predictions; not investment
            advice.
          </p>
        </>
      )}
    </div>
  )
}

function heatColor(pct: number, scale: number): string {
  const a = Math.min(1, Math.abs(pct) / scale) * 0.75
  return pct >= 0 ? `rgba(22, 163, 74, ${a})` : `rgba(220, 38, 38, ${a})`
}

function PnlMap({ result }: { result: SafetyCheckData }) {
  const { moves_pct, ivs, pnl } = result.heatmap
  const entry = result.entry_price || 1
  const pct = pnl.map((row) => row.map((v) => (100 * v) / entry))
  const scale = Math.max(25, ...pct.flat().map((v) => Math.abs(v)))
  const ivNow = result.volatility.iv
  const ivAfter = result.scenario.iv_after
  const nearest = (xs: number[], x: number) =>
    xs.reduce((best, v, i) => (Math.abs(v - x) < Math.abs(xs[best] - x) ? i : best), 0)
  const targetCol = nearest(moves_pct, result.scenario.target_move_pct)
  const nowRow = nearest(ivs, ivNow)
  const afterRow = nearest(ivs, ivAfter)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  // On a narrow screen the target column can sit off to the side: centre it
  // once layout has settled, and again whenever the table changes width.
  useEffect(() => {
    const box = scrollRef.current
    const cell = box?.querySelectorAll('thead th')[targetCol + 1]
    if (!box || !cell) return
    const centre = () => {
      if (box.scrollWidth <= box.clientWidth) return
      const b = box.getBoundingClientRect()
      const c = cell.getBoundingClientRect()
      box.scrollLeft += c.left - b.left - (b.width - c.width) / 2
    }
    const frame = requestAnimationFrame(centre)
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(centre)
    observer?.observe(box)
    return () => {
      cancelAnimationFrame(frame)
      observer?.disconnect()
    }
  }, [targetCol])
  return (
    <div ref={scrollRef} className="overflow-x-auto">
      <table className="w-full border-separate border-spacing-0.5 text-xs tabular-nums">
        <thead>
          <tr>
            <th className="sticky left-0 bg-card px-2 py-1 text-left font-medium text-muted-foreground">
              IV \ move
            </th>
            {moves_pct.map((m, j) => (
              <th
                key={m}
                className={`px-1.5 py-1 text-center font-medium ${
                  j === targetCol ? 'text-foreground' : 'text-muted-foreground'
                }`}
              >
                {m >= 0 ? '+' : ''}
                {m.toFixed(1)}%
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ivs.map((iv, i) => (
            <tr key={iv}>
              <th className="sticky left-0 bg-card px-2 py-1 text-left font-medium whitespace-nowrap">
                {iv.toFixed(1)}%
                {i === nowRow && <span className="ml-1 text-muted-foreground">now</span>}
                {i === afterRow && i !== nowRow && (
                  <span className="ml-1 text-amber-600 dark:text-amber-400">after event</span>
                )}
              </th>
              {pct[i].map((v, j) => (
                <td
                  key={moves_pct[j]}
                  title={`Move ${moves_pct[j].toFixed(2)}%, IV ${iv.toFixed(1)}%: ${v.toFixed(1)}% of premium`}
                  className={`rounded px-1.5 py-1 text-center ${
                    j === targetCol ? 'ring-1 ring-foreground/60' : ''
                  } ${i === afterRow ? 'font-semibold' : ''}`}
                  style={{ backgroundColor: heatColor(v, scale) }}
                >
                  {v >= 0 ? '+' : ''}
                  {v.toFixed(0)}%
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Stat({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string
  sub?: string
  tone?: 'pos' | 'neg'
}) {
  const color =
    tone === 'pos'
      ? 'text-emerald-600 dark:text-emerald-400'
      : tone === 'neg'
        ? 'text-red-600 dark:text-red-400'
        : ''
  return (
    <Card className="py-0">
      <CardContent className="p-3">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className={`text-lg font-semibold ${color}`}>{value}</div>
        {sub && (
          <div className="text-xs text-muted-foreground truncate" title={sub}>
            {sub}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
