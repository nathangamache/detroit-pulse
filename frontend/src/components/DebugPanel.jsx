import { useRef, useEffect, useMemo, useState } from 'react'

const MAX_EVENTS = 200

const FEED_SHORT = {
  wayneco_detroit_police_fire:      'DPD/Fire',
  wayneco_detroit_police_dispatch:  'DPD Dispatch',
  wayneco_detroit_fire:             'Detroit Fire',
  wayneco_detroit_ems:              'Detroit EMS',
  wayneco_public_safety:            'Wayne Co.',
  wayneco_plymouthnorthville:       'Plymouth-Northville',
  wayneco_downriver:                'Downriver',
  wayneco_grossepointe:             'Grosse Pointe',
  wayneco_dearborn:                 'Dearborn',
  wayneco_westland_gardencity:      'Westland',
  wayneco_southwestern:             'SW Wayne',
  wayneco_romulus:                  'Romulus',
  wayneco_northville_plymouth_city: 'Northville',
  wayneco_franklin_bingham:         'Franklin',
  oaklandco_royaloak_fire:          'Royal Oak Fire',
  washtenaw_metro:                  'Washtenaw',
  washtenaw_livingston:             'Livingston',
}

function shortFeed(id) {
  if (!id) return ''
  return FEED_SHORT[id] || id.replace(/^wayneco_/, '').replace(/_/g, ' ')
}

function feedColor(id) {
  if (!id) return '#475569'
  if (id.startsWith('wayneco'))   return '#60a5fa'
  if (id.startsWith('oaklandco')) return '#34d399'
  if (id.startsWith('washtenaw')) return '#fbbf24'
  return '#94a3b8'
}

function fmtTs(ts) {
  if (!ts) return ''
  const d = new Date(typeof ts === 'number' ? ts : ts)
  if (isNaN(d)) return ''
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}

const ACTION_COL = {
  NEW:          '#34d399',
  UPDATE:       '#60a5fa',
  RESOLVE:      '#94a3b8',
  UNASSOCIATED: '#475569',
  RETRY:        '#fbbf24',
}
const PRIORITY_COL = {
  HIGH: '#f87171', MEDIUM: '#fbbf24', LOW: '#34d399', UNKNOWN: '#64748b',
}

// ── Stage config: label, color, icon char ────────────────────────────────
const STAGE_CONFIG = {
  ingest:        { label: 'INGEST',    color: '#475569', desc: 'Audio chunk received' },
  transcription: { label: 'WHISPER',  color: '#7c3aed', desc: 'Transcribed by Whisper' },
  normalization: { label: 'NORM',     color: '#2563eb', desc: 'Address extracted by LLM' },
  geocoding:     { label: 'GEO',      color: '#0891b2', desc: 'Geocoded to coordinates' },
  structuring:   { label: 'STRUCT',   color: '#0369a1', desc: 'Incident structured by LLM' },
  unit_validation:{ label: 'UNITS',   color: '#6d28d9', desc: 'Unit location validated' },
  correlation:   { label: 'CORR',     color: '#1d4ed8', desc: 'Correlated to incident' },
}

const INCIDENT_STAGE_CONFIG = {
  new:     { label: 'NEW',     color: '#34d399' },
  update:  { label: 'UPDATE',  color: '#60a5fa' },
  resolve: { label: 'RESOLVE', color: '#94a3b8' },
}

/**
 * Parse any incoming WebSocket event into a normalized display object.
 *
 * Shapes we handle:
 * 1. pipeline:debug  → { event:"pipeline:debug", data:{"stage":"ingest","feed_id":...,...} }
 * 2. debug:normalization → { event:"debug:normalization", feed_id:..., data:{transcript,...}, ts }
 * 3. incident:new/update/resolve → { event:"incident:new", data:{incident fields} }
 */
