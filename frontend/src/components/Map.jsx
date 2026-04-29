import { useEffect, useRef, useCallback } from 'react'
import mapboxgl from 'mapbox-gl'
import 'mapbox-gl/dist/mapbox-gl.css'
import { INCIDENT_COLORS, INCIDENT_LABELS } from '../utils/incidents'

mapboxgl.accessToken = import.meta.env.VITE_MAPBOX_TOKEN || ''

const DETROIT_CENTER = [-83.05, 42.38]
const DEFAULT_ZOOM   = 10
const SOURCE_ID    = 'incidents'
const CIRCLE_LAYER = 'incidents-circles'
const LABEL_LAYER  = 'incidents-labels'
const API = ''

// External overlay source/layer IDs
const EXT_SOURCES = {
  fire_stations: 'ext-fire-stations',
  dte_outages:   'ext-dte-outages',
  precincts:     'ext-precincts',
  battalions:    'ext-battalions',
}

// ── Zoom-responsive radius expressions ────────────────────────────────────
// Dots scale from tiny at zoom-out to readable at zoom-in.
// At zoom 8 (full metro view) they're 2px — no overlap.
// At zoom 14 (street level) they're 10px — clearly legible.
const RADIUS_NORMAL = [
  'interpolate', ['linear'], ['zoom'],
  7,  1.5,
  9,  3,
  11, 5,
  13, 8,
  15, 12,
]

// Selected incident gets a boost at every zoom level
const RADIUS_SELECTED_BOOST = [
  'interpolate', ['linear'], ['zoom'],
  7,  3,
  9,  5,
  11, 8,
  13, 12,
  15, 16,
]

const STROKE_WIDTH_NORMAL = [
  'interpolate', ['linear'], ['zoom'],
  7,  0,
  9,  0.5,
  11, 1,
  13, 1.5,
  15, 2,
]

const GLOW_RADIUS = [
  'interpolate', ['linear'], ['zoom'],
  7,  3,
  9,  6,
  11, 10,
  13, 16,
  15, 22,
]

// Build a GeoJSON FeatureCollection from incidents array
function toGeoJSON(incidents) {
  return {
    type: 'FeatureCollection',
    features: incidents
      .filter(i => i.lat && i.lng)
      .map(i => ({
        type: 'Feature',
        geometry: {
          type:        'Point',
          coordinates: [i.lng, i.lat],
        },
        properties: {
          incident_id:   i.incident_id,
          incident_type: i.incident_type || 'UNKNOWN',
          status:        i.status        || 'ACTIVE',
          priority:      i.priority      || 'UNKNOWN',
          address:       i.address_full  || i.address_raw || '',
          summary:       i.summary       || '',
          county:        i.county        || '',
          units:         (i.units || []).join(', '),
          color:         INCIDENT_COLORS[i.incident_type] || INCIDENT_COLORS.UNKNOWN,
          selected:      0,
        },
      })),
  }
}

