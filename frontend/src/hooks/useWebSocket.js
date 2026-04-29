import { useEffect, useRef, useState, useCallback } from 'react'

// Dynamically build WebSocket URL from current page location
// Works on localhost, internal IP, or any domain in front
const getWsUrl = () => {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const host     = window.location.host
  return `${protocol}://${host}/ws`
}

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