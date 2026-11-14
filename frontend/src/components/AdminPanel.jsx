import { useState, useEffect, useCallback } from 'react'

import { API, adminHeaders } from '../api'

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
  // Admin is only reachable on the box itself (the gateway blocks /admin) and only when
  // PULSE_ADMIN_TOKEN is set on the server. The token typed here is kept for this tab only.
  const [token, setToken] = useState(() => { try { return sessionStorage.getItem('pulseAdminToken') || '' } catch { return '' } })
  const [overview, setOverview] = useState(null)
  const [usage, setUsage] = useState(null)
  const [errors, setErrors] = useState([])
  const [tab, setTab] = useState('overview')
  const [statusMsg, setStatusMsg] = useState(null)
  const [statusErr, setStatusErr] = useState(false)
  const [mergeSrc, setMergeSrc] = useState('')
  const [mergeTgt, setMergeTgt] = useState('')
  const [incId, setIncId] = useState('')

  const showStatus = (msg, isErr = false) => {
    setStatusMsg(msg); setStatusErr(isErr)
    setTimeout(() => setStatusMsg(null), 4000)
  }

  const call = useCallback(async (method, path, body) => {
    const res = await fetch(`${API}${path}`, {
      method,
      headers: { ...adminHeaders(), ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      const why = res.status === 404 ? 'admin disabled on the server (PULSE_ADMIN_TOKEN unset) or not found'
        : res.status === 403 ? 'wrong admin token' : (data?.error?.message || `HTTP ${res.status}`)
      throw new Error(why)
    }
    return data
  }, [])

  const refresh = useCallback(async () => {
    try {
      const [ov, us, er] = await Promise.all([
        call('GET', '/admin/overview'), call('GET', '/admin/llm/usage?days=30'),
        call('GET', '/admin/transmissions/errors?limit=50'),
      ])
      setOverview(ov); setUsage(us); setErrors(er)
    } catch (e) { showStatus(e.message, true) }
  }, [call])

  useEffect(() => {
    const t = setTimeout(() => { if (token) refresh() }, 0)
    return () => clearTimeout(t)
  }, [token, refresh])

  const saveToken = (v) => {
    setToken(v)
    try { sessionStorage.setItem('pulseAdminToken', v) } catch { /* storage blocked */ }
  }

  const act = async (fn, ok) => {
    try { await fn(); showStatus(ok); refresh() } catch (e) { showStatus(e.message, true) }
  }

  const llm = overview?.llm
  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <span style={styles.title}>ADMIN</span>
        <Btn onClick={refresh} small>REFRESH</Btn>
        <Btn onClick={onClose} small color="var(--text-dim)">CLOSE</Btn>
      </div>
      <div style={styles.tabs}>
        {['overview', 'cost', 'errors', 'actions'].map(t => (
          <button key={t} style={{ ...styles.tab, ...(tab === t ? { color: 'var(--green)' } : {}) }}
                  onClick={() => setTab(t)}>{t.toUpperCase()}</button>
        ))}
      </div>
      <div style={styles.body}>
        <Section title="Admin token">
          <input type="password" value={token} placeholder="X-Pulse-Admin-Token"
                 onChange={e => saveToken(e.target.value)} style={styles.mergeInput} />
        </Section>
        <StatusMsg msg={statusMsg} isError={statusErr} />

        {tab === 'overview' && overview && (
          <>
            <Section title="Queue">
              {Object.entries(overview.queue || {}).map(([k, v]) => <StatRow key={k} label={k} value={v} />)}
            </Section>
            <Section title="Incidents">
              {Object.entries(overview.incidents || {}).map(([k, v]) => <StatRow key={k} label={k} value={v} />)}
            </Section>
            <Section title="Extraction source (24h)">
              {Object.entries(overview.extract_sources_24h || {}).map(([k, v]) => <StatRow key={k} label={k} value={v} />)}
            </Section>
            <Section title="Recent pipeline events" accent="var(--amber)">
              {(overview.recent_events || []).length === 0 && <Empty text="none" />}
              {(overview.recent_events || []).map((e, i) => (
                <div key={i} style={styles.chunkRow}>
                  <div style={styles.chunkRowTop}><span style={styles.chunkFeed}>{e.kind}</span>
                    <span style={styles.chunkTime}>{e.ts}</span></div>
                  <div style={styles.chunkTranscript}>{JSON.stringify(e.detail)}</div>
                </div>
              ))}
            </Section>
          </>
        )}

        {tab === 'cost' && llm && (
          <>
            <Section title="Claude spend (UTC)">
              <StatRow label="today" value={`$${llm.spent_today_usd.toFixed(4)} / $${llm.daily_cap_usd}`} />
              <StatRow label="this month" value={`$${llm.spent_month_usd.toFixed(4)} / $${llm.monthly_cap_usd}`} />
              <StatRow label="degraded to rules" value={llm.degraded || 'no'}
                       accent={llm.degraded ? 'var(--red)' : undefined} />
              <StatRow label="price table" value={llm.price_version} />
            </Section>
            <Section title="Daily usage">
              {(usage?.daily || []).length === 0 && <Empty text="no calls yet" />}
              {(usage?.daily || []).map((d, i) => (
                <StatRow key={i} label={`${String(d.day).slice(0, 10)} ${d.workload}`}
                         value={`${d.calls} calls · ${d.items} items · $${Number(d.cost_usd).toFixed(4)}`} />
              ))}
            </Section>
          </>
        )}

        {tab === 'errors' && (
          <Section title="Failed transmissions" accent="var(--red)">
            {errors.length === 0 && <Empty text="no errors" />}
            {errors.map(e => (
              <div key={e.chunk_id} style={styles.chunkRow}>
                <div style={styles.chunkRowTop}>
                  <span style={styles.chunkFeed}>{e.channel_id}</span>
                  <span style={styles.chunkTime}>{e.captured_at}</span>
                </div>
                <div style={styles.chunkTranscript}>{e.error}</div>
                <Btn small onClick={() => act(() => call('POST', `/admin/transmissions/${e.chunk_id}/requeue?stage=transcribe`), 'requeued')}>REQUEUE</Btn>
              </div>
            ))}
          </Section>
        )}

        {tab === 'actions' && (
          <>
            <Section title="Incident">
              <input value={incId} placeholder="incident id" onChange={e => setIncId(e.target.value.trim())}
                     style={styles.mergeInput} />
              <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
                <Btn disabled={!incId} onClick={() => act(() => call('POST', `/admin/incidents/${incId}/resolve`), 'resolved')}>RESOLVE</Btn>
                <DangerBtn disabled={!incId} onClick={() => act(() => call('DELETE', `/admin/incidents/${incId}`), 'deleted')}>DELETE</DangerBtn>
              </div>
            </Section>
            <Section title="Merge incidents">
              <div style={styles.mergeDesc}>Moves every transmission of the source into the target, then deletes the source.</div>
              <div style={styles.mergeField}><div style={styles.mergeLabel}>SOURCE</div>
                <input value={mergeSrc} onChange={e => setMergeSrc(e.target.value.trim())} style={styles.mergeInput} /></div>
              <div style={styles.mergeField}><div style={styles.mergeLabel}>TARGET</div>
                <input value={mergeTgt} onChange={e => setMergeTgt(e.target.value.trim())} style={styles.mergeInput} /></div>
              <Btn disabled={!mergeSrc || !mergeTgt}
                   onClick={() => act(() => call('POST', '/admin/incidents/merge', { source_id: mergeSrc, target_id: mergeTgt }), 'merged')}>MERGE</Btn>
            </Section>
            <Section title="Danger zone" accent="var(--red)">
              <div style={styles.mergeDesc}>Wipes live incidents, transmissions and rail data. The LLM cost ledger is kept.</div>
              <DangerBtn onClick={() => act(() => call('DELETE', '/admin/reset/all?confirm=RESET-LIVE-DATA'), 'reset done')}>RESET LIVE DATA</DangerBtn>
            </Section>
          </>
        )}
      </div>
    </div>
  )
}

function Empty({ text }) {
  return <div style={{ fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--text-faint)', padding: '6px 0' }}>{text}</div>
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