export default function Map({ incidents, selectedId, onSelectIncident, layers = {} }) {
  const containerRef = useRef(null)
  const mapRef       = useRef(null)
  const popupRef     = useRef(null)
  const readyRef     = useRef(false)

  // Init map once
  useEffect(() => {
    if (mapRef.current) return
    if (!mapboxgl.accessToken) return

    const map = new mapboxgl.Map({
      container: containerRef.current,
      style:     'mapbox://styles/mapbox/dark-v11',
      center:    DETROIT_CENTER,
      zoom:      DEFAULT_ZOOM,
    })

    mapRef.current = map
    map.addControl(new mapboxgl.NavigationControl(), 'top-right')

    map.on('load', () => {
      // ── GeoJSON source ──────────────────────────────────────────
      map.addSource(SOURCE_ID, {
        type: 'geojson',
        data: toGeoJSON([]),
      })

      // ── Outer glow (active incidents only) ─────────────────────
      map.addLayer({
        id:     'incidents-glow',
        type:   'circle',
        source: SOURCE_ID,
        filter: ['==', ['get', 'status'], 'ACTIVE'],
        paint: {
          'circle-radius':       GLOW_RADIUS,
          'circle-color':        ['get', 'color'],
          'circle-opacity':      0.15,
          'circle-blur':         1,
          'circle-stroke-width': 0,
        },
      })

      // ── Main circle layer ───────────────────────────────────────
      map.addLayer({
        id:     CIRCLE_LAYER,
        type:   'circle',
        source: SOURCE_ID,
        paint: {
          'circle-radius': RADIUS_NORMAL,
          'circle-color':  ['get', 'color'],
          'circle-opacity': [
            'case',
            ['==', ['get', 'status'], 'RESOLVED'], 0.3,
            1,
          ],
          'circle-stroke-width': STROKE_WIDTH_NORMAL,
          'circle-stroke-color': [
            'case',
            ['==', ['get', 'incident_id'], ''], '#ffffff',
            ['get', 'color'],
          ],
        },
      })

      // ── Cursor ──────────────────────────────────────────────────
      map.on('mouseenter', CIRCLE_LAYER, () => {
        map.getCanvas().style.cursor = 'pointer'
      })
      map.on('mouseleave', CIRCLE_LAYER, () => {
        map.getCanvas().style.cursor = ''
      })

      readyRef.current = true
    })

    map.on('error', e => console.error('Mapbox error:', e))

    return () => {
      readyRef.current = false
      map.remove()
      mapRef.current = null
    }
  }, [])

  // Load external overlay data when layers toggle
  useEffect(() => {
    const map = mapRef.current
    if (!map || !readyRef.current) return

    const overlays = [
      {
        id:       'ext-fire-stations',
        endpoint: `${API}/external/fire-stations`,
        visible:  layers.fireStations,
        type:     'circle',
        paint: {
          circle: {
            'circle-color':        '#ff4d6a',
            'circle-radius':       7,
            'circle-stroke-width': 2,
            'circle-stroke-color': '#fff',
            'circle-opacity':      0.9,
          },
        },
        label: 'F',
        popupFields: ['name', 'address', 'battalion'],
      },
      {
        id:       'ext-dte-outages',
        endpoint: 'https://outagemap.serv.dteenergy.com/GISRest/services/OMP/OutageLocations/MapServer/2/query?where=1%3D1&outFields=*&f=geojson&resultRecordCount=500&geometry=-84.1%2C42.0%2C-82.5%2C43.1&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects',
        visible:  layers.dteOutages,
        type:     'fill',
        paint: {
          fill: {
            'fill-color':         '#f5a623',
            'fill-opacity':       0.3,
            'fill-outline-color': '#f5a623',
          },
        },
        label: 'E',
        popupFields: [],
      },
    ]

    const polygonLayers = [
      {
        id:         'ext-precincts',
        endpoint:   `${API}/external/precincts`,
        visible:    layers.precincts,
        color:      '#4db8ff',
        label:      'precinct',
        labelField: 'precinct',
      },
      {
        id:         'ext-battalions',
        endpoint:   `${API}/external/battalions`,
        visible:    layers.battalions,
        color:      '#ff8844',
        label:      'battalion',
        labelField: 'label',
      },
      {
        id:         'ext-counties',
        endpoint:   `${API}/external/counties`,
        visible:    layers.counties,
        color:      '#a78bfa',
        label:      'county',
        labelField: 'label',
      },
    ]

    // Handle polygon boundary layers
    polygonLayers.forEach(async ({ id, endpoint, visible, color, label, labelField }) => {
      const fillId  = `${id}-fill`
      const lineId  = `${id}-line`
      const labelId = `${id}-label`

      if (!visible) {
        [fillId, lineId, labelId].forEach(lid => {
          if (map.getLayer(lid)) map.setLayoutProperty(lid, 'visibility', 'none')
        })
        return
      }

      try {
        const resp = await fetch(endpoint)
        const data = await resp.json()

        if (map.getSource(id)) {
          map.getSource(id).setData(data)
          ;[fillId, lineId, labelId].forEach(lid => {
            if (map.getLayer(lid)) map.setLayoutProperty(lid, 'visibility', 'visible')
          })
        } else {
          map.addSource(id, { type: 'geojson', data })

          const lineWidth   = id.includes('counties') ? 2.5 : 1.5
          const fillOpacity = id.includes('counties') ? 0.04 : 0.08
          const labelSize   = id.includes('counties') ? 13 : 11

          map.addLayer({
            id: fillId, type: 'fill', source: id,
            paint: { 'fill-color': color, 'fill-opacity': fillOpacity },
          }, CIRCLE_LAYER)

          map.addLayer({
            id: lineId, type: 'line', source: id,
            paint: { 'line-color': color, 'line-width': lineWidth, 'line-opacity': 0.7 },
          }, CIRCLE_LAYER)

          map.addLayer({
            id: labelId, type: 'symbol', source: id,
            layout: {
              'text-field':         ['get', labelField],
              'text-size':          labelSize,
              'text-allow-overlap': false,
              'text-font':          ['DIN Offc Pro Medium', 'Arial Unicode MS Bold'],
            },
            paint: { 'text-color': color, 'text-opacity': 0.9 },
          })
        }
      } catch (e) {
        console.error(`Failed to load polygon layer ${id}:`, e)
      }
    })

    overlays.forEach(async ({ id, endpoint, visible, paint, label }) => {
      const circleId = `${id}-circles`
      const labelId  = `${id}-labels`

      if (!visible) {
        if (map.getLayer(circleId)) {
          map.setLayoutProperty(circleId, 'visibility', 'none')
          map.setLayoutProperty(labelId,  'visibility', 'none')
        }
        return
      }

      try {
        const resp = await fetch(endpoint)
        const data = await resp.json()

        if (map.getSource(id)) {
          map.getSource(id).setData(data)
          map.setLayoutProperty(circleId, 'visibility', 'visible')
          map.setLayoutProperty(labelId,  'visibility', 'visible')
        } else {
          map.addSource(id, { type: 'geojson', data })

          map.addLayer({
            id:     circleId,
            type:   'circle',
            source: id,
            paint:  paint.circle,
          })

          map.addLayer({
            id:     labelId,
            type:   'symbol',
            source: id,
            layout: {
              'text-field':         label,
              'text-size':          10,
              'text-offset':        [0, -1.8],
              'text-allow-overlap': true,
            },
            paint: { 'text-color': '#ffffff' },
          })

          map.on('click', circleId, (e) => {
            const props = e.features[0].properties
            const name  = props.name || props.description || props.type || id
            const ll    = e.features[0].geometry.coordinates
            new mapboxgl.Popup({ closeButton: true, maxWidth: '300px' })
              .setLngLat(ll)
              .setHTML(`<div style="font-family:monospace;font-size:11px;color:#e0e0e0;background:#1a2420;padding:8px;border-radius:4px">
                <div style="color:#00e5a0;font-weight:bold;margin-bottom:6px">${name}</div>
                ${props.route      ? `<div>Route: ${props.route}</div>`           : ''}
                ${props.county     ? `<div>County: ${props.county}</div>`         : ''}
                ${props.start_time ? `<div>Since: ${props.start_time}</div>`      : ''}
                ${props.image_url  ? `<br><img src="${props.image_url}" style="width:100%;border-radius:2px" onerror="this.style.display='none'">` : ''}
              </div>`)
              .addTo(map)
          })

          map.on('mouseenter', circleId, () => { map.getCanvas().style.cursor = 'pointer' })
          map.on('mouseleave', circleId, () => { map.getCanvas().style.cursor = '' })
        }
      } catch (e) {
        console.error(`Failed to load overlay ${id}:`, e)
      }
    })
  }, [layers])

  // Update GeoJSON source whenever incidents change
  useEffect(() => {
    const map = mapRef.current
    if (!map || !readyRef.current) return
    const source = map.getSource(SOURCE_ID)
    if (!source) return
    source.setData(toGeoJSON(incidents))
  }, [incidents])

  // Update selected circle styling when selectedId changes
  useEffect(() => {
    const map = mapRef.current
    if (!map || !readyRef.current) return
    const sid = selectedId || ''

    // Radius: selected gets boosted zoom-responsive size, others get normal
    map.setPaintProperty(CIRCLE_LAYER, 'circle-radius', [
      'case',
      ['==', ['get', 'incident_id'], sid], RADIUS_SELECTED_BOOST,
      RADIUS_NORMAL,
    ])

    // Stroke width: selected gets thicker at all zooms
    map.setPaintProperty(CIRCLE_LAYER, 'circle-stroke-width', [
      'case',
      ['==', ['get', 'incident_id'], sid],
      ['interpolate', ['linear'], ['zoom'], 7, 1.5, 11, 2, 15, 3],
      STROKE_WIDTH_NORMAL,
    ])

    // Stroke color: selected gets white ring
    map.setPaintProperty(CIRCLE_LAYER, 'circle-stroke-color', [
      'case',
      ['==', ['get', 'incident_id'], sid], '#ffffff',
      ['get', 'color'],
    ])

    // Glow also boosted on selected
    map.setPaintProperty('incidents-glow', 'circle-opacity', [
      'case',
      ['==', ['get', 'incident_id'], sid], 0.35,
      ['==', ['get', 'status'], 'ACTIVE'], 0.15,
      0,
    ])
  }, [selectedId])

  // Click handler — look up full incident object by id
  const incidentsRef = useRef(incidents)
  useEffect(() => { incidentsRef.current = incidents }, [incidents])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !readyRef.current) return
    const handler = (e) => {
      if (!e.features?.length) return
      const props = e.features[0].properties
      const full  = incidentsRef.current.find(i => i.incident_id === props.incident_id)
      if (full) onSelectIncident(full)
    }
    map.on('click', CIRCLE_LAYER, handler)
    return () => map.off('click', CIRCLE_LAYER, handler)
  }, [onSelectIncident])

  if (!mapboxgl.accessToken) {
    return (
      <div style={styles.noToken}>
        <div style={styles.noTokenText}>VITE_MAPBOX_TOKEN not set</div>
        <div style={styles.noTokenSub}>Add token to frontend/.env and restart Vite</div>
      </div>
    )
  }

  return (
    <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
  )
}

