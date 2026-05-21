import { useState, useEffect, useRef, useCallback, useMemo, Fragment } from 'react'
import ExcelJS from 'exceljs'
import './App.css'
import { trackShipment, fetchULD } from './api'
import type { TrackingResult, FlightLeg } from './types'

// ── Config ────────────────────────────────────────────────────────────
const MAX_LEGS = 3   // max flight leg columns (1st, 2nd, 3rd)
const MAX_AWBS = 30
const MAWB_COL_W = 120  // px — fixed width of sticky MAWB column

// Light cell tint + dark header bg for each flight group
const LEG_PALETTE = [
  { hdr: '#1a3a5c', cell: '#eef4ff' },  // 1st – blue
  { hdr: '#155b35', cell: '#edfaf2' },  // 2nd – green
  { hdr: '#6b3d10', cell: '#fff8ee' },  // 3rd – amber
] as const

// MAWB must be NNN-NNNNNNN or NNN-NNNNNNNN (3 digits, dash, 7-8 digits)
const AWB_RE = /^\d{3}-\d{7,8}$/

// ── Notes banner ──────────────────────────────────────────────────────
const NOTES = [
  {
    label:  'Data',
    text:   'All tracking data is extracted directly from each airline\'s website. If any date or time looks suspicious, please verify with the specific airline specialist.',
    bg:     '#fff7e6',
    border: '#f59e0b',
  },
  {
    label:  'Cargo Info',
    text:   'ULD / cargo details are pulled from each airline\'s system. If no ULD info is shown, the airline may not provide it publicly — contact their specialist directly.',
    bg:     '#f0f9ff',
    border: '#38bdf8',
  },
  {
    label:  'Availability',
    text:   'Not all airline websites are accessible at all times. If a result shows "not found", the airline\'s tracking system may be temporarily unavailable or has no recent updates.',
    bg:     '#f0fdf4',
    border: '#4ade80',
  },
]

function NotesBanner() {
  return (
    <div className="notes-banner">
      {NOTES.map(n => (
        <div
          key={n.label}
          className="note-row"
          style={{ background: n.bg, borderLeft: `4px solid ${n.border}` }}
        >
          <strong className="note-label">{n.label}:</strong>
          <span className="note-text">{n.text}</span>
        </div>
      ))}
    </div>
  )
}

// ── Types ─────────────────────────────────────────────────────────────
type ULDEntry = string

type ShipmentRow =
  | { kind: 'loading'; awb: string }
  | { kind: 'error';   awb: string; message: string }
  | { kind: 'ok';      awb: string; data: TrackingResult; ulds: ULDEntry[] | null }

// ── Helpers ───────────────────────────────────────────────────────────
function parseAWBs(raw: string): string[] {
  return raw.split(/[\n,;]+/).map(s => s.trim().replace(/\s+/g, '')).filter(Boolean)
}

function badgeClass(s: string) {
  const t = (s || '').toLowerCase()
  if (t.includes('booked') || t.includes('bkd')) return 'badge badge-booked'
  if (t.includes('depart') || t === 'dep')        return 'badge badge-departed'
  if (t.includes('arriv') || t.includes('dlv') || t.includes('delivered')) return 'badge badge-arrived'
  return 'badge badge-default'
}

function dotClass(s: FlightLeg['departure_status']) { return `dot dot-${s}` }

function fmtTime(time: string, date: string, status: FlightLeg['departure_status']) {
  if (!time) return null
  return (
    <div className="c-time">
      <div className="dt">{date} {time}</div>
      <div className="s"><span className={dotClass(status)} />{status}</div>
    </div>
  )
}

// ── Hook: fetch ULD for a result ──────────────────────────────────────
function useULD(row: ShipmentRow, onDone: (awb: string, ulds: ULDEntry[]) => void) {
  const fetched = useRef(false)

  useEffect(() => {
    if (row.kind !== 'ok' || fetched.current) return
    fetched.current = true

    const legs = row.data.flights.filter(f => f.flrs_id > 0)
    if (!legs.length) { onDone(row.awb, []); return }

    const [prefix, awbNum] = row.data.awb.split('-')
    Promise.all(
      legs.map(leg =>
        fetchULD(prefix, awbNum, leg.flight_no, leg.from_airport, leg.to_airport, leg.departure_date, leg.flrs_id)
          .then(res => res.ulds.map(u => `${u.uld}  (${u.pieces} pcs)  [${leg.flight_no}]`))
          .catch(err => { console.error('[ULD] fetch failed for', leg.flight_no, err); return [] as string[]; })
      )
    ).then(nested => { onDone(row.awb, nested.flat()) })
  }, [row, onDone])
}

