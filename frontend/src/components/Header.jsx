export default function Header({
  connected, incidentCount, showDebug, showAdmin, onSetRightPanel
}) {
  return (
    <header style={s.root}>

      {/* Brand */}
      <div style={s.brand}>
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <rect x="1" y="1" width="18" height="18" rx="3" fill="#111e2e" stroke="#1e3a5f" strokeWidth="1"/>
          <circle cx="10" cy="10" r="5.5" stroke="#3b82f6" strokeWidth="1.5"/>
          <circle cx="10" cy="10" r="1.5" fill="#3b82f6"/>
          <line x1="10" y1="4.5" x2="10" y2="2" stroke="#3b82f6" strokeWidth="1.5" strokeLinecap="round"/>
          <line x1="10" y1="18" x2="10" y2="15.5" stroke="#3b82f6" strokeWidth="1.5" strokeLinecap="round"/>
          <line x1="15.5" y1="10" x2="18" y2="10" stroke="#3b82f6" strokeWidth="1.5" strokeLinecap="round"/>
          <line x1="2" y1="10" x2="4.5" y2="10" stroke="#3b82f6" strokeWidth="1.5" strokeLinecap="round"/>
        </svg>
        <div style={s.brandText}>
          <span style={s.brandName}>Detroit Pulse</span>
          <span style={s.brandSub}>Public Safety Intelligence</span>
        </div>
      </div>

      {/* Nav */}
      <nav style={s.nav}>
        <NavBtn active={showDebug} onClick={() => onSetRightPanel('debug')}>
          Pipeline
        </NavBtn>
        <NavBtn active={showAdmin} onClick={() => onSetRightPanel('admin')}>
          Admin
        </NavBtn>
      </nav>

      {/* Status */}
      <div style={s.status}>
        <div style={s.activeBlock}>
          <span style={s.activeNum}>{incidentCount}</span>
          <span style={s.activeLabel}>active</span>
        </div>
        <div style={s.sep} />
        <div style={s.liveBlock}>
          <span style={{
            ...s.liveDot,
            background:  connected ? '#10b981' : '#ef4444',
            boxShadow:   connected ? '0 0 0 3px rgba(16,185,129,0.18)' : 'none',
          }} />
          <span style={{
            ...s.liveLabel,
            color: connected ? '#10b981' : '#64748b',
          }}>
            {connected ? 'Live' : 'Offline'}
          </span>
        </div>
      </div>

    </header>
  )
}

function NavBtn({ active, onClick, children }) {
  return (
    <button
      style={{
        ...s.navBtn,
        ...(active ? s.navBtnActive : {}),
      }}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

const s = {
  root: {
    height:         '46px',
    display:        'flex',
    alignItems:     'center',
    justifyContent: 'space-between',
    padding:        '0 16px',
    background:     '#080d14',
    borderBottom:   '1px solid rgba(255,255,255,0.07)',
    flexShrink:     0,
    zIndex:         100,
  },

  brand: {
    display:    'flex',
    alignItems: 'center',
    gap:        '10px',
    flexShrink: 0,
  },
  brandText: {
    display:       'flex',
    flexDirection: 'column',
    gap:           '1px',
  },
  brandName: {
    fontFamily:    "'Barlow', sans-serif",
    fontSize:      '14px',
    fontWeight:    600,
    color:         '#f1f5f9',
    lineHeight:    1,
    letterSpacing: '0.01em',
  },
  brandSub: {
    fontFamily:    "'DM Mono', monospace",
    fontSize:      '9px',
    color:         '#334155',
    letterSpacing: '0.04em',
    lineHeight:    1,
  },

  nav: {
    display:    'flex',
    alignItems: 'center',
    gap:        '2px',
  },
  navBtn: {
    fontFamily:    "'Barlow', sans-serif",
    fontSize:      '13px',
    fontWeight:    500,
    color:         '#475569',
    padding:       '4px 12px',
    borderRadius:  '5px',
    border:        '1px solid transparent',
    background:    'transparent',
    cursor:        'pointer',
    transition:    'all 0.12s ease',
    letterSpacing: '0',
  },
  navBtnActive: {
    color:      '#f1f5f9',
    background: 'rgba(255,255,255,0.06)',
    border:     '1px solid rgba(255,255,255,0.10)',
  },

  status: {
    display:    'flex',
    alignItems: 'center',
    gap:        '12px',
    flexShrink: 0,
  },
  activeBlock: {
    display:    'flex',
    alignItems: 'baseline',
    gap:        '4px',
  },
  activeNum: {
    fontFamily: "'Barlow', sans-serif",
    fontSize:   '20px',
    fontWeight: 600,
    color:      '#f1f5f9',
    lineHeight: 1,
  },
  activeLabel: {
    fontFamily: "'DM Mono', monospace",
    fontSize:   '9px',
    color:      '#475569',
    letterSpacing: '0.05em',
  },
  sep: {
    width:      '1px',
    height:     '18px',
    background: 'rgba(255,255,255,0.08)',
  },
  liveBlock: {
    display:    'flex',
    alignItems: 'center',
    gap:        '6px',
  },
  liveDot: {
    width:        '7px',
    height:       '7px',
    borderRadius: '50%',
    flexShrink:   0,
    transition:   'all 0.3s ease',
    display:      'inline-block',
  },
  liveLabel: {
    fontFamily:    "'DM Mono', monospace",
    fontSize:      '10px',
    fontWeight:    500,
    letterSpacing: '0.04em',
    transition:    'color 0.3s ease',
  },
}