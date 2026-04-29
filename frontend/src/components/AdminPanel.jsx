import { useState, useEffect, useCallback } from 'react'

const API = ''

const Section = ({ title, children, accent = 'var(--green)' }) => (
  <div style={{ marginBottom: '20px' }}>
    <div style={{
      fontFamily:    'var(--mono)',
      fontSize:      '9px',
      letterSpacing: '2px',
      color:         accent,
      textTransform: 'uppercase',
      padding:       '6px 0 8px',
      borderBottom:  `1px solid ${accent}33`,
      marginBottom:  '10px',
    }}>
      {title}
    </div>
    {children}
  </div>
)

const StatRow = ({ label, value, accent }) => (
  <div style={{
    display:        'flex',
    justifyContent: 'space-between',
    padding:        '4px 0',
    borderBottom:   '1px solid var(--border-dim)',
  }}>
    <span style={{ fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--text-dim)' }}>
      {label}
    </span>
    <span style={{ fontFamily: 'var(--mono)', fontSize: '10px', color: accent || 'var(--text)' }}>
      {value ?? '—'}
    </span>
  </div>
)

const Btn = ({ children, onClick, color = 'var(--green)', disabled, small }) => (
  <button
    onClick={onClick}
    disabled={disabled}
    style={{
      fontFamily:    'var(--mono)',
      fontSize:      small ? '9px' : '9px',
      letterSpacing: '1px',
      padding:       small ? '3px 8px' : '4px 10px',
      border:        `1px solid ${color}55`,
      borderRadius:  '2px',
      background:    'transparent',
      color:         disabled ? 'var(--text-dim)' : color,
      cursor:        disabled ? 'not-allowed' : 'pointer',
      opacity:       disabled ? 0.5 : 1,
      transition:    'background 0.15s',
      whiteSpace:    'nowrap',
    }}
    onMouseEnter={e => !disabled && (e.target.style.background = `${color}15`)}
    onMouseLeave={e => !disabled && (e.target.style.background = 'transparent')}
  >
    {children}
  </button>
)

const DangerBtn = ({ children, onClick, disabled, small }) => {
  const [confirm, setConfirm] = useState(false)
  const handleClick = () => {
    if (!confirm) { setConfirm(true); setTimeout(() => setConfirm(false), 3000); return }
    setConfirm(false); onClick()
  }
  return (
    <Btn color={confirm ? 'var(--red)' : 'var(--amber)'}
         onClick={handleClick} disabled={disabled} small={small}>
      {confirm ? '⚠ CONFIRM?' : children}
    </Btn>
  )
}

const StatusMsg = ({ msg, isError }) => msg ? (
  <div style={{
    fontFamily:   'var(--mono)',
    fontSize:     '10px',
    color:        isError ? 'var(--red)' : 'var(--green)',
    padding:      '6px 0',
    borderRadius: '2px',
  }}>
    {isError ? '✗ ' : '✓ '}{msg}
  </div>
) : null

export default function AdminPanel({ onClose }) {
  const [overview,      setOverview]      = useState(null)
  const [incidents,     setIncidents]     = useState([])
  const [units,         setUnits]         = useState({})
  const [dbIncidents,   setDbIncidents]   = useState([])
  const [unassociated,  setUnassociated]  = useState([])
  const [loading,       setLoading]       = useState(false)
  const [lastRefresh,   setLastRefresh]   = useState(null)
  const [tab,           setTab]           = useState('overview')
  const [statusMsg,     setStatusMsg]     = useState(null)
  const [statusErr,     setStatusErr]     = useState(false)

  // Merge state
  const [mergeSource,   setMergeSource]   = useState('')
  const [mergeTarget,   setMergeTarget]   = useState('')
  const [merging,       setMerging]       = useState(false)

  const showStatus = (msg, isError = false) => {
    setStatusMsg(msg); setStatusErr(isError)
    setTimeout(() => setStatusMsg(null), 4000)
  }

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [ov, inc, un, dbi, una] = await Promise.all([
        fetch(`${API}/admin/redis/overview`).then(r => r.json()),
        fetch(`${API}/admin/redis/incidents`).then(r => r.json()),
        fetch(`${API}/admin/redis/units`).then(r => r.json()),
        fetch(`${API}/admin/db/incidents?limit=50`).then(r => r.json()),
        fetch(`${API}/admin/db/unassociated?limit=50`).then(r => r.json()),
      ])
      setOverview(ov)
      setIncidents(inc.incidents || [])
      setUnits(un.units || {})
      setDbIncidents(dbi.incidents || [])
      setUnassociated(una.chunks || [])
      setLastRefresh(new Date().toLocaleTimeString())
    } catch (e) {
      showStatus('Refresh failed', true)
    }
    setLoading(false)
  }, [])

  useEffect(() => { refresh() }, [refresh])

  const del = async (url) => {
    await fetch(`${API}${url}`, { method: 'DELETE' })
    await refresh()
  }

  const reprocessChunk = async (chunkId) => {
    showStatus(`Reprocessing ${chunkId.slice(0, 8)}...`)
    try {
      const res  = await fetch(`${API}/admin/reprocess/chunk/${chunkId}`,
                               { method: 'POST' })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Failed')
      showStatus(
        `✓ ${data.correlation_action} — ${data.incident_id
          ? data.incident_id.slice(0, 8)
          : 'unassociated'} (${data.geocode_confidence || 'no geo'})`
      )
      await refresh()
    } catch (e) {
      showStatus(e.message, true)
    }
  }

  const mergeIncidents = async () => {
    if (!mergeSource || !mergeTarget) {
      showStatus('Enter both source and target IDs', true); return
    }
    setMerging(true)
    try {
      const res  = await fetch(`${API}/admin/merge/incidents`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ source_id: mergeSource, target_id: mergeTarget }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Failed')
      showStatus(
        `Merged — ${data.chunks_relinked} chunks relinked, ` +
        `${data.units_reassigned.length} units reassigned`
      )
      setMergeSource(''); setMergeTarget('')
      await refresh()
    } catch (e) {
      showStatus(e.message, true)
    }
    setMerging(false)
  }

  const tabs = ['overview', 'redis incidents', 'units',
                'db incidents', 'unassociated', 'merge', 'queues']

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <span style={styles.title}>// REDIS ADMIN</span>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          {lastRefresh && (
            <span style={{ fontFamily: 'var(--mono)', fontSize: '9px',
                           color: 'var(--text-dim)' }}>
              {lastRefresh}
            </span>
          )}
          <Btn onClick={refresh} disabled={loading}>
            {loading ? 'LOADING...' : '↻ REFRESH'}
          </Btn>
          <DangerBtn onClick={() => del('/admin/reset/all')}>
            ☢ RESET ALL
          </DangerBtn>
          {onClose && (
            <button onClick={onClose} style={{
              fontFamily: 'var(--mono)', fontSize: '10px',
              color: 'var(--text-dim)', background: 'transparent',
              border: '1px solid var(--border)', borderRadius: '2px',
              padding: '3px 10px', cursor: 'pointer', letterSpacing: '1px',
            }}>✕ CLOSE</button>
          )}
        </div>
      </div>

      {statusMsg && (
        <div style={{
          padding:    '6px 14px',
          fontFamily: 'var(--mono)',
          fontSize:   '10px',
          color:      statusErr ? 'var(--red)' : 'var(--green)',
          background: statusErr
            ? 'rgba(255,77,106,0.08)'
            : 'rgba(0,229,160,0.08)',
          borderBottom: '1px solid var(--border)',
        }}>
          {statusErr ? '✗ ' : '✓ '}{statusMsg}
        </div>
      )}

      <div style={styles.tabs}>
        {tabs.map(t => (
          <button key={t} onClick={() => setTab(t)} style={{
            ...styles.tab,
            color:        tab === t ? 'var(--green)' : 'var(--text-dim)',
            borderBottom: tab === t
              ? '2px solid var(--green)'
              : '2px solid transparent',
          }}>
            {t.toUpperCase()}
            {t === 'unassociated' && unassociated.length > 0 && (
              <span style={styles.badge}>{unassociated.length}</span>
            )}
          </button>
        ))}
      </div>

      <div style={styles.body}>

        {/* ── Overview ── */}
        {tab === 'overview' && overview && (
          <div>
            <Section title="Memory & Connections">
              <StatRow label="Used memory"       value={overview.used_memory_human} />
              <StatRow label="Connected clients" value={overview.connected_clients} />
            </Section>
            <Section title="Queues" accent="var(--amber)">
              <StatRow label="Transcription"  value={overview.queues?.transcription} accent="var(--amber)" />
              <StatRow label="Normalization"  value={overview.queues?.normalization}  accent="var(--amber)" />
              <StatRow label="Unassociated"   value={overview.queues?.unassociated}   accent="var(--red)" />
            </Section>
            <Section title="State" accent="var(--blue)">
              <StatRow label="Active units"     value={overview.active_units}     accent="var(--blue)" />
              <StatRow label="Active incidents" value={overview.active_incidents} accent="var(--blue)" />
              <StatRow label="Geocache entries" value={overview.geocache_entries} />
              <StatRow label="Debug history"    value={overview.debug_history} />
            </Section>
            <Section title="Quick Actions" accent="var(--red)">
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                <DangerBtn onClick={() => del('/admin/redis/incidents')}>CLEAR REDIS INCIDENTS</DangerBtn>
                <DangerBtn onClick={() => del('/admin/redis/units')}>CLEAR UNITS</DangerBtn>
                <DangerBtn onClick={() => del('/admin/redis/queues/transcription')}>FLUSH TRANSCRIPTION Q</DangerBtn>
                <DangerBtn onClick={() => del('/admin/redis/queues/normalization')}>FLUSH NORMALIZATION Q</DangerBtn>
                <DangerBtn onClick={() => del('/admin/redis/debug')}>CLEAR DEBUG HISTORY</DangerBtn>
                <DangerBtn onClick={() => del('/admin/db/incidents')}>WIPE DB INCIDENTS</DangerBtn>
              </div>
            </Section>
          </div>
        )}

        {/* ── Redis Incidents ── */}
        {tab === 'redis incidents' && (
          <div>
            <div style={styles.listHeader}>
              <span style={styles.listCount}>{incidents.length} active in Redis</span>
              <div style={{ display: 'flex', gap: '6px' }}>
                <DangerBtn onClick={() => del('/admin/redis/incidents')}>CLEAR ALL</DangerBtn>
              </div>
            </div>
            {incidents.length === 0 && <Empty text="No active incidents in Redis" />}
            {incidents.map(inc => (
              <IncidentRow
                key={inc.incident_id}
                incident={inc}
                onDelete={() => del(`/admin/redis/incidents/${inc.incident_id}`)}
                onCopyId={() => {
                  navigator.clipboard.writeText(inc.incident_id)
                  showStatus(`Copied ${inc.incident_id.slice(0, 8)}...`)
                }}
              />
            ))}
          </div>
        )}

        {/* ── Units ── */}
        {tab === 'units' && (
          <div>
            <div style={styles.listHeader}>
              <span style={styles.listCount}>{Object.keys(units).length} active units</span>
              <DangerBtn onClick={() => del('/admin/redis/units')}>CLEAR ALL</DangerBtn>
            </div>
            {Object.keys(units).length === 0 && <Empty text="No active unit assignments" />}
            {Object.entries(units).map(([unit, incidentId]) => (
              <div key={unit} style={styles.unitRow}>
                <div>
                  <span style={styles.unitId}>{unit}</span>
                  <span style={styles.unitInc}>→ {incidentId.slice(0, 8)}...</span>
                </div>
                <div style={{ display: 'flex', gap: '6px' }}>
                  <Btn small color="var(--text-dim)"
                       onClick={() => {
                         navigator.clipboard.writeText(incidentId)
                         showStatus(`Copied ${incidentId.slice(0, 8)}...`)
                       }}>
                    COPY ID
                  </Btn>
                  <DangerBtn small onClick={() => del(`/admin/redis/units/${unit}`)}>
                    RELEASE
                  </DangerBtn>
                </div>
              </div>
            ))}
          </div>
        )}

        {/* ── DB Incidents ── */}
        {tab === 'db incidents' && (
          <div>
            <div style={styles.listHeader}>
              <span style={styles.listCount}>{dbIncidents.length} in database</span>
              <DangerBtn onClick={() => del('/admin/db/incidents')}>WIPE ALL</DangerBtn>
            </div>
            {dbIncidents.length === 0 && <Empty text="No incidents in database" />}
            {dbIncidents.map(inc => (
              <DbIncidentRow
                key={inc.incident_id}
                incident={inc}
                onDelete={() => del(`/admin/db/incidents/${inc.incident_id}`)}
                onCopyId={() => {
                  navigator.clipboard.writeText(String(inc.incident_id))
                  showStatus(`Copied ${String(inc.incident_id).slice(0, 8)}...`)
                }}
                onSetMergeSource={() => {
                  setMergeSource(String(inc.incident_id))
                  setTab('merge')
                  showStatus(`Source set to ${String(inc.incident_id).slice(0, 8)}...`)
                }}
                onSetMergeTarget={() => {
                  setMergeTarget(String(inc.incident_id))
                  setTab('merge')
                  showStatus(`Target set to ${String(inc.incident_id).slice(0, 8)}...`)
                }}
              />
            ))}
          </div>
        )}

        {/* ── Unassociated chunks ── */}
        {tab === 'unassociated' && (
          <div>
            <div style={styles.listHeader}>
              <span style={styles.listCount}>
                {unassociated.length} unassociated chunks
              </span>
              <Btn onClick={refresh} small>REFRESH</Btn>
            </div>
            {unassociated.length === 0 && (
              <Empty text="No unassociated chunks — great!" />
            )}
            {unassociated.map((chunk, i) => (
              <div key={chunk.chunk_id || i} style={styles.chunkRow}>
                <div style={styles.chunkRowTop}>
                  <span style={styles.chunkFeed}>{chunk.feed_id}</span>
                  <span style={styles.chunkTime}>
                    {chunk.timestamp
                      ? new Date(chunk.timestamp * 1000).toLocaleTimeString()
                      : '—'}
                  </span>
                </div>
                <div style={styles.chunkTranscript}>
                  &ldquo;{(chunk.transcript || chunk.raw_transcript || '').slice(0, 150)}&rdquo;
                </div>
                {chunk.chunk_id && (
                  <div style={styles.chunkId}>{chunk.chunk_id}</div>
                )}
                <div style={{ display: 'flex', gap: '6px', marginTop: '8px' }}>
                  {chunk.chunk_id && (
                    <Btn small color="var(--blue)"
                         onClick={() => reprocessChunk(chunk.chunk_id)}>
                      ↻ REPROCESS
                    </Btn>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* ── Merge incidents ── */}
        {tab === 'merge' && (
          <div>
            <Section title="Merge Incidents">
              <div style={styles.mergeDesc}>
                Merges SOURCE into TARGET. All transcript chunks from the
                source are re-linked to the target. Source is deleted from
                DB and Redis. Units are combined. Target incident is
                broadcast to the live map.
              </div>

              <div style={styles.mergeField}>
                <label style={styles.mergeLabel}>SOURCE incident ID (will be deleted)</label>
                <input
                  style={styles.mergeInput}
                  placeholder="source incident UUID"
                  value={mergeSource}
                  onChange={e => setMergeSource(e.target.value)}
                />
              </div>

              <div style={styles.mergeField}>
                <label style={styles.mergeLabel}>TARGET incident ID (kept, receives chunks)</label>
                <input
                  style={styles.mergeInput}
                  placeholder="target incident UUID"
                  value={mergeTarget}
                  onChange={e => setMergeTarget(e.target.value)}
                />
              </div>

              <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
                <DangerBtn
                  onClick={mergeIncidents}
                  disabled={merging || !mergeSource || !mergeTarget}
                >
                  {merging ? 'MERGING...' : '⇢ MERGE INCIDENTS'}
                </DangerBtn>
                <Btn color="var(--text-dim)"
                     onClick={() => { setMergeSource(''); setMergeTarget('') }}>
                  CLEAR
                </Btn>
              </div>
            </Section>

            <Section title="Reprocess a chunk by ID" accent="var(--blue)">
              <ReprocessById onReprocess={reprocessChunk} showStatus={showStatus} />
            </Section>

            <Section title="Quick tip" accent="var(--text-dim)">
              <div style={styles.mergeDesc}>
                To merge: go to DB INCIDENTS tab, click COPY ID on the
                incident you want to remove (source), then COPY ID on
                the incident you want to keep (target), paste both here.
                Or use the SET AS SOURCE / SET AS TARGET buttons on each
                incident row to populate automatically.
              </div>
            </Section>
          </div>
        )}

        {/* ── Queues ── */}
        {tab === 'queues' && (
          <div>
            {['transcription', 'normalization', 'unassociated'].map(q => (
              <Section key={q} title={`${q} queue`} accent="var(--amber)">
                <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <DangerBtn onClick={() => del(`/admin/redis/queues/${q}`)}>
                    FLUSH
                  </DangerBtn>
                </div>
              </Section>
            ))}
          </div>
        )}

      </div>
    </div>
  )
}

function ReprocessById({ onReprocess, showStatus }) {
  const [chunkId, setChunkId] = useState('')
  return (
    <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
      <input
        style={{ ...styles.mergeInput, flex: 1 }}
        placeholder="chunk UUID"
        value={chunkId}
        onChange={e => setChunkId(e.target.value)}
      />
      <Btn color="var(--blue)"
           disabled={!chunkId}
           onClick={() => { onReprocess(chunkId); setChunkId('') }}>
        ↻ REPROCESS
      </Btn>
    </div>
  )
}

function IncidentRow({ incident, onDelete, onCopyId }) {
  return (
    <div style={styles.incRow}>
      <div style={styles.incTop}>
        <span style={styles.incType}>{incident.incident_type}</span>
        <span style={styles.incStatus}>{incident.status}</span>
        <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto' }}>
          <Btn small color="var(--text-dim)" onClick={onCopyId}>COPY ID</Btn>
          <DangerBtn small onClick={onDelete}>DELETE</DangerBtn>
        </div>
      </div>
      <div style={styles.incAddr}>{incident.address_full || 'No address'}</div>
      <div style={styles.incMeta}>
        <span>{incident.incident_id?.slice(0, 8)}...</span>
        <span>{incident.county}</span>
        <span>{(incident.units || []).join(', ') || 'no units'}</span>
      </div>
    </div>
  )
}

function DbIncidentRow({ incident, onDelete, onCopyId,
                         onSetMergeSource, onSetMergeTarget }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div style={styles.incRow}>
      <div style={styles.incTop}>
        <span style={styles.incType}>{incident.incident_type}</span>
        <span style={{
          ...styles.incStatus,
          color: incident.status === 'ACTIVE'
            ? 'var(--green)' : 'var(--text-dim)',
        }}>
          {incident.status}
        </span>
        <div style={{ display: 'flex', gap: '4px', marginLeft: 'auto',
                      flexWrap: 'wrap' }}>
          <Btn small color="var(--text-dim)"
               onClick={() => setExpanded(e => !e)}>
            {expanded ? 'HIDE' : 'EXPAND'}
          </Btn>
          <Btn small color="var(--text-dim)" onClick={onCopyId}>COPY ID</Btn>
          <Btn small color="var(--amber)" onClick={onSetMergeSource}>SRC</Btn>
          <Btn small color="var(--blue)"  onClick={onSetMergeTarget}>TGT</Btn>
          <DangerBtn small onClick={onDelete}>DELETE</DangerBtn>
        </div>
      </div>
      <div style={styles.incAddr}>{incident.address_full || 'No address'}</div>
      <div style={styles.incMeta}>
        <span>{String(incident.incident_id).slice(0, 8)}...</span>
        <span>{incident.county}</span>
        <span>{String(incident.opened_at || '').slice(11, 19)}</span>
      </div>
      {expanded && (
        <div style={styles.incExpanded}>
          <div style={styles.incExpandedId}>{incident.incident_id}</div>
          {incident.summary && (
            <div style={styles.incExpandedSummary}>{incident.summary}</div>
          )}
        </div>
      )}
    </div>
  )
}

function Empty({ text }) {
  return (
    <div style={{ padding: '20px', textAlign: 'center',
                  fontFamily: 'var(--mono)', fontSize: '11px',
                  color: 'var(--text-dim)' }}>
      {text}
    </div>
  )
}

const styles = {
  container: {
    display: 'flex', flexDirection: 'column',
    height: '100%', background: 'var(--bg2)',
    borderLeft: '1px solid var(--border)',
  },
  header: {
    padding: '10px 14px', borderBottom: '1px solid var(--border)',
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    flexShrink: 0,
  },
  title: {
    fontFamily: 'var(--mono)', fontSize: '11px',
    color: 'var(--amber)', letterSpacing: '1px',
  },
  tabs: {
    display: 'flex', borderBottom: '1px solid var(--border)',
    flexShrink: 0, overflowX: 'auto',
  },
  tab: {
    fontFamily: 'var(--mono)', fontSize: '9px', letterSpacing: '1px',
    padding: '7px 10px', background: 'transparent', border: 'none',
    cursor: 'pointer', whiteSpace: 'nowrap', transition: 'color 0.15s',
    position: 'relative',
  },
  badge: {
    display: 'inline-block', marginLeft: '4px',
    background: 'var(--red)', color: '#fff',
    fontFamily: 'var(--mono)', fontSize: '8px',
    padding: '1px 4px', borderRadius: '8px',
  },
  body: { overflowY: 'auto', flex: 1, padding: '14px' },
  listHeader: {
    display: 'flex', justifyContent: 'space-between',
    alignItems: 'center', marginBottom: '10px',
  },
  listCount: {
    fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--text-dim)',
  },
  incRow: {
    background: 'var(--bg3)', border: '1px solid var(--border-dim)',
    borderRadius: '2px', padding: '8px 10px', marginBottom: '6px',
  },
  incTop: {
    display: 'flex', alignItems: 'center', gap: '6px',
    marginBottom: '4px', flexWrap: 'wrap',
  },
  incType: {
    fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--red)',
  },
  incStatus: {
    fontFamily: 'var(--mono)', fontSize: '9px', color: 'var(--text-dim)',
  },
  incAddr: {
    fontSize: '11px', color: 'var(--text)', marginBottom: '4px',
  },
  incMeta: {
    display: 'flex', gap: '12px',
    fontFamily: 'var(--mono)', fontSize: '9px', color: 'var(--text-dim)',
  },
  incExpanded: {
    marginTop: '8px', paddingTop: '8px',
    borderTop: '1px solid var(--border-dim)',
  },
  incExpandedId: {
    fontFamily: 'var(--mono)', fontSize: '9px',
    color: 'var(--text-dim)', wordBreak: 'break-all', marginBottom: '4px',
  },
  incExpandedSummary: {
    fontSize: '11px', color: 'var(--text)',
    fontStyle: 'italic', lineHeight: 1.5,
  },
  unitRow: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    padding: '6px 0', borderBottom: '1px solid var(--border-dim)',
  },
  unitId: {
    fontFamily: 'var(--mono)', fontSize: '11px',
    color: 'var(--green)', marginRight: '8px',
  },
  unitInc: {
    fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--text-dim)',
  },
  chunkRow: {
    background: 'var(--bg3)', border: '1px solid var(--border-dim)',
    borderRadius: '2px', padding: '8px 10px', marginBottom: '6px',
  },
  chunkRowTop: {
    display: 'flex', justifyContent: 'space-between',
    marginBottom: '5px',
  },
  chunkFeed: {
    fontFamily: 'var(--mono)', fontSize: '9px', color: 'var(--blue)',
  },
  chunkTime: {
    fontFamily: 'var(--mono)', fontSize: '9px', color: 'var(--text-dim)',
  },
  chunkTranscript: {
    fontSize: '11px', color: 'var(--text)',
    fontStyle: 'italic', lineHeight: 1.5,
  },
  chunkId: {
    fontFamily: 'var(--mono)', fontSize: '9px',
    color: 'var(--text-dim)', marginTop: '4px', wordBreak: 'break-all',
  },
  mergeDesc: {
    fontSize: '11px', color: 'var(--text-dim)',
    lineHeight: 1.6, marginBottom: '14px',
  },
  mergeField: { marginBottom: '10px' },
  mergeLabel: {
    display: 'block', fontFamily: 'var(--mono)', fontSize: '9px',
    color: 'var(--text-dim)', letterSpacing: '1px',
    textTransform: 'uppercase', marginBottom: '4px',
  },
  mergeInput: {
    width: '100%', background: 'var(--bg3)',
    border: '1px solid var(--border)', color: 'var(--text)',
    fontFamily: 'var(--mono)', fontSize: '10px',
    padding: '6px 8px', borderRadius: '2px', outline: 'none',
  },
}