// ── Row component ─────────────────────────────────────────────────────
function TableRow({
  row, maxCargo, onULDReady, statusWidth,
}: {
  row: ShipmentRow
  maxCargo: number
  onULDReady: (awb: string, ulds: ULDEntry[]) => void
  statusWidth: number
}) {
  // eslint-disable-next-line react-hooks/rules-of-hooks
  useULD(row, onULDReady)

  const legSpan   = MAX_LEGS * 5
  const totalCols = 2 + legSpan + maxCargo

  const awbCell = (awb: string) => (
    <td
      className="c-awb sticky-awb"
      style={{ width: MAWB_COL_W, minWidth: MAWB_COL_W, maxWidth: MAWB_COL_W }}
    >
      {awb}
    </td>
  )

  if (row.kind === 'loading') {
    return (
      <tr>
        {awbCell(row.awb)}
        <td
          colSpan={totalCols - 1}
          className="c-cargo-loading sticky-status"
          style={{ left: MAWB_COL_W, width: statusWidth, minWidth: statusWidth }}
        >
          <span className="spinner" />Fetching…
        </td>
      </tr>
    )
  }
  if (row.kind === 'error') {
    return (
      <tr>
        {awbCell(row.awb)}
        <td
          colSpan={totalCols - 1}
          className="c-err sticky-status"
          style={{ left: MAWB_COL_W, width: statusWidth, minWidth: statusWidth }}
        >
          ⚠ {row.message}
        </td>
      </tr>
    )
  }

  const { data, ulds } = row
  const legs = data.flights

  const legCells = Array.from({ length: MAX_LEGS }, (_, i) => {
    const leg  = legs[i]
    const sep  = i === 0 ? ' grp-sep' : ''
    const bg   = LEG_PALETTE[i].cell
    if (!leg) return (
      <Fragment key={i}>
        <td className={`c-empty${sep}`} style={{ background: bg }}>—</td>
        <td className="c-empty"        style={{ background: bg }}>—</td>
        <td className="c-empty"        style={{ background: bg }}>—</td>
        <td className="c-empty"        style={{ background: bg }}>—</td>
        <td className="c-empty"        style={{ background: bg }}>—</td>
      </Fragment>
    )
    return (
      <Fragment key={i}>
        <td className={`c-flight${sep}`} style={{ background: bg }}>
          <a
            href={`https://www.flightaware.com/live/flight/${leg.flight_no.replace(/^([A-Z]{1,3})0+(\d)/, '$1$2')}`}
            target="_blank"
            rel="noreferrer"
            className="flight-link"
          >
            {leg.flight_no}
          </a>
        </td>
        <td className="c-ap" style={{ background: bg }}>{leg.from_airport}</td>
        <td className="c-ap" style={{ background: bg }}>{leg.to_airport}</td>
        <td style={{ background: bg }}>{fmtTime(leg.departure_time, leg.departure_date, leg.departure_status) || <span className="c-empty">—</span>}</td>
        <td style={{ background: bg }}>{fmtTime(leg.arrival_time,   leg.arrival_date,   leg.arrival_status)   || <span className="c-empty">—</span>}</td>
      </Fragment>
    )
  })

  const cargoCells: React.ReactNode[] = []
  for (let i = 0; i < maxCargo; i++) {
    if (ulds === null) {
      cargoCells.push(
        <td key={`c${i}`} className="c-cargo-loading">
          {i === 0 && <><span className="spinner" />Loading…</>}
        </td>
      )
    } else {
      const entry = ulds[i]
      cargoCells.push(
        <td key={`c${i}`} className={entry ? 'c-cargo' : 'c-empty'}>{entry ?? '—'}</td>
      )
    }
  }

  return (
    <tr>
      {awbCell(data.awb)}
      <td
        className="c-status sticky-status"
        style={{ left: MAWB_COL_W, width: statusWidth, minWidth: statusWidth }}
      >
        <span className={badgeClass(data.status)}>{data.status || data.status_code}</span>
      </td>
      {legCells}
      {cargoCells}
    </tr>
  )
}