function parseEvent(raw) {
  const eventType = raw.event || ''
  const ts        = raw.ts || Date.now()

  // ── 1. pipeline:debug (ingest, transcription, geocoding, structuring, etc.) ──
  if (eventType === 'pipeline:debug' || eventType === 'debug:pipeline') {
    const d       = raw.data || {}
    const stage   = d.stage || 'unknown'
    const feed_id = d.feed_id || raw.feed_id || ''
    const cfg     = STAGE_CONFIG[stage] || { label: stage.toUpperCase(), color: '#475569' }

    // Build a human-readable summary and detail fields per stage
    let summary = ''
    let detail  = {}

    if (stage === 'ingest') {
      summary = d.stream_url ? `pulling ${d.stream_url.replace('https://', '').slice(0, 40)}` : d.status || ''
      detail  = { status: d.status, stream_url: d.stream_url, chunk_duration: d.chunk_duration }
    } else if (stage === 'transcription') {
      summary = d.transcript
        ? `"${d.transcript.slice(0, 60)}${d.transcript.length > 60 ? '…' : ''}"`
        : d.status || ''
      detail  = {
        transcript:    d.transcript,
        whisper_model: d.whisper_model,
        duration_s:    d.duration_s,
        elapsed_ms:    d.elapsed_ms,
      }
    } else if (stage === 'normalization') {
      summary = d.normalized && d.normalized !== 'NO_LOCATION'
        ? `→ ${d.normalized}`
        : d.status === 'starting'
          ? `"${(d.transcript||'').slice(0,50)}…"`
          : 'no address'
      detail  = { transcript: d.transcript, normalized: d.normalized, elapsed_ms: d.elapsed_ms }
    } else if (stage === 'geocoding') {
      summary = d.lat && d.lng
        ? `${d.confidence} · ${d.source} · ${d.lat?.toFixed(4)},${d.lng?.toFixed(4)}`
        : d.address || d.status || ''
      detail  = { address: d.address, lat: d.lat, lng: d.lng, confidence: d.confidence, source: d.source, elapsed_ms: d.elapsed_ms }
    } else if (stage === 'structuring') {
      if (d.status === 'complete') {
        summary = d.has_incident
          ? `${d.incident_type || '?'} · ${d.correlation_action || '?'}`
          : 'no incident'
      } else {
        summary = d.status || ''
      }
      detail = {
        has_incident:      d.has_incident,
        incident_type:     d.incident_type,
        correlation_action:d.correlation_action,
        summary:           d.summary,
        elapsed_ms:        d.elapsed_ms,
      }
    } else if (stage === 'correlation') {
      summary = d.action
        ? `${d.action}${d.incident_id ? ` · ${d.incident_id.slice(0,8)}` : ''}`
        : d.status || ''
      detail = { action: d.action, incident_id: d.incident_id, elapsed_ms: d.elapsed_ms }
    } else if (stage === 'unit_validation') {
      summary = d.units?.length
        ? `${d.units.join(', ')} · ${d.valid ? 'valid' : 'invalid'}`
        : d.status || ''
      detail = { units: d.units, valid: d.valid, plausible: d.plausible, distance_km: d.distance_km, inferred: d.inferred }
    } else {
      summary = JSON.stringify(d).slice(0, 80)
      detail  = d
    }

    return {
      kind:      'stage',
      stage,
      cfg,
      feed_id,
      feed_short: shortFeed(feed_id),
      ts,
      summary,
      detail,
    }
  }

  // ── 2. debug:normalization (single-event summary from pipeline_worker_v3) ──
  if (eventType === 'debug:normalization' || eventType.includes(':normalization')) {
    const feed_id  = raw.feed_id || raw.data?.feed_id || ''
    const inner    = raw.data?.data || raw.data || {}
    const structured = inner.structured || {}

    const hasInc    = structured.has_incident
    const action    = structured.correlation_action || null
    const summary   = hasInc
      ? `${structured.incident_type || '?'} · ${action || '?'} · ${inner.geocoded || inner.normalized || 'no address'}`
      : `no incident · ${inner.normalized || 'no address'}`

    return {
      kind:      'normalization',
      stage:     'normalization',
      cfg:       STAGE_CONFIG.normalization,
      feed_id,
      feed_short: shortFeed(feed_id),
      ts,
      summary,
      detail: {
        transcript:    inner.transcript,
        normalized:    inner.normalized,
        geocoded:      inner.geocoded,
        has_incident:  hasInc,
        incident_type: structured.incident_type,
        priority:      structured.priority,
        action,
        units_added:   structured.units_added,
        llm_summary:   structured.summary_update,
        norm_ms:       inner.norm_ms,
        geo_ms:        inner.geo_ms,
        struct_ms:     inner.struct_ms,
        retry:         inner.retry_attempt,
      },
    }
  }

  // ── 3. incident:new / update / resolve ───────────────────────────────────
  if (eventType.startsWith('incident:')) {
    const kind = eventType.replace('incident:', '')
    const d    = raw.data || {}
    const cfg  = INCIDENT_STAGE_CONFIG[kind] || { label: kind.toUpperCase(), color: '#94a3b8' }

    const summary = [
      d.incident_type,
      d.address_full || d.address_raw,
      d.priority !== 'UNKNOWN' ? d.priority : null,
    ].filter(Boolean).join(' · ')

    return {
      kind,
      stage:      'incident',
      cfg,
      feed_id:    d.feed_id || '',
      feed_short: shortFeed(d.feed_id),
      ts,
      summary,
      detail: {
        incident_id:   d.incident_id,
        incident_type: d.incident_type,
        priority:      d.priority,
        address:       d.address_full || d.address_raw,
        units:         d.units,
        summary:       d.summary,
      },
    }
  }

  // ── 4. Fallback ───────────────────────────────────────────────────────────
  const d       = raw.data || {}
  const feed_id = raw.feed_id || d.feed_id || ''
  return {
    kind:       'other',
    stage:      eventType,
    cfg:        { label: eventType.split(':').pop()?.toUpperCase().slice(0, 8) || '?', color: '#334155' },
    feed_id,
    feed_short: shortFeed(feed_id),
    ts,
    summary:    JSON.stringify(d).slice(0, 80),
    detail:     d,
  }
}

