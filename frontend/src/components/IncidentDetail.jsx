import { useState, useEffect, useRef } from 'react'
import { formatTime, formatAge, getTagClass, INCIDENT_LABELS, PRIORITY_COLORS } from '../utils/incidents'

const API = ''
const CHUNK_POLL_INTERVAL = 3000   // poll every 3s while ACTIVE

const FEED_LABELS = {
  wayneco_downriver:                'Downriver Public Safety',
  wayneco_detroit_police_fire:      'Detroit Police and Fire',
  wayneco_detroit_police_dispatch:  'Detroit Police Dispatch',
  wayneco_detroit_fire:             'Detroit Fire',
  wayneco_public_safety:            'Wayne County Public Safety',
  wayneco_westland_gardencity:      'Westland-Garden City',
  wayneco_dearborn:                 'Dearborn Police and Fire',
  wayneco_grossepointe:             'Grosse Pointes and Harper Woods',
  wayneco_plymouthnorthville:       'Plymouth-Northville Public Safety',
  wayneco_southwestern:             'Southwestern Wayne County',
  wayneco_detroit_ems:              'Detroit EMS',
  wayneco_romulus:                  'Romulus-Huron Township',
  wayneco_northville_plymouth_city: 'Northville / Plymouth City Fire',
  wayneco_franklin_bingham:         'Franklin-Bingham Fire',
  oaklandco_royaloak_fire:          'Royal Oak Fire',
  washtenaw_metro:                  'Washtenaw Metro',
  washtenaw_livingston:             'Livingston County',
}

const FEED_ACCENT = {
  wayneco:    '#3b82f6',
  oaklandco:  '#10b981',
  washtenaw:  '#f59e0b',
}

function getFeedAccent(feedId) {
  if (!feedId) return 'var(--text-faint)'
  for (const [k, v] of Object.entries(FEED_ACCENT)) {
    if (feedId.startsWith(k)) return v
  }
  return 'var(--text-faint)'
}

function FeedChip({ feedId }) {
  if (!feedId) return null
  const label = FEED_LABELS[feedId] || feedId
  const color = getFeedAccent(feedId)
  return (
    <span style={{
      fontFamily:    'var(--mono)',
      fontSize:      '9px',
      padding:       '2px 6px',
      borderRadius:  '3px',
      border:        `1px solid ${color}33`,
      background:    `${color}0f`,
      color,
      letterSpacing: '0.03px',
      whiteSpace:    'nowrap',
    }}>
      {label}
    </span>
  )
}