// ── Table header ──────────────────────────────────────────────────────
const LEG_LABELS = ['1st Flight', '2nd Flight', '3rd Flight']

function TableHeader({
  maxCargo,
  sortCol, sortDir, onSort,
  statusWidth, onResizeStart,
}: {
  maxCargo: number
  sortCol: 'mawb' | 'firstArr' | null
  sortDir: 'asc' | 'desc'
  onSort: (col: 'mawb' | 'firstArr') => void
  statusWidth: number
  onResizeStart: (e: React.MouseEvent) => void
}) {
  const sortIcon = (col: 'mawb' | 'firstArr') =>
    sortCol === col ? (sortDir === 'asc' ? ' ▲' : ' ▼') : ' ⇅'

  return (
    <thead>
      <tr>
        {/* Sticky MAWB — sortable */}
        <th
          className="sticky-awb sortable"
          style={{ width: MAWB_COL_W, minWidth: MAWB_COL_W }}
          onClick={() => onSort('mawb')}
        >
          MAWB<span className="sort-icon">{sortIcon('mawb')}</span>
        </th>
        {/* Sticky resizable Status */}
        <th
          className="sticky-status"
          style={{ left: MAWB_COL_W, width: statusWidth, minWidth: statusWidth }}
        >
          Status
          <div className="col-resize-handle" onMouseDown={onResizeStart} />
        </th>
        {/* Single-row flight group headers — group label + sub-label stacked in first col */}
        {Array.from({ length: MAX_LEGS }, (_, i) => (
          <Fragment key={i}>
            <th
              className={`leg-first-col${i === 0 ? ' grp-sep' : ''}`}
              style={{ background: LEG_PALETTE[i].hdr }}
            >
              <div className="leg-grp-label">{LEG_LABELS[i]}</div>
              <div>Flight</div>
            </th>
            <th style={{ background: LEG_PALETTE[i].hdr }}>From</th>
            <th style={{ background: LEG_PALETTE[i].hdr }}>To</th>
            <th style={{ background: LEG_PALETTE[i].hdr }}>ATD / ETD</th>
            <th
              style={{ background: LEG_PALETTE[i].hdr }}
              className={i === 0 ? 'sortable' : ''}
              onClick={i === 0 ? () => onSort('firstArr') : undefined}
            >
              ATA / ETA{i === 0 && <span className="sort-icon">{sortIcon('firstArr')}</span>}
            </th>
          </Fragment>
        ))}
        {Array.from({ length: maxCargo }, (_, i) => (
          <th key={i} className={`cargo-hdr${i === 0 ? ' grp-sep' : ''}`}>
            Cargo info {i + 1}
          </th>
        ))}
      </tr>
    </thead>
  )
}