// ── Component ─────────────────────────────────────────────────────────────

export default function DebugPanel({ events, onClose }) {
  const [expanded, setExpanded] = useState(null)
  const listRef   = useRef(null)
  const pinBottom = useRef(true)

  const displayEvents = useMemo(() => events.slice(-MAX_EVENTS).map(parseEvent), [events])

  useEffect(() => {
    if (pinBottom.current && listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight
    }
  }, [displayEvents])

  const handleScroll = () => {
    if (!listRef.current) return
    const { scrollTop, scrollHeight, clientHeight } = listRef.current
    pinBottom.current = scrollHeight - scrollTop - clientHeight < 60
  }

  const stats = useMemo(() => {
    const stages   = displayEvents.filter(e => e.kind === 'stage' || e.kind === 'normalization')
    const ingest   = stages.filter(e => e.stage === 'ingest').length
    const whisper  = stages.filter(e => e.stage === 'transcription').length
    const newInc   = displayEvents.filter(e => e.kind === 'new').length
    const updInc   = displayEvents.filter(e => e.kind === 'update').length
    const normEvs  = displayEvents.filter(e => e.stage === 'normalization' || e.stage === 'structuring')
    const avgMs    = normEvs.length
      ? Math.round(normEvs.reduce((a, e) => a + (e.detail?.struct_ms || e.detail?.elapsed_ms || 0), 0) / normEvs.length)
      : null
    return { total: events.length, ingest, whisper, newInc, updInc, avgMs }
  }, [displayEvents, events.length])

  return (
    <div style={s.root}>
      <div style={s.header}>
        <div style={s.hLeft}>
          <span style={s.hTitle}>Pipeline Log</span>
          <span style={s.hCount}>{events.length}{events.length >= MAX_EVENTS ? ` (last ${MAX_EVENTS})` : ''}</span>
        </div>
        <button style={s.closeBtn} onClick={onClose} title="Close">
          <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
            <path d="M1 1L9 9M9 1L1 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
        </button>
      </div>

      {/* Stats strip */}
      <div style={s.statsRow}>
        <StatPill n={stats.ingest}  l="ingest"   />
        <StatPill n={stats.whisper} l="whisper"  />
        <StatPill n={stats.newInc}  l="new"      c="#34d399" />
        <StatPill n={stats.updInc}  l="updates"  c="#60a5fa" />
        {stats.avgMs !== null && <StatPill n={`${stats.avgMs}ms`} l="avg LLM" />}
      </div>

      {/* Log */}
      <div style={s.list} ref={listRef} onScroll={handleScroll}>
        {displayEvents.length === 0 && (
          <div style={s.empty}>
            Waiting for pipeline events…
          </div>
        )}
        {displayEvents.map((ev, i) => (
          <Row
            key={i}
            ev={ev}
            expanded={expanded === i}
            onToggle={() => setExpanded(expanded === i ? null : i)}
          />
        ))}
      </div>
    </div>
  )
}

