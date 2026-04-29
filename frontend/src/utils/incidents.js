export const INCIDENT_LABELS = {
  STRUCTURE_FIRE:      'Structure Fire',
  VEHICLE_FIRE:        'Vehicle Fire',
  MEDICAL:             'Medical',
  SHOOTING:            'Shooting',
  ASSAULT:             'Assault',
  WELFARE_CHECK:       'Welfare Check',
  PURSUIT:             'Pursuit',
  HAZMAT:              'HazMat',
  BOMB_THREAT:         'Bomb Threat',
  SUSPICIOUS:          'Suspicious',
  DOMESTIC:            'Domestic',
  TRAFFIC:             'Traffic',
  TRAFFIC_ACCIDENT:    'Traffic Accident',
  BURGLARY:            'Burglary',
  ROBBERY:             'Robbery',
  THEFT:               'Theft',
  ASSIST_CITIZEN:      'Assist Citizen',
  COMMERCE:            'Commerce',
  FIRE:                'Fire',
  HAZARD:              'Hazard',
  SUICIDE:             'Suicide / Mental',
  USE_OF_FORCE:        'Use of Force',
  SHAKEN_AND_BATTERED: 'Assault',
  RECOVERY_AUTO:       'Auto Recovery',
  ANIMAL_CONTROL:      'Animal Control',
  PRISONER:            'Prisoner',
  OTHER:               'Other',
  UNKNOWN:             'Unknown',
}

export const PRIORITY_COLORS = {
  HIGH:    '#ef4444',
  MEDIUM:  '#f59e0b',
  LOW:     '#10b981',
  UNKNOWN: '#64748b',
}


export const INCIDENT_COLORS = {
  // ── Fire (red-orange family) ──────────────────────────────────────
  STRUCTURE_FIRE:      '#ff3d00',   // deep red-orange — most severe
  VEHICLE_FIRE:        '#ff6d00',   // orange — vehicle fire
  FIRE:                '#ff9100',   // amber-orange — generic fire

  // ── Medical ───────────────────────────────────────────────────────
  MEDICAL:             '#03a9f4',   // bright sky blue — EMS/medical
  SUICIDE:             '#7986cb',   // blue-indigo — mental health crisis

  // ── Violent crime ─────────────────────────────────────────────────
  SHOOTING:            '#d50000',   // pure red — most serious
  ASSAULT:             '#ff1744',   // vivid red — assault
  SHAKEN_AND_BATTERED: '#f50057',   // red-pink — domestic battery
  DOMESTIC:            '#e040fb',   // vivid purple-pink — domestic disturbance
  USE_OF_FORCE:        '#aa00ff',   // deep violet — officer use of force

  // ── Property crime ────────────────────────────────────────────────
  ROBBERY:             '#ff6f00',   // deep amber — robbery (person + property)
  BURGLARY:            '#ffa000',   // amber — break-in
  THEFT:               '#ffd600',   // yellow — theft
  COMMERCE:            '#f9a825',   // gold — commercial crime
  RECOVERY_AUTO:       '#ffb300',   // warm amber — auto recovery

  // ── Police / order ────────────────────────────────────────────────
  PURSUIT:             '#304ffe',   // vivid blue — active vehicle pursuit
  SUSPICIOUS:          '#448aff',   // lighter blue — suspicious activity
  WELFARE_CHECK:       '#40c4ff',   // pale blue — wellness/welfare
  PRISONER:            '#1565c0',   // dark navy blue — prisoner transport

  // ── Community / assist ────────────────────────────────────────────
  ASSIST_CITIZEN:      '#00c853',   // bright green — positive call
  ANIMAL_CONTROL:      '#69f0ae',   // light green — animal related

  // ── Hazardous ─────────────────────────────────────────────────────
  HAZMAT:              '#aeea00',   // neon lime — active chemical hazard
  HAZARD:              '#c6ff00',   // yellow-green — general hazard
  BOMB_THREAT:         '#ffea00',   // bright yellow — explosive threat

  // ── Traffic ───────────────────────────────────────────────────────
  TRAFFIC_ACCIDENT:    '#00bfa5',   // teal-green — accident
  TRAFFIC:             '#80cbc4',   // muted teal — general traffic

  // ── Fallback ──────────────────────────────────────────────────────
  OTHER:               '#90a4ae',   // blue-grey
  UNKNOWN:             '#546e7a',   // dark blue-grey
}

export function getTagClass(incidentType) {
  const type = (incidentType || '').toUpperCase()
  if (type.includes('FIRE') || type === 'FIRE') return 'fire'
  if (type === 'MEDICAL' || type === 'SUICIDE') return 'medical'
  if (['SHOOTING','ASSAULT','DOMESTIC','ROBBERY','BURGLARY',
       'USE_OF_FORCE','SHAKEN_AND_BATTERED'].includes(type)) return 'shooting'
  if (type === 'HAZMAT' || type === 'HAZARD' || type === 'BOMB_THREAT') return 'hazmat'
  if (['PURSUIT','WELFARE_CHECK','SUSPICIOUS','COMMERCE',
       'ASSIST_CITIZEN'].includes(type)) return 'police'
  if (['TRAFFIC','TRAFFIC_ACCIDENT','THEFT'].includes(type)) return 'unknown'
  return 'unknown'
}

export function formatTime(ts) {
  if (!ts) return '--'
  try {
    const d = new Date(ts)
    if (isNaN(d)) return '--'
    return d.toLocaleTimeString('en-US', {
      hour:   '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  } catch {
    return '--'
  }
}

export function formatAge(ts) {
  if (!ts) return '--'
  try {
    const d = new Date(ts)
    if (isNaN(d)) return '--'
    const diffMs = Date.now() - d.getTime()
    if (diffMs < 0)        return 'just now'
    if (diffMs < 60000)    return 'just now'
    if (diffMs < 3600000)  return `${Math.floor(diffMs / 60000)}m ago`
    if (diffMs < 86400000) return `${Math.floor(diffMs / 3600000)}h ago`
    return `${Math.floor(diffMs / 86400000)}d ago`
  } catch {
    return '--'
  }
}

export function sortIncidents(incidents, sortBy) {
  const PRIORITY_ORDER = { HIGH: 0, MEDIUM: 1, LOW: 2, UNKNOWN: 3 }
  switch (sortBy) {
    case 'Newest':
      return [...incidents].sort((a, b) =>
        new Date(b.opened_at) - new Date(a.opened_at))
    case 'Most Updates':
      return [...incidents].sort((a, b) =>
        (b.chunk_count || 0) - (a.chunk_count || 0))
    case 'Priority':
      return [...incidents].sort((a, b) =>
        (PRIORITY_ORDER[a.priority] ?? 3) - (PRIORITY_ORDER[b.priority] ?? 3))
    default:
      return [...incidents].sort((a, b) =>
        new Date(b.last_updated || b.opened_at) -
        new Date(a.last_updated || a.opened_at))
  }
}