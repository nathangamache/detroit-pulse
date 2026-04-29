import { useState, useMemo } from 'react'
import { formatAge, getTagClass, INCIDENT_LABELS, INCIDENT_COLORS } from '../utils/incidents'

const PRIORITY_ORDER = { HIGH: 0, MEDIUM: 1, LOW: 2, UNKNOWN: 3 }
const PRIORITY_COLOR = {
  HIGH: '#ef4444', MEDIUM: '#f59e0b', LOW: '#10b981', UNKNOWN: '#475569',
}
export default function IncidentFeed({ incidents, selectedId, onSelect, filterType = 'ALL', onFilterChange }) {
  const [sortBy,     setSortBy]    = useState('updated')
  const [filterOpen, setFilterOpen]= useState(false)

  const setFilterType = (t) => {
    onFilterChange?.(t)
    setFilterOpen(false)
  }

  const active   = incidents.filter(i => i.status === 'ACTIVE')
  const resolved = incidents.filter(i => i.status !== 'ACTIVE').slice(0, 3)

  const types = useMemo(() => {
    const seen = new Set()
    const out  = ['ALL']
    active.forEach(i => {
      const t = i.incident_type || 'UNKNOWN'
      if (!seen.has(t)) { seen.add(t); out.push(t) }
    })
    return out
  }, [active])

  const sorted = useMemo(() => {
    let list = filterType === 'ALL'
      ? [...active]
      : active.filter(i => (i.incident_type || 'UNKNOWN') === filterType)
    switch (sortBy) {
      case 'newest':   return list.sort((a,b)=> new Date(b.opened_at)-new Date(a.opened_at))
      case 'updates':  return list.sort((a,b)=> (b.chunk_count||0)-(a.chunk_count||0))
      case 'priority': return list.sort((a,b)=> (PRIORITY_ORDER[a.priority]??3)-(PRIORITY_ORDER[b.priority]??3))
      default:         return list.sort((a,b)=> new Date(b.last_updated||b.opened_at)-new Date(a.last_updated||a.opened_at))
    }
  }, [active, sortBy, filterType])

  const activeLabel = filterType === 'ALL' ? null : (INCIDENT_LABELS[filterType] || filterType)

  return (
    <div style={s.root}>
      <div style={s.head}>
        <div style={s.title}>
          <span style={s.dot} />
          Live Incidents
        </div>
        <span style={s.badge}>{active.length}</span>
      </div>

      <div style={s.sortBar}>
        {[{k:'updated',l:'Recent'},{k:'newest',l:'Newest'},{k:'priority',l:'Priority'},{k:'updates',l:'Activity'}].map(o => (
          <button key={o.k} style={{...s.sBtn,...(sortBy===o.k?s.sBtnOn:{})}} onClick={()=>setSortBy(o.k)}>{o.l}</button>
        ))}
        <button
          style={{...s.fToggle,...(filterOpen?s.fToggleOn:{}),...(activeLabel?s.fToggleActive:{})}}
          onClick={()=>setFilterOpen(v=>!v)}
        >
          <svg width="10" height="8" viewBox="0 0 10 8" fill="none">
            <path d="M0 1h10M2 4h6M4 7h2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
          </svg>
          {activeLabel ? activeLabel : 'Filter'}
        </button>
      </div>

      {filterOpen && (
        <div style={s.fPanel}>
          {types.map(t => {
            const lbl = t==='ALL' ? 'All' : (INCIDENT_LABELS[t]||t)
            return (
              <button key={t} style={{...s.fChip,...(filterType===t?s.fChipOn:{})}}
                onClick={()=>setFilterType(t)}>
                {lbl}
              </button>
            )
          })}
        </div>
      )}

      <div style={s.list}>
        {sorted.length === 0 && (
          <div style={s.empty}>
            <div style={{fontSize:18,opacity:0.25,marginBottom:4}}>◎</div>
            <div style={s.emptyTxt}>No active incidents</div>
          </div>
        )}
        {sorted.map(inc => (
          <Card key={inc.incident_id} inc={inc}
            selected={inc.incident_id===selectedId}
            onClick={()=>onSelect(inc)} />
        ))}
        {resolved.length > 0 && <>
          <div style={s.resHead}>Recently Resolved</div>
          {resolved.map(inc => (
            <Card key={inc.incident_id} inc={inc}
              selected={inc.incident_id===selectedId}
              onClick={()=>onSelect(inc)} dim />
          ))}
        </>}
      </div>
    </div>
  )
}

function Card({ inc, selected, onClick, dim }) {
  const type    = inc.incident_type || 'UNKNOWN'
  const tagCls  = getTagClass(type)
  const label   = INCIDENT_LABELS[type] || type
  const age     = formatAge(inc.last_updated || inc.opened_at)
  const addr    = inc.address_full || inc.address_raw || 'Location unknown'
  const priCol  = PRIORITY_COLOR[inc.priority] || PRIORITY_COLOR.UNKNOWN
  const barCol  = INCIDENT_COLORS[type] || INCIDENT_COLORS.UNKNOWN

  return (
    <button style={{...s.card,...(selected?s.cardOn:{}),...(dim?s.cardDim:{})}} onClick={onClick}>
      <div style={{...s.bar, background: barCol}} />
      <div style={s.body}>
        <div style={s.top}>
          <span className={`tag tag-${tagCls}`}>{label}</span>
          <span style={s.age}>{age}</span>
        </div>
        <div style={s.addr}>{addr.length>46 ? addr.slice(0,46)+'…' : addr}</div>
        {inc.summary && !dim && (
          <div style={s.summary}>{inc.summary.length>80 ? inc.summary.slice(0,80)+'…' : inc.summary}</div>
        )}
        <div style={s.foot}>
          <span style={{...s.pri, color: priCol}}>{inc.priority}</span>
          {inc.units?.length > 0 && <span style={s.meta}>{inc.units.length} units</span>}
          {inc.chunk_count > 1 && <span style={s.meta}>{inc.chunk_count} updates</span>}
        </div>
      </div>
    </button>
  )
}