function StatPill({ n, l, c }) {
  return (
    <div style={s.statPill}>
      <span style={{ ...s.statN, color: c || '#cbd5e1' }}>{n ?? 0}</span>
      <span style={s.statL}>{l}</span>
    </div>
  )
}

function Row({ ev, expanded, onToggle }) {
  const fc       = feedColor(ev.feed_id)
  const isInc    = ['new','update','resolve'].includes(ev.kind)
  const rowStyle = isInc
    ? { ...s.row, borderLeftColor: ev.cfg.color, borderLeftWidth: '2px' }
    : s.row

  return (
    <div style={rowStyle} onClick={onToggle}>
      <div style={s.compact}>
        {/* Stage badge */}
        <span style={{
          ...s.stageBadge,
          color:       ev.cfg.color,
          borderColor: `${ev.cfg.color}35`,
          background:  `${ev.cfg.color}10`,
          minWidth:    '44px',
          textAlign:   'center',
        }}>
          {ev.cfg.label}
        </span>

        {/* Feed chip — only if feed is known */}
        {ev.feed_short && (
          <span style={{ ...s.feedChip, color: fc, borderColor: `${fc}28`, background: `${fc}0c` }}>
            {ev.feed_short}
          </span>
        )}

        {/* Human-readable summary */}
        <span style={s.summary}>
          {ev.summary}
        </span>

        {/* Timestamp */}
        <span style={s.ts}>{fmtTs(ev.ts)}</span>

        {/* Expand chevron */}
        <span style={{ ...s.chevron, transform: expanded ? 'rotate(90deg)' : 'none' }}>›</span>
      </div>

      {expanded && (
        <DetailPanel ev={ev} />
      )}
    </div>
  )
}

function DetailPanel({ ev }) {
  const d = ev.detail || {}

  // Build key-value rows from the detail object, filtering nulls
  const rows = Object.entries(d)
    .filter(([, v]) => v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && v.length === 0))

  if (rows.length === 0) return null

  return (
    <div style={s.detail} onClick={e => e.stopPropagation()}>
      {/* Transcript gets special block treatment */}
      {d.transcript && (
        <div style={s.transcriptBlock}>{d.transcript}</div>
      )}

      <div style={s.kvGrid}>
        {rows
          .filter(([k]) => k !== 'transcript')
          .map(([k, v]) => {
            // Determine value color based on key+value
            let vc = '#94a3b8'
            if (k === 'has_incident') vc = v ? '#34d399' : '#f87171'
            else if (k === 'action' || k === 'correlation_action') vc = ACTION_COL[v] || '#94a3b8'
            else if (k === 'priority') vc = PRIORITY_COL[v] || '#94a3b8'
            else if (k === 'valid' || k === 'plausible') vc = v ? '#34d399' : '#f87171'
            else if (k === 'confidence') vc = v === 'HIGH' ? '#34d399' : v === 'MEDIUM' ? '#fbbf24' : '#f87171'
            else if (k === 'normalized' && v === 'NO_LOCATION') vc = '#475569'
            else if (k.endsWith('_ms') || k.endsWith('_s')) vc = '#7c3aed'
            else if (k === 'llm_summary' || k === 'summary') vc = '#94a3b8'

            const displayV = Array.isArray(v)
              ? v.join(', ')
              : typeof v === 'boolean'
                ? String(v)
                : String(v)

            const isLong = displayV.length > 60

            return (
              <div key={k} style={{ ...s.kvRow, ...(isLong ? { flexDirection: 'column', gap: '2px' } : {}) }}>
                <span style={s.kvKey}>{k.replace(/_/g, ' ')}</span>
                <span style={{ ...s.kvVal, color: vc, ...(k === 'llm_summary' || k === 'summary' ? { fontStyle: 'italic' } : {}) }}>
                  {isLong ? displayV.slice(0, 180) + (displayV.length > 180 ? '…' : '') : displayV}
                </span>
              </div>
            )
          })}
      </div>
    </div>
  )
}

// ── Styles ─────────────────────────────────────────────────────────────────

