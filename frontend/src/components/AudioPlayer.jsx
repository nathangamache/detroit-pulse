import { useRef, useState, useEffect } from 'react'

const API = ''

export default function AudioPlayer({ feeds }) {
  const [activeFeed, setActiveFeed] = useState(null)
  const [playing,    setPlaying]    = useState(false)
  const [volume,     setVolume]     = useState(0.8)
  const [error,      setError]      = useState(null)
  const audioRef = useRef(null)

  const enabledFeeds = feeds.filter(f => f.enabled)

  useEffect(() => {
    if (enabledFeeds.length > 0 && !activeFeed) {
      setActiveFeed(enabledFeeds[0].id)
    }
  }, [feeds])

  const togglePlay = () => {
    if (!audioRef.current) return
    setError(null)

    if (playing) {
      audioRef.current.pause()
      audioRef.current.src = ''
      audioRef.current.load()
      setPlaying(false)
      return
    }

    // Set src fresh every time we press play
    // Appending timestamp busts any browser cache
    const streamUrl = `${API}/audio/stream/${activeFeed}?t=${Date.now()}`
    audioRef.current.src = streamUrl
    audioRef.current.volume = volume

    const playPromise = audioRef.current.play()
    if (playPromise !== undefined) {
      playPromise
        .then(() => setPlaying(true))
        .catch(e => {
          console.error('Playback failed:', e)
          setError('Playback blocked — click play again')
          setPlaying(false)
        })
    }
  }

  const handleFeedChange = (e) => {
    // Stop current stream before switching
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current.src = ''
      audioRef.current.load()
    }
    setActiveFeed(e.target.value)
    setPlaying(false)
    setError(null)
  }

  const handleVolume = (e) => {
    const v = parseFloat(e.target.value)
    setVolume(v)
    if (audioRef.current) audioRef.current.volume = v
  }

  const activeFeedName = enabledFeeds.find(f => f.id === activeFeed)?.name || ''

  return (
    <div style={styles.container}>
      <div style={styles.label}>// AUDIO</div>

      <div style={styles.controls}>
        <button onClick={togglePlay} style={styles.playBtn}>
          {playing ? '⏸ PAUSE' : '▶ PLAY'}
        </button>

        <select
          value={activeFeed || ''}
          onChange={handleFeedChange}
          style={styles.select}
        >
          {enabledFeeds.map(f => (
            <option key={f.id} value={f.id}>{f.name}</option>
          ))}
        </select>

        <div style={styles.volumeWrap}>
          <span style={styles.volLabel}>VOL</span>
          <input
            type="range" min="0" max="1" step="0.05"
            value={volume}
            onChange={handleVolume}
            style={styles.slider}
          />
        </div>
      </div>

      {error && (
        <div style={styles.error}>⚠ {error}</div>
      )}

      {playing && !error && (
        <div style={styles.nowPlaying}>
          <span style={styles.liveDot} />
          {activeFeedName}
        </div>
      )}

      <audio
        ref={audioRef}
        style={{ display: 'none' }}
        onError={(e) => {
          console.error('Audio element error:', e)
          setError('Stream error — try again')
          setPlaying(false)
        }}
        onPlaying={() => setPlaying(true)}
      />
    </div>
  )
}

const styles = {
  container: {
    padding:      '10px 16px',
    borderBottom: '1px solid var(--border)',
    background:   'var(--bg2)',
    flexShrink:   0,
  },
  label: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    color:         'var(--text-dim)',
    letterSpacing: '2px',
    marginBottom:  '8px',
  },
  controls: {
    display:    'flex',
    alignItems: 'center',
    gap:        '10px',
    flexWrap:   'wrap',
  },
  playBtn: {
    padding:    '5px 12px',
    fontSize:   '10px',
    flexShrink: 0,
  },
  select: {
    flex:         1,
    minWidth:     '160px',
    background:   'var(--bg3)',
    border:       '1px solid var(--border)',
    color:        'var(--text)',
    fontFamily:   'var(--mono)',
    fontSize:     '10px',
    padding:      '5px 8px',
    borderRadius: 'var(--radius)',
    outline:      'none',
  },
  volumeWrap: {
    display:    'flex',
    alignItems: 'center',
    gap:        '6px',
    flexShrink: 0,
  },
  volLabel: {
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    color:         'var(--text-dim)',
    letterSpacing: '1px',
  },
  slider: {
    width:       '70px',
    cursor:      'pointer',
    accentColor: 'var(--green)',
  },
  nowPlaying: {
    display:       'flex',
    alignItems:    'center',
    gap:           '6px',
    marginTop:     '8px',
    fontFamily:    'var(--mono)',
    fontSize:      '9px',
    color:         'var(--text-dim)',
    letterSpacing: '0.5px',
  },
  liveDot: {
    display:      'inline-block',
    width:        '6px',
    height:       '6px',
    borderRadius: '50%',
    background:   'var(--red)',
    boxShadow:    '0 0 6px var(--red)',
  },
  error: {
    marginTop:  '6px',
    fontFamily: 'var(--mono)',
    fontSize:   '10px',
    color:      'var(--red)',
  },
}
