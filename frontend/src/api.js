// One place for the API location. The app is served under Vite's base ('/pulse/'), and the
// gateway proxies '/pulse/api/*' to Pulse on 127.0.0.1:7710 (adding its bearer token), so
// every call is relative to the page: no host, no token in the browser.
export const API = import.meta.env.BASE_URL.replace(/\/$/, '') + '/api'

export function wsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${window.location.host}${API}/ws`
}

// Admin calls carry the admin token the user typed into the admin panel (kept only for this tab).
export function adminHeaders() {
  let tok = ''
  try { tok = sessionStorage.getItem('pulseAdminToken') || '' } catch { /* storage blocked */ }
  return tok ? { 'X-Pulse-Admin-Token': tok } : {}
}

// Map style: OpenFreeMap (free, keyless, no request limits; attribution shown by MapLibre).
// Point VITE_MAP_STYLE_URL at a self-hosted style (e.g. one backed by a PMTiles file) to avoid
// third-party tile requests entirely.
export const MAP_STYLE = import.meta.env.VITE_MAP_STYLE_URL || 'https://tiles.openfreemap.org/styles/dark'