// ── App ───────────────────────────────────────────────────────────────
export default function App() {
  const [input, setInput] = useState('')
  const [rows, setRows]   = useState<ShipmentRow[]>([])
  const [busy, setBusy]   = useState(false)
  const [searchKey, setSearchKey] = useState(0)

  // ── Column sort state ─────────────────────────────────────────────
  const [sortCol, setSortCol] = useState<'mawb' | 'firstArr' | null>(null)
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('asc')

  function toggleSort(col: 'mawb' | 'firstArr') {
    if (sortCol === col) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortCol(col); setSortDir('asc') }
  }

  // ── Status column resize ───────────────────────────────────────────
  const [statusWidth, setStatusWidth] = useState(90)
  const resizingRef  = useRef(false)
  const resizeStartX = useRef(0)
  const resizeStartW = useRef(0)

  const startResize = useCallback((e: React.MouseEvent) => {
    resizingRef.current  = true
    resizeStartX.current = e.clientX
    resizeStartW.current = statusWidth
    e.preventDefault()
  }, [statusWidth])

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!resizingRef.current) return
      setStatusWidth(w => Math.max(50, resizeStartW.current + (e.clientX - resizeStartX.current)))
    }
    const onUp = () => { resizingRef.current = false }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => { window.removeEventListener('mousemove', onMove); window.removeEventListener('mouseup', onUp) }
  }, [])

  // Validate format as user types
  const all        = parseAWBs(input)
  const invalidAWBs = all.filter(a => a.length > 0 && !AWB_RE.test(a))

  const maxCargo = Math.max(
    1,
    ...rows.map(r => (r.kind === 'ok' && r.ulds ? r.ulds.length : 0))
  )

  // ── Sorted rows (MAWB asc/desc or 1st-flight arrival asc/desc) ───
  const sortedRows = useMemo(() => {
    if (!sortCol) return rows
    return [...rows].sort((a, b) => {
      let va = '', vb = ''
      if (sortCol === 'mawb') {
        va = a.awb; vb = b.awb
      } else {
        const fa = a.kind === 'ok' ? a.data.flights[0] : null
        const fb = b.kind === 'ok' ? b.data.flights[0] : null
        va = fa ? `${fa.arrival_date} ${fa.arrival_time}` : ''
        vb = fb ? `${fb.arrival_date} ${fb.arrival_time}` : ''
      }
      const cmp = va.localeCompare(vb)
      return sortDir === 'asc' ? cmp : -cmp
    })
  }, [rows, sortCol, sortDir])

  async function handleExport() {
    const legCount = 3
    const maxULD = Math.max(1, ...rows.map(r => (r.kind === 'ok' && r.ulds ? r.ulds.length : 0)))

    const baseHeaders = ['MAWB', 'Status', 'From', 'To', 'Pieces', 'Weight (kg)']
    const legHeaders: string[] = []
    for (let i = 1; i <= legCount; i++) {
      legHeaders.push(
        `Flight ${i} Flight`, `Flight ${i} From`, `Flight ${i} To`,
        `Flight ${i} Dept Datetime`, `Flight ${i} Dept Status`,
        `Flight ${i} Arr Datetime`,  `Flight ${i} Arr Status`,
      )
    }
    const uldHeaders = Array.from({ length: maxULD }, (_, i) => `Cargo ${i + 1}`)
    const allHeaders = [...baseHeaders, ...legHeaders, ...uldHeaders]

    // ARGB color palette (AA + RRGGBB)
    const LEG_ARGB  = ['FF1A3A5C', 'FF155B35', 'FF6B3D10'] as const
    const BASE_ARGB = 'FF3D3D3D'
    const CARGO_ARGB = 'FF2A5080'

    const wb = new ExcelJS.Workbook()
    const ws = wb.addWorksheet('Tracking')

    // Header row with colors
    const hdrRow = ws.addRow(allHeaders)
    hdrRow.eachCell({ includeEmpty: true }, (cell, colNum) => {
      const col = colNum - 1  // 0-based
      let bgArgb: string
      if (col < baseHeaders.length) {
        bgArgb = BASE_ARGB
      } else {
        const legCol = col - baseHeaders.length
        const legIdx = Math.floor(legCol / 7)
        bgArgb = legIdx < 3 ? LEG_ARGB[legIdx] : CARGO_ARGB
      }
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: bgArgb } }
      cell.font = { color: { argb: 'FFFFFFFF' }, bold: true }
      cell.alignment = { vertical: 'middle', horizontal: 'center', wrapText: false }
    })
    hdrRow.height = 20

    // Data rows
    rows.forEach(row => {
      if (row.kind === 'loading') { ws.addRow([row.awb, 'Loading...']); return }
      if (row.kind === 'error')   { ws.addRow([row.awb, `Error: ${row.message}`]); return }
      const { data, ulds } = row
      const base = [
        data.awb, data.status || data.status_code,
        data.from_airport, data.to_airport,
        data.total_pieces ?? '', data.total_weight_kg ?? '',
      ]
      const legCells: (string | number)[] = []
      for (let i = 0; i < legCount; i++) {
        const leg = data.flights[i]
        if (leg) {
          const depDt = [leg.departure_date, leg.departure_time].filter(Boolean).join(' ')
          const arrDt = [leg.arrival_date,   leg.arrival_time  ].filter(Boolean).join(' ')
          legCells.push(leg.flight_no, leg.from_airport, leg.to_airport,
            depDt, leg.departure_status, arrDt, leg.arrival_status)
        } else {
          legCells.push('', '', '', '', '', '', '')
        }
      }
      const uldCells = Array.from({ length: maxULD }, (_, i) => ulds?.[i] ?? '')
      ws.addRow([...base, ...legCells, ...uldCells])
    })

    // Download
    const buffer = await wb.xlsx.writeBuffer()
    const blob = new Blob([buffer], { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `cargo-tracking-${new Date().toISOString().slice(0, 10)}.xlsx`
    a.click()
    URL.revokeObjectURL(url)
  }

  async function handleSearch() {
    const validAWBs = all.filter(a => AWB_RE.test(a)).slice(0, MAX_AWBS)
    if (!validAWBs.length) return

    setBusy(true)
    setSearchKey(k => k + 1)
    setRows(validAWBs.map(awb => ({ kind: 'loading', awb })))

    await Promise.all(validAWBs.map(async (awb, idx) => {
      try {
        const data = await trackShipment(awb)
        setRows(prev => {
          const n = [...prev]
          n[idx] = { kind: 'ok', awb: data.awb, data, ulds: null }
          return n
        })
      } catch (e) {
        setRows(prev => {
          const n = [...prev]
          n[idx] = { kind: 'error', awb, message: e instanceof Error ? e.message : String(e) }
          return n
        })
      }
    }))
    setBusy(false)
  }

  const onULDReady = useCallback((awb: string, ulds: ULDEntry[]) => {
    setRows(prev => prev.map(r =>
      r.kind === 'ok' && r.awb === awb ? { ...r, ulds } : r
    ))
  }, [])

  function onKey(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) handleSearch()
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100vh' }}>
      <header className="header">
        <div className="header-title">✈ Air Cargo Tracker</div>
        <div className="header-sub">AGS Logistics</div>
      </header>

      {/* Notes banner */}
      <NotesBanner />

      <div className="search-bar">
        <span className="search-label">MAWB No.</span>
        <div style={{ display: 'flex', flexDirection: 'column', flex: 1, minWidth: 200, maxWidth: 420 }}>
          <textarea
            className={`awb-textarea${invalidAWBs.length ? ' awb-textarea-error' : ''}`}
            placeholder={'695-59554773\n695-60392323'}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={onKey}
            spellCheck={false}
            rows={2}
          />
          {invalidAWBs.length > 0 && (
            <div className="awb-validation-error">
              Invalid format (must be NNN-NNNNNNN):&nbsp;
              {invalidAWBs.map(a => (
                <span key={a} className="awb-invalid-chip">{a}</span>
              ))}
            </div>
          )}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          <button
            className="search-btn"
            onClick={handleSearch}
            disabled={busy || (all.length > 0 && all.filter(a => AWB_RE.test(a)).length === 0)}
          >
            {busy ? 'Searching…' : 'Search'}
          </button>
          <span className="search-hint">One per line · Ctrl+Enter · Max {MAX_AWBS}</span>
        </div>
      </div>

      <main className="main">
        {rows.length === 0 ? (
          <div className="state-msg">
            Enter one or more MAWB numbers above.
            <div className="hint">Multiple AWBs: one per line, format NNN-NNNNNNN</div>
          </div>
        ) : (
          <>
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 6 }}>
              <button className="export-btn" onClick={handleExport}>
                ↓ Export Excel
              </button>
            </div>
            <div className="tbl-wrap">
              <table className="tbl">
                <TableHeader
                  maxCargo={maxCargo}
                  sortCol={sortCol} sortDir={sortDir} onSort={toggleSort}
                  statusWidth={statusWidth} onResizeStart={startResize}
                />
                <tbody>
                  {sortedRows.map((row, i) => (
                    <TableRow
                      key={row.awb + '-' + searchKey + '-' + i}
                      row={row}
                      maxCargo={maxCargo}
                      onULDReady={onULDReady}
                      statusWidth={statusWidth}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </main>
    </div>
  )
}
