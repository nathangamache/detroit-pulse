import { useState, useEffect, useCallback, useRef } from 'react'
import Map, { LayerControls } from './components/Map'
import IncidentFeed   from './components/IncidentFeed'
import IncidentDetail from './components/IncidentDetail'
import AudioPlayer    from './components/AudioPlayer'
import Header         from './components/Header'
import DebugPanel     from './components/DebugPanel'
import AdminPanel     from './components/AdminPanel'
import { useWebSocket } from './hooks/useWebSocket'

const API = ''
const MAX_DEBUG_EVENTS = 200

export default function App() {
  const [incidents,   setIncidents]  = useState([])
  const [selectedId,  setSelectedId] = useState(null)
  const [feeds,       setFeeds]      = useState([])
  const [debugEvents, setDebugEvents]= useState([])

  const [showDebug,  setShowDebug]  = useState(false)
  const [showAdmin,  setShowAdmin]  = useState(false)
  const [showDetail, setShowDetail] = useState(false)
  const [mapLayers,  setMapLayers]  = useState({
    fireStations: false,
    battalions:   false,
    precincts:    false,
    crimes:       false,
    dteOutages:   false,
    counties:     false,
  })

  const seenTs = useRef(new Set())

  useEffect(() => {
    fetch(`${API}/incidents/active`)
      .then(r => r.json())
      .then(data => setIncidents(data))
      .catch(() => {})
    fetch(`${API}/audio/feeds`)
      .then(r => r.json())
      .then(data => setFeeds(data))
      .catch(() => {})
  }, [])

  const addDebugEvent = useCallback((event, data, ts) => {
    const key = data?.event_uuid ? data.event_uuid : `${event}:${ts}`
    if (seenTs.current.has(key)) return
    seenTs.current.add(key)
    if (seenTs.current.size > MAX_DEBUG_EVENTS * 2) {
      const arr = [...seenTs.current]
      seenTs.current = new Set(arr.slice(-MAX_DEBUG_EVENTS))
    }
    setDebugEvents(prev => [...prev, { event, data, ts }].slice(-MAX_DEBUG_EVENTS))
  }, [])

  const handleWsEvent = useCallback((event, data) => {
    if (event === 'debug:history') {
      ;(data.events || []).forEach(e => addDebugEvent(e.event, e.data, e.ts * 1000))
      return
    }

    addDebugEvent(event, data, Date.now())

    switch (event) {
      case 'incident:new':
        setIncidents(prev => {
          if (prev.find(i => i.incident_id === data.incident_id)) return prev
          return [data, ...prev]
        })
        break
      case 'incident:update':
        setIncidents(prev => prev.map(i =>
          i.incident_id === data.incident_id ? { ...i, ...data } : i
        ))
        break
      case 'incident:resolve':
        setIncidents(prev => prev.map(i =>
          i.incident_id === data.incident_id ? { ...i, ...data } : i
        ))
        break
      default:
        break
    }
  }, [addDebugEvent])

  const [filterType, setFilterType] = useState('ALL')

  const connected        = useWebSocket(handleWsEvent)
  const selectedIncident = incidents.find(i => i.incident_id === selectedId) || null
  const activeCount      = incidents.filter(i => i.status === 'ACTIVE').length

  // Filtered incidents — shared between feed and map
  const filteredIncidents = filterType === 'ALL'
    ? incidents
    : incidents.filter(i => (i.incident_type || 'UNKNOWN') === filterType)

  const handleSetRightPanel = (panel) => {
    if (panel === 'debug') {
      if (!showDebug) { setShowDetail(false); setSelectedId(null) }
      setShowDebug(v => !v)
      setShowAdmin(false)
    } else if (panel === 'admin') {
      setShowAdmin(v => !v)
      setShowDebug(false)
    }
  }

  const handleSelectIncident = (inc) => {
    setSelectedId(inc.incident_id)
    setShowDetail(true)
    setShowDebug(false)
  }

  const handleCloseDetail = () => {
    setSelectedId(null)
    setShowDetail(false)
  }

  const showRightSidebar = showDetail || showDebug

  return (
    <div style={styles.root}>
      <Header
        connected={connected}
        incidentCount={activeCount}
        showDebug={showDebug}
        showAdmin={showAdmin}
        onSetRightPanel={handleSetRightPanel}
      />

      <div style={styles.body}>
        {/* ── Left panel: audio + feed ── */}
        <div style={styles.leftPanel}>
          <div style={{ flexShrink: 0 }}>
            <AudioPlayer feeds={feeds} />
          </div>
          <IncidentFeed
            incidents={incidents}
            selectedId={selectedId}
            onSelect={handleSelectIncident}
            filterType={filterType}
            onFilterChange={setFilterType}
          />
        </div>

        {/* ── Map ── */}
        <div style={styles.mapPanel}>
          <Map
            incidents={filteredIncidents}
            selectedId={selectedId}
            onSelectIncident={handleSelectIncident}
            layers={mapLayers}
          />
          <LayerControls layers={mapLayers} onChange={setMapLayers} />
        </div>

        {/* ── Admin panel (slides over map) ── */}
        {showAdmin && (
          <div style={{
            ...styles.adminPanel,
            right: showRightSidebar ? '360px' : '0',
          }}>
            <AdminPanel onClose={() => setShowAdmin(false)} />
          </div>
        )}

        {/* ── Right sidebar: detail or debug ── */}
        {showRightSidebar && (
          <div style={styles.rightPanel}>

            {showDetail ? (
              <IncidentDetail
                incident={selectedIncident}
                onClose={handleCloseDetail}
              />
            ) : (
              <DebugPanel
                events={debugEvents}
                onClose={() => setShowDebug(false)}
              />
            )}
          </div>
        )}
      </div>
    </div>
  )
}

const styles = {
  root: {
    display:       'flex',
    flexDirection: 'column',
    height:        '100vh',
    width:         '100vw',
    overflow:      'hidden',
    background:    'var(--bg)',
  },
  body: {
    display:   'flex',
    flex:      1,
    overflow:  'hidden',
    position:  'relative',
    minHeight: 0,
  },
  leftPanel: {
    width:         '320px',
    flexShrink:    0,
    display:       'flex',
    flexDirection: 'column',
    overflow:      'hidden',
    position:      'relative',
    zIndex:        10,
    minHeight:     0,
  },
  mapPanel: {
    position: 'absolute',
    top: 0, left: '320px', right: 0, bottom: 0,
    overflow: 'hidden',
  },
  rightPanel: {
    position:   'absolute',
    top: 0, right: 0, bottom: 0,
    width:      '360px',
    zIndex:     20,
    overflow:   'hidden',
    background: 'var(--bg2)',
    borderLeft: '1px solid var(--border)',
    boxShadow:  '-8px 0 32px rgba(0,0,0,0.4)',
  },
  adminPanel: {
    position:   'absolute',
    top: 0, left: '300px', bottom: 0,
    width:      '400px',
    zIndex:     15,
    overflow:   'hidden',
    background: 'var(--bg2)',
    borderLeft: '1px solid var(--border)',
    boxShadow:  '8px 0 32px rgba(0,0,0,0.5)',
    transition: 'right 0.2s ease',
  },
}