const s = {
  root: {
    display: 'flex', flexDirection: 'column',
    height: '100%', background: 'var(--bg2)',
  },

  header: {
    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
    padding: '10px 13px',
    borderBottom: '1px solid rgba(255,255,255,0.07)',
    flexShrink: 0,
    background: 'var(--navy-950)',
  },
  hLeft:   { display: 'flex', alignItems: 'center', gap: '8px' },
  hTitle:  { fontFamily: 'var(--cond)', fontSize: '13px', fontWeight: 600, letterSpacing: '0.04em', color: '#f1f5f9' },
  hCount:  { fontFamily: 'var(--mono)', fontSize: '10px', color: '#475569' },
  closeBtn:{ color: '#475569', padding: '3px', border: 'none', background: 'transparent', cursor: 'pointer', display: 'flex', alignItems: 'center' },

  statsRow: {
    display: 'flex', alignItems: 'center', gap: '0',
    padding: '6px 13px',
    borderBottom: '1px solid rgba(255,255,255,0.05)',
    flexShrink: 0,
    background: 'rgba(0,0,0,0.12)',
  },
  statPill: { display: 'flex', alignItems: 'baseline', gap: '4px', marginRight: '14px' },
  statN:    { fontFamily: 'var(--mono)', fontSize: '13px', fontWeight: 500, color: '#cbd5e1' },
  statL:    { fontFamily: 'var(--mono)', fontSize: '9px', color: '#475569' },

  list:  { flex: 1, overflowY: 'auto', minHeight: 0 },
  empty: {
    fontFamily: 'var(--mono)', fontSize: '10px', color: '#475569',
    padding: '24px 16px', textAlign: 'center',
  },

  row: {
    borderBottom: '1px solid rgba(255,255,255,0.04)',
    borderLeft: '2px solid transparent',
    cursor: 'pointer',
  },
  compact: {
    display: 'flex', alignItems: 'center',
    padding: '5px 10px 5px 10px',
    gap: '6px', minHeight: '28px',
  },

  // Stage badge — fixed width so summaries align
  stageBadge: {
    fontFamily: 'var(--mono)', fontSize: '8px', fontWeight: 600,
    letterSpacing: '0.06em',
    padding: '2px 5px', borderRadius: '2px',
    border: '1px solid', flexShrink: 0,
  },

  feedChip: {
    fontFamily: 'var(--mono)', fontSize: '8px',
    padding: '1px 5px', borderRadius: '2px',
    border: '1px solid', whiteSpace: 'nowrap', flexShrink: 0,
  },

  summary: {
    fontFamily: 'var(--mono)', fontSize: '9px', color: '#64748b',
    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
    flex: 1, minWidth: 0,
  },

  ts: {
    fontFamily: 'var(--mono)', fontSize: '8px', color: '#2d3f55',
    whiteSpace: 'nowrap', flexShrink: 0,
  },
  chevron: {
    color: '#2d3f55', fontSize: '12px', lineHeight: 1,
    transition: 'transform 0.12s', display: 'inline-block', flexShrink: 0,
  },

  // Detail panel
  detail: {
    padding: '7px 12px 9px 12px',
    background: 'rgba(0,0,0,0.20)',
    borderTop: '1px solid rgba(255,255,255,0.04)',
    display: 'flex', flexDirection: 'column', gap: '6px',
  },

  transcriptBlock: {
    fontFamily: 'var(--mono)', fontSize: '10px', color: '#94a3b8',
    lineHeight: 1.6,
    padding: '6px 8px',
    background: 'rgba(255,255,255,0.03)',
    borderRadius: '3px',
    border: '1px solid rgba(255,255,255,0.06)',
    whiteSpace: 'pre-wrap', wordBreak: 'break-word',
  },

  kvGrid: {
    display: 'flex', flexDirection: 'column', gap: '3px',
  },
  kvRow: {
    display: 'flex', alignItems: 'baseline', gap: '8px',
  },
  kvKey: {
    fontFamily: 'var(--mono)', fontSize: '8px', color: '#3d5166',
    width: '90px', flexShrink: 0, letterSpacing: '0.02em',
  },
  kvVal: {
    fontFamily: 'var(--mono)', fontSize: '9px', color: '#94a3b8',
    flex: 1, wordBreak: 'break-word',
  },
}