function GeoConfBadge({ source, confidence }) {
  if (!confidence || confidence === 'FAILED') return (
    <span style={{ ...s.geoBadge, color: 'var(--text-faint)', background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border)' }}>
      NO GEO
    </span>
  )
  const color = confidence === 'HIGH' ? '#10b981' : confidence === 'MEDIUM' ? '#f59e0b' : '#94a3b8'
  return (
    <span style={{ ...s.geoBadge, color, background: `${color}0f`, border: `1px solid ${color}33` }}>
      {source}/{confidence}
    </span>
  )
}

export default function IncidentDetail({ incident, onClose }) {
  const [chunks,        setChunks]        = useState([])
  const [timeRemaining, setTimeRemaining] = useState(null)
  const pollRef = useRef(null)

  const fetchChunks = (iid) => {
    return fetch(`${API}/incidents/${iid}/chunks`)
      .then(r => r.json())
      .then(data => {
        const sorted = (data.chunks || []).sort(
          (a, b) => new Date(b.timestamp) - new Date(a.timestamp)
        )
        setChunks(sorted)
        return sorted.length
      })
      .catch(() => 0)
  }

  // Fetch immediately when switching incidents, with retry backoff
  // for the NEW case where the chunk DB write races the WebSocket event.
  useEffect(() => {
    if (!incident) return
    setChunks([])

    let attempts = 0
    const tryFetch = () => {
      fetchChunks(incident.incident_id).then(count => {
        attempts++
        // Retry up to 4 times with growing delays if we got 0 chunks
        // (covers the race between incident:new WebSocket and DB write)
        if (count === 0 && attempts < 4) {
          const delay = attempts * 600  // 600ms, 1200ms, 1800ms
          setTimeout(tryFetch, delay)
        }
      })
    }
    tryFetch()

    // Poll continuously while ACTIVE
    if (incident.status === 'ACTIVE') {
      pollRef.current = setInterval(
        () => fetchChunks(incident.incident_id),
        CHUNK_POLL_INTERVAL
      )
    }

    return () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null }
    }
  }, [incident?.incident_id])

  // Refetch chunks immediately whenever last_updated changes —
  // this fires on every incident:update WebSocket event so new chunks
  // appear in real time without waiting for the next poll interval.
  const lastUpdatedRef = useRef(null)
  useEffect(() => {
    if (!incident?.incident_id) return
    const lu = incident.last_updated || incident.opened_at
    if (lu && lu !== lastUpdatedRef.current) {
      lastUpdatedRef.current = lu
      fetchChunks(incident.incident_id)
    }
  }, [incident?.last_updated, incident?.incident_id])

  // Stop polling and do one final fetch when incident resolves
  useEffect(() => {
    if (incident?.status === 'RESOLVED' && pollRef.current) {
      clearInterval(pollRef.current); pollRef.current = null
      if (incident?.incident_id) fetchChunks(incident.incident_id)
    }
  }, [incident?.status])

  useEffect(() => {
    if (!incident || incident.status === 'RESOLVED') { setTimeRemaining(null); return }
    const STALE_MS = 48 * 60 * 60 * 1000
    const calculate = () => {
      const lastUpdated = incident.last_updated
        ? new Date(incident.last_updated) : new Date(incident.opened_at)
      const elapsed   = Date.now() - lastUpdated.getTime()
      const remaining = Math.max(0, STALE_MS - elapsed)
      const pct       = Math.max(0, Math.min(100, (remaining / STALE_MS) * 100))
      const hours     = Math.floor(remaining / 3600000)
      const minutes   = Math.floor((remaining % 3600000) / 60000)
      return { remaining, pct, hours, minutes }
    }
    setTimeRemaining(calculate())
    const timer = setInterval(() => setTimeRemaining(calculate()), 60000)
    return () => clearInterval(timer)
  }, [incident?.incident_id, incident?.last_updated])

  if (!incident) return null

  const type = incident.incident_type || 'UNKNOWN'
  const priorityColor = {
    HIGH: '#ef4444', MEDIUM: '#f59e0b', LOW: '#10b981', UNKNOWN: 'var(--text-faint)'
  }[incident.priority] || 'var(--text-faint)'

  return (
    <div style={s.container}>

      {/* ── Header ── */}
      <div style={s.header}>
        <div style={s.headerLeft}>
          <span className={`tag tag-${getTagClass(type)}`}>
            {INCIDENT_LABELS[type] || type}
          </span>
          <span style={{ ...s.priorityLabel, color: priorityColor }}>
            {incident.priority}
          </span>
        </div>
        <button onClick={onClose} style={s.closeBtn}>
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
            <path d="M2 2L12 12M12 2L2 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
          Close
        </button>
      </div>

      <div style={s.body}>

        {/* ── Location ── */}
        <div style={s.section}>
          <div style={s.sectionLabel}>Location</div>
          <div style={s.locationVal}>
            {incident.address_full || incident.address_raw || 'Unknown location'}
          </div>
          {incident.lat && incident.lng && (
            <div style={s.coords}>
              {incident.lat.toFixed(5)}, {incident.lng.toFixed(5)}
            </div>
          )}
        </div>

        {/* ── Status row ── */}
        <div style={s.metaGrid}>
          <div style={s.metaCell}>
            <span style={s.metaLabel}>Status</span>
            <span style={{
              ...s.metaVal,
              color: incident.status === 'ACTIVE' ? '#10b981' : 'var(--text-dim)',
            }}>
              {incident.status}
            </span>
          </div>
          <div style={s.metaCell}>
            <span style={s.metaLabel}>Opened</span>
            <span style={s.metaVal}>{formatTime(incident.opened_at)}</span>
          </div>
          <div style={s.metaCell}>
            <span style={s.metaLabel}>Updated</span>
            <span style={s.metaVal}>
              {formatAge(incident.last_updated || incident.opened_at)}
            </span>
          </div>
        </div>

        {/* ── Precinct / Battalion ── */}
        {(incident.precinct || incident.battalion) && (
          <div style={s.metaGrid}>
            {incident.precinct && (
              <div style={s.metaCell}>
                <span style={s.metaLabel}>Precinct</span>
                <span style={s.metaVal}>DPD {incident.precinct}</span>
              </div>
            )}
            {incident.battalion && (
              <div style={s.metaCell}>
                <span style={s.metaLabel}>Battalion</span>
                <span style={s.metaVal}>DFD {incident.battalion}</span>
              </div>
            )}
          </div>
        )}

        {/* ── Units ── */}
        {incident.units?.length > 0 && (
          <div style={s.section}>
            <div style={s.sectionLabel}>
              Units ({incident.units.length})
              {incident.units_cleared?.length > 0 && (
                <span style={{ color: 'var(--text-faint)' }}>
                  {' '}— {incident.units_cleared.length} cleared
                </span>
              )}
            </div>
            <div style={s.unitsRow}>
              {incident.units.map(u => {
                const cleared = incident.units_cleared?.includes(u)
                return (
                  <span key={u} className={`unit-chip${cleared ? ' cleared' : ''}`}>
                    {u}
                  </span>
                )
              })}
            </div>
          </div>
        )}

        {/* ── Nearest stations ── */}
        {incident.nearest_stations?.length > 0 && (
          <div style={s.section}>
            <div style={s.sectionLabel}>Nearest Stations</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
              {incident.nearest_stations.map(st => (
                <div key={st.name} style={s.stationRow}>
                  <span style={s.stationName}>{st.name}</span>
                  <span style={s.stationDist}>{st.distance_km}km</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── AI Summary ── */}
        {incident.summary && (
          <div style={s.summaryBox}>
            <div style={s.summaryLabel}>
              <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style={{ opacity: 0.5 }}>
                <circle cx="5" cy="5" r="4" stroke="currentColor" strokeWidth="1"/>
                <path d="M5 3v2.5L6.5 7" stroke="currentColor" strokeWidth="1" strokeLinecap="round"/>
              </svg>
              AI Summary
            </div>
            <div style={s.summaryText}>{incident.summary}</div>
          </div>
        )}

        {/* ── Auto-close countdown ── */}
        {timeRemaining && incident.status !== 'RESOLVED' && (
          <div style={s.countdownBox}>
            <div style={s.countdownRow}>
              <span style={s.countdownLabel}>Auto-close in</span>
              <span style={{
                ...s.countdownVal,
                color: timeRemaining.pct > 50 ? '#10b981'
                     : timeRemaining.pct > 20 ? '#f59e0b' : '#ef4444',
              }}>
                {timeRemaining.remaining === 0
                  ? 'Stale — pending close'
                  : `${timeRemaining.hours}h ${timeRemaining.minutes}m`}
              </span>
            </div>
            <div style={s.progressTrack}>
              <div style={{
                ...s.progressFill,
                width: `${timeRemaining.pct}%`,
                background: timeRemaining.pct > 50 ? '#10b981'
                          : timeRemaining.pct > 20 ? '#f59e0b' : '#ef4444',
              }} />
            </div>
          </div>
        )}

        {/* ── Incident ID ── */}
        <div style={s.section}>
          <div style={s.sectionLabel}>Incident ID</div>
          <div style={s.incidentId}>{incident.incident_id}</div>
        </div>

        {/* ── Transcript history ── */}
        <div style={s.section}>
          <div style={{ ...s.sectionLabel, display: 'flex', alignItems: 'center', gap: '6px' }}>
            <span>Transcript History ({chunks.length})</span>
            {incident.status === 'ACTIVE' && (
              <span style={s.liveBadge}>● LIVE</span>
            )}
          </div>
          <div style={s.chunks}>
            {chunks.length === 0 && (
              <div style={s.noChunks}>No transcript chunks yet</div>
            )}
            {chunks.map(chunk => {
              const actionColor = {
                NEW:     '#10b981',
                UPDATE:  '#3b82f6',
                RESOLVE: 'var(--text-faint)',
              }[chunk.correlation_action] || '#f59e0b'

              return (
                <div key={chunk.chunk_id} style={{ ...s.chunk, borderLeftColor: actionColor }}>
                  <div style={s.chunkHeader}>
                    <div style={s.chunkHeaderLeft}>
                      <span style={{ ...s.chunkAction, color: actionColor }}>
                        {chunk.correlation_action}
                      </span>
                      <span style={s.chunkTime}>{formatTime(chunk.timestamp)}</span>
                    </div>
                  </div>

                  <div style={s.chunkMeta}>
                    <FeedChip feedId={chunk.feed_id || incident.feed_id} />
                    <GeoConfBadge
                      source={chunk.geocode_source}
                      confidence={chunk.geocode_confidence}
                    />
                  </div>

                  <div style={s.chunkText}>
                    {chunk.raw_transcript || '(no transcript)'}
                  </div>

                  {chunk.normalized_address && chunk.normalized_address !== 'NO_LOCATION' && (
                    <div style={s.chunkAddr}>
                      ↳ {chunk.normalized_address}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>

      </div>
    </div>
  )
}

const s = {
  container: {
    display:       'flex',
    flexDirection: 'column',
    height:        '100%',
    background:    'var(--bg2)',
  },
  header: {
    padding:        '11px 14px',
    borderBottom:   '1px solid var(--border)',
    display:        'flex',
    justifyContent: 'space-between',
    alignItems:     'center',
    flexShrink:     0,
    background:     'var(--navy-950)',
  },
  headerLeft: {
    display:    'flex',
    alignItems: 'center',
    gap:        '8px',
  },
  priorityLabel: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    fontWeight:    500,
    letterSpacing: '0.08em',
  },
  closeBtn: {
    display:       'flex',
    alignItems:    'center',
    gap:           '5px',
    fontFamily:    'var(--mono)',
    fontSize:      '10px',
    color:         'var(--text-faint)',
    padding:       '4px 8px',
    borderRadius:  'var(--radius)',
    border:        '1px solid var(--border-md)',
    background:    'transparent',
    cursor:        'pointer',
    transition:    'all 0.12s ease',
  },
  body: {
    overflowY: 'auto',
    flex:      1,
    padding:   '14px',
    display:   'flex',
    flexDirection: 'column',
    gap:       '14px',
  },
  section: {
    display: 'flex',
    flexDirection: 'column',
    gap: '5px',
  },
  sectionLabel: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    fontWeight:    500,
    letterSpacing: '0.12em',
    textTransform: 'uppercase',
    color:         'var(--text-faint)',
  },
  locationVal: {
    fontSize:   '13px',
    fontWeight: 500,
    color:      'var(--text-bright)',
    lineHeight: 1.3,
  },
  coords: {
    fontFamily: 'var(--mono)',
    fontSize:   '10px',
    color:      'var(--text-faint)',
  },
  metaGrid: {
    display: 'flex',
    gap:     '16px',
  },
  metaCell: {
    display:       'flex',
    flexDirection: 'column',
    gap:           '3px',
  },
  metaLabel: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    letterSpacing: '0.1em',
    color:         'var(--text-faint)',
    textTransform: 'uppercase',
  },
  metaVal: {
    fontSize:   '12px',
    fontWeight: 500,
    color:      'var(--text-bright)',
  },
  unitsRow: {
    display:  'flex',
    flexWrap: 'wrap',
    gap:      '4px',
  },
  stationRow: {
    display:        'flex',
    justifyContent: 'space-between',
    alignItems:     'center',
  },
  stationName: {
    fontFamily: 'var(--mono)',
    fontSize:   '10px',
    color:      'var(--text-dim)',
  },
  stationDist: {
    fontFamily: 'var(--mono)',
    fontSize:   '10px',
    color:      'var(--text-faint)',
  },
  summaryBox: {
    background:   'rgba(255,255,255,0.02)',
    border:       '1px solid var(--border)',
    borderRadius: 'var(--radius)',
    padding:      '10px 12px',
  },
  summaryLabel: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    letterSpacing: '0.1em',
    color:         'var(--text-faint)',
    textTransform: 'uppercase',
    marginBottom:  '6px',
    display:       'flex',
    alignItems:    'center',
    gap:           '5px',
  },
  summaryText: {
    fontSize:   '12px',
    color:      'var(--text)',
    lineHeight: 1.6,
    fontStyle:  'italic',
  },
  countdownBox: {
    display:       'flex',
    flexDirection: 'column',
    gap:           '6px',
  },
  countdownRow: {
    display:        'flex',
    justifyContent: 'space-between',
    alignItems:     'center',
  },
  countdownLabel: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    letterSpacing: '0.08em',
    color:         'var(--text-faint)',
    textTransform: 'uppercase',
  },
  countdownVal: {
    fontFamily: 'var(--mono)',
    fontSize:   '10px',
    fontWeight: 500,
  },
  progressTrack: {
    height:       '3px',
    background:   'var(--border)',
    borderRadius: '2px',
    overflow:     'hidden',
  },
  progressFill: {
    height:       '100%',
    borderRadius: '2px',
    transition:   'width 0.5s ease, background 0.3s ease',
  },
  incidentId: {
    fontFamily:  'var(--mono)',
    fontSize:    '9px',
    color:       'var(--text-faint)',
    letterSpacing:'0.03em',
    wordBreak:   'break-all',
  },
  liveBadge: {
    fontFamily:    'var(--mono)',
    fontSize:      '8px',
    color:         '#10b981',
    letterSpacing: '0.06em',
    fontWeight:    500,
  },
  chunks: {
    display:       'flex',
    flexDirection: 'column',
    gap:           '6px',
    marginTop:     '4px',
  },
  noChunks: {
    fontFamily: 'var(--mono)',
    fontSize:   '11px',
    color:      'var(--text-faint)',
    padding:    '8px 0',
  },
  chunk: {
    background:   'var(--bg3)',
    border:       '1px solid var(--border)',
    borderLeft:   '2px solid',
    borderRadius: 'var(--radius)',
    padding:      '8px 10px',
    display:      'flex',
    flexDirection:'column',
    gap:          '5px',
  },
  chunkHeader: {
    display:        'flex',
    justifyContent: 'space-between',
    alignItems:     'center',
  },
  chunkHeaderLeft: {
    display:    'flex',
    alignItems: 'center',
    gap:        '8px',
  },
  chunkAction: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    fontWeight:    500,
    letterSpacing: '0.06em',
  },
  chunkTime: {
    fontFamily: 'var(--mono)',
    fontSize:   '9px',
    color:      'var(--text-faint)',
  },
  chunkMeta: {
    display:    'flex',
    alignItems: 'center',
    gap:        '5px',
    flexWrap:   'wrap',
  },
  geoBadge: {
    fontFamily:    'var(--mono)',
    fontSize:      '8px',
    padding:       '1px 5px',
    borderRadius:  '2px',
    letterSpacing: '0.03em',
  },
  chunkText: {
    fontSize:   '11.5px',
    color:      'var(--text)',
    lineHeight: 1.5,
  },
  chunkAddr: {
    fontFamily: 'var(--mono)',
    fontSize:   '10px',
    color:      'var(--blue)',
  },
}