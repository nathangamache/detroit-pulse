import { useEffect, useRef, useState, useCallback } from 'react'

import { wsUrl } from '../api'

// WebSocket at <base>/api/ws (e.g. /pulse/api/ws behind the gateway). If it can't connect,
// App.jsx falls back to polling the REST API, so the UI keeps working without WebSockets.
const getWsUrl = wsUrl

const PING_INTERVAL   = 25000
const RECONNECT_DELAY = 3000

export function useWebSocket(onEvent) {
  const ws         = useRef(null)
  const pingTimer  = useRef(null)
  const reconnect  = useRef(null)
  const onEventRef = useRef(onEvent)
  const [connected, setConnected] = useState(false)

  useEffect(() => { onEventRef.current = onEvent }, [onEvent])

  const connect = useCallback(() => {
    if (ws.current?.readyState === WebSocket.OPEN) return

    const socket = new WebSocket(getWsUrl())
    ws.current = socket

    socket.onopen = () => {
      setConnected(true)
      clearTimeout(reconnect.current)
      pingTimer.current = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: 'ping' }))
        }
      }, PING_INTERVAL)
    }

    socket.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)
        if (msg.event !== 'pong') {
          onEventRef.current?.(msg.event, msg.data)
        }
      } catch {}
    }

    socket.onclose = () => {
      setConnected(false)
      clearInterval(pingTimer.current)
      reconnect.current = setTimeout(connect, RECONNECT_DELAY)
    }

    socket.onerror = () => socket.close()
  }, [])

  useEffect(() => {
    const timer = setTimeout(connect, 500)
    return () => {
      clearTimeout(timer)
      clearTimeout(reconnect.current)
      clearInterval(pingTimer.current)
      ws.current?.close()
    }
  }, [connect])

  return connected
}