const s = {
  root: { display:'flex', flexDirection:'column', flex:1, minHeight:0, overflow:'hidden' },
  head: { display:'flex', alignItems:'center', justifyContent:'space-between', padding:'10px 14px 7px', flexShrink:0 },
  title: { display:'flex', alignItems:'center', gap:'8px', fontFamily:'var(--cond)', fontSize:'13px', fontWeight:600, color:'#f1f5f9', letterSpacing:'0.03em' },
  dot: { width:'7px', height:'7px', borderRadius:'50%', background:'#10b981', boxShadow:'0 0 0 3px rgba(16,185,129,0.18)', flexShrink:0 },
  badge: { fontFamily:'var(--mono)', fontSize:'11px', fontWeight:500, color:'#f1f5f9', background:'rgba(255,255,255,0.06)', border:'1px solid rgba(255,255,255,0.11)', padding:'1px 7px', borderRadius:'3px' },

  sortBar: { display:'flex', alignItems:'center', gap:'2px', padding:'0 10px 7px', flexShrink:0 },
  sBtn: { fontFamily:'var(--mono)', fontSize:'9px', letterSpacing:'0.04em', color:'#475569', padding:'3px 6px', borderRadius:'3px', border:'1px solid transparent', background:'transparent', cursor:'pointer', whiteSpace:'nowrap' },
  sBtnOn: { color:'#cbd5e1', background:'rgba(255,255,255,0.06)', border:'1px solid rgba(255,255,255,0.10)' },
  fToggle: { marginLeft:'auto', fontFamily:'var(--mono)', fontSize:'9px', color:'#475569', padding:'3px 7px', borderRadius:'3px', border:'1px solid rgba(255,255,255,0.07)', background:'rgba(255,255,255,0.03)', cursor:'pointer', display:'flex', alignItems:'center', gap:'4px', whiteSpace:'nowrap' },
  fToggleOn: { color:'#3b82f6', background:'rgba(59,130,246,0.08)', border:'1px solid rgba(59,130,246,0.22)' },
  fToggleActive: { color:'#3b82f6', background:'rgba(59,130,246,0.08)', border:'1px solid rgba(59,130,246,0.22)' },

  fPanel: { padding:'6px 10px 8px', display:'flex', flexWrap:'wrap', gap:'4px', flexShrink:0, borderBottom:'1px solid rgba(255,255,255,0.06)', background:'rgba(0,0,0,0.12)' },
  fChip: { fontFamily:'var(--mono)', fontSize:'9px', padding:'2px 7px', borderRadius:'2px', border:'1px solid rgba(255,255,255,0.07)', background:'transparent', color:'#64748b', cursor:'pointer' },
  fChipOn: { color:'#3b82f6', background:'rgba(59,130,246,0.08)', border:'1px solid rgba(59,130,246,0.28)' },

  list: { overflowY:'auto', flex:1, minHeight:0, padding:'4px 8px 8px', display:'flex', flexDirection:'column', gap:'4px' },
  empty: { display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center', padding:'28px 0' },
  emptyTxt: { fontFamily:'var(--mono)', fontSize:'11px', color:'#475569' },

  card: { display:'flex', width:'100%', background:'rgba(17,30,46,0.85)', border:'1px solid rgba(255,255,255,0.07)', borderRadius:'7px', cursor:'pointer', overflow:'hidden', transition:'all 0.12s', textAlign:'left', flexShrink:0 },
  cardOn: { background:'rgba(59,130,246,0.07)', border:'1px solid rgba(59,130,246,0.32)' },
  cardDim: { opacity:0.40 },
  bar: { width:'3px', flexShrink:0 },
  body: { flex:1, padding:'8px 10px 7px', minWidth:0, display:'flex', flexDirection:'column', gap:'3px' },
  top: { display:'flex', alignItems:'center', justifyContent:'space-between', gap:'6px' },
  age: { fontFamily:'var(--mono)', fontSize:'9px', color:'#475569', flexShrink:0, whiteSpace:'nowrap' },
  addr: { fontSize:'12px', fontWeight:500, color:'#f1f5f9', lineHeight:1.3, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' },
  summary: { fontSize:'11px', color:'#94a3b8', lineHeight:1.35 },
  foot: { display:'flex', alignItems:'center', gap:'8px' },
  pri: { fontFamily:'var(--mono)', fontSize:'9px', fontWeight:500, letterSpacing:'0.05em' },
  meta: { fontFamily:'var(--mono)', fontSize:'9px', color:'#475569' },
  resHead: { fontFamily:'var(--mono)', fontSize:'9px', letterSpacing:'0.10em', color:'#475569', textTransform:'uppercase', padding:'8px 4px 4px', borderTop:'1px solid rgba(255,255,255,0.05)' },
}