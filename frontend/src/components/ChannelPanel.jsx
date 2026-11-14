import { useEffect, useState } from 'react'
import { API } from '../api'

// Monitored SDR channels (config/channels.yaml) with transmissions in the last hour.
// Replaces the old Broadcastify audio player: Pulse no longer relays any third-party audio.
export default function ChannelPanel() {
  const [channels, setChannels] = useState([])
  const [open, setOpen] = useState(false)

  useEffect(() => {
    const load = () => fetch(`${API}/channels`).then(r => r.ok ? r.json() : []).then(setChannels).catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => clearInterval(t)
  }, [])

  const enabled = channels.filter(c => c.enabled)
  const busy = enabled.filter(c => c.transmissions_last_hour > 0).length
  return (
    <div style={s.wrap}>
      <button style={s.head} onClick={() => setOpen(o => !o)}>
        <span>CHANNELS</span>
        <span style={s.count}>{busy}/{enabled.length} active · 1h</span>
      </button>
      {open && (
        <div style={s.list}>
          {enabled.length === 0 && <div style={s.empty}>No channels configured (config/channels.local.yaml)</div>}
          {enabled.map(c => (
            <div key={c.id} style={s.row}>
              <span style={{ ...s.dot, background: c.transmissions_last_hour > 0 ? '#10b981' : '#475569' }} />
              <span style={s.label}>{c.label}</span>
              <span style={s.kind}>{c.is_rail ? 'rail' : c.kind}</span>
              <span style={s.n}>{c.transmissions_last_hour}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const s = {
  wrap: { borderBottom: '1px solid var(--border)', background: 'var(--bg2)' },
  head: { width: '100%', display: 'flex', justifyContent: 'space-between', padding: '10px 14px',
          background: 'transparent', border: 'none', color: 'var(--text)', fontFamily: 'var(--mono)',
          fontSize: '11px', letterSpacing: '0.08em', cursor: 'pointer' },
  count: { color: 'var(--text-faint)' },
  list: { maxHeight: '220px', overflowY: 'auto', padding: '0 10px 8px' },
  empty: { fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--text-faint)', padding: '6px 4px' },
  row: { display: 'flex', alignItems: 'center', gap: '8px', padding: '4px', fontSize: '12px' },
  dot: { width: '7px', height: '7px', borderRadius: '50%', flexShrink: 0 },
  label: { flex: 1, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  kind: { fontFamily: 'var(--mono)', fontSize: '9px', color: 'var(--text-faint)' },
  n: { fontFamily: 'var(--mono)', fontSize: '10px', color: 'var(--text-dim, #94a3b8)', minWidth: '22px', textAlign: 'right' },
}