const styles = {
  noToken: {
    width: '100%', height: '100%',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: '#080c10', flexDirection: 'column', gap: '12px',
  },
  noTokenText: { fontFamily: 'monospace', color: '#ff4d6a', fontSize: '13px' },
  noTokenSub:  { fontFamily: 'monospace', color: '#5a7060', fontSize: '11px' },
}

// ── Layer Controls ────────────────────────────────────────────────────────

export function LayerControls({ layers, onChange }) {
  const controls = [
    { key: 'fireStations', label: 'Fire Stations',    color: '#ff4d6a' },
    { key: 'battalions',   label: 'DFD Battalions',   color: '#ff8844' },
    { key: 'precincts',    label: 'DPD Precincts',    color: '#4db8ff' },
    { key: 'dteOutages',   label: 'DTE Outages',      color: '#f5a623' },
    { key: 'counties',     label: 'County Boundaries',color: '#a78bfa' },
  ]

  return (
    <div style={lc.panel}>
      <div style={lc.title}>Map Layers</div>
      {controls.map(({ key, label, color }) => {
        const on = !!layers[key]
        return (
          <label key={key} style={lc.row}>
            <span
              style={{
                ...lc.swatch,
                background:  on ? color : 'transparent',
                borderColor: on ? color : 'rgba(255,255,255,0.15)',
                boxShadow:   on ? `0 0 6px ${color}66` : 'none',
              }}
              onClick={() => onChange({ ...layers, [key]: !on })}
            />
            <span
              style={{ ...lc.rowLabel, color: on ? '#f1f5f9' : '#475569' }}
              onClick={() => onChange({ ...layers, [key]: !on })}
            >
              {label}
            </span>
          </label>
        )
      })}
    </div>
  )
}

const lc = {
  panel: {
    position:      'absolute',
    bottom:        '36px',
    left:          '12px',
    zIndex:        10,
    background:    'rgba(13, 21, 32, 0.92)',
    border:        '1px solid rgba(255,255,255,0.09)',
    borderRadius:  '8px',
    padding:       '10px 12px',
    display:       'flex',
    flexDirection: 'column',
    gap:           '6px',
    backdropFilter:'blur(8px)',
    minWidth:      '152px',
  },
  title: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    letterSpacing: '0.12em',
    textTransform: 'uppercase',
    color:         '#475569',
    marginBottom:  '2px',
  },
  row: {
    display:    'flex',
    alignItems: 'center',
    gap:        '8px',
    cursor:     'pointer',
  },
  swatch: {
    width:        '10px',
    height:       '10px',
    borderRadius: '2px',
    border:       '1px solid',
    flexShrink:   0,
    cursor:       'pointer',
    transition:   'all 0.15s ease',
    display:      'inline-block',
  },
  rowLabel: {
    fontFamily:  'var(--mono)',
    fontSize:    '10px',
    transition:  'color 0.15s ease',
    userSelect:  'none',
  },
}