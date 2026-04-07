/**
 * Fermax Intercom Card  v1.0.0
 *
 * Custom Lovelace card for the Fermax Blue integration.
 *
 * Behaviour
 * ─────────
 *  • Idle  — shows the last doorbell snapshot (camera.async_camera_image).
 *            Polls HA's WebRTC endpoint every few seconds; if no stream is
 *            active it gets a quick "no_stream" error and stays on the
 *            snapshot without any visible flash.
 *  • Connecting — shows snapshot dimmed + spinner; triggered when the server
 *            accepted the offer but is still waiting for the mediasoup relay
 *            to become ready (typically ~11 s after a doorbell ring).
 *  • Live  — shows the live video stream with a LIVE badge, mic toggle, and
 *            hang-up button.  Two-way audio: browser mic → intercom, intercom
 *            audio → browser speaker.
 *
 * When the entity_picture URL changes (new snapshot) the card immediately
 * retries the WebRTC connection so it goes live within one polling cycle of
 * the stream becoming ready.
 *
 * Usage
 * ─────
 *   type: custom:fermax-intercom-card
 *   entity: camera.fermax_olimpos_visitor
 *
 * Optional config keys
 * ─────────────────────
 *   aspect_ratio: "16/9"   (default "4/3")
 *   show_controls: false   (default true — hide mic/hangup in live view)
 */

const CARD_VERSION = '1.0.0';

// ── Retry / timing constants ─────────────────────────────────────────────────
const NO_STREAM_RETRY_MS   = 3000;  // Poll interval when no stream is running
const ERROR_RETRY_MS       = 5000;  // Back-off after unexpected errors
const RECONNECT_DELAY_MS   = 2000;  // Delay before reconnect after drop
const ENTITY_CHANGE_RETRY  = 150;   // Fast retry when snapshot URL changes
const CONNECTING_REVEAL_MS = 700;   // Delay before showing spinner (avoids
                                    // flash for fast no_stream responses)

const STUN = [
  { urls: 'stun:stun.l.google.com:19302' },
  { urls: 'stun:stun1.l.google.com:19302' },
];

// ─────────────────────────────────────────────────────────────────────────────

class FermaxIntercardCard extends HTMLElement {

  // ── Construction ────────────────────────────────────────────────────────────

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });

    // Lovelace
    this._hass   = null;
    this._config = null;

    // State machine: 'idle' | 'connecting' | 'live'
    this._state  = 'idle';

    // WebRTC session
    this._pc                = null;   // RTCPeerConnection
    this._unsub             = null;   // HA subscription cancel fn
    this._sessionId         = null;   // HA WebRTC session id
    this._pendingCandidates = [];     // Client ICE candidates buffered before
                                      // session_id arrives

    // Timers
    this._retryTimer      = null;
    this._connectingTimer = null;

    // Mic
    this._micStream = null;   // MediaStream from getUserMedia
    this._micMuted  = false;

    // Change-detection for snapshot URL
    this._lastEntityPicture = null;
  }

  // ── Lovelace API ────────────────────────────────────────────────────────────

  static getStubConfig() {
    return { entity: 'camera.fermax_visitor' };
  }

  setConfig(config) {
    if (!config.entity) {
      throw new Error('[fermax-intercom-card] "entity" is required');
    }
    this._config = config;
    this._render();
  }

  /** Called by HA whenever state changes anywhere in the system. */
  set hass(hass) {
    const prevHass = this._hass;
    this._hass = hass;

    if (!this._config) return;

    // ── Update snapshot image ──
    const entity = hass.states[this._config.entity];
    if (entity?.attributes.entity_picture) {
      const url = hass.hassUrl(entity.attributes.entity_picture);
      const img = this.shadowRoot?.querySelector('.snapshot');
      if (img && img.dataset.src !== url) {
        img.dataset.src = url;
        img.src = url;
      }

      // ── Detect new snapshot → fast retry when idle ──
      if (
        prevHass &&
        this._state === 'idle' &&
        entity.attributes.entity_picture !==
          prevHass.states[this._config.entity]?.attributes.entity_picture
      ) {
        this._scheduleRetry(ENTITY_CHANGE_RETRY);
      }
    }

    // First hass assignment — start polling
    if (!prevHass) {
      this._scheduleRetry(0);
    }
  }

  connectedCallback() {
    if (this._hass && this._state === 'idle' && !this._retryTimer) {
      this._scheduleRetry(0);
    }
  }

  disconnectedCallback() {
    this._cleanup();
  }

  // ── Rendering ────────────────────────────────────────────────────────────────

  _render() {
    const aspectRatio = this._config?.aspect_ratio ?? '4/3';
    const showControls = this._config?.show_controls !== false;

    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }

        ha-card { overflow: hidden; }

        .wrapper {
          position: relative;
          width: 100%;
          aspect-ratio: ${aspectRatio};
          background: #111;
          overflow: hidden;
        }

        /* ── Media layers ─────────────────────────────── */
        .snapshot, .stream {
          position: absolute;
          inset: 0;
          width: 100%;
          height: 100%;
          object-fit: cover;
          transition: opacity 0.3s ease;
        }

        .snapshot { opacity: 1; }
        .snapshot.dim { opacity: 0.35; }

        .stream {
          opacity: 0;
          pointer-events: none;
        }
        .stream.visible { opacity: 1; pointer-events: auto; }

        /* ── Connecting overlay ───────────────────────── */
        .overlay {
          position: absolute;
          inset: 0;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 12px;
          color: #fff;
          font-family: var(--primary-font-family, sans-serif);
          font-size: 14px;
          opacity: 0;
          pointer-events: none;
          transition: opacity 0.25s ease;
        }
        .overlay.visible {
          opacity: 1;
        }

        .spinner {
          width: 38px;
          height: 38px;
          border: 3px solid rgba(255,255,255,0.22);
          border-top-color: #fff;
          border-radius: 50%;
          animation: spin 0.85s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* ── LIVE badge ───────────────────────────────── */
        .live-badge {
          position: absolute;
          top: 10px;
          left: 10px;
          background: rgba(210, 30, 30, 0.88);
          color: #fff;
          font-size: 10px;
          font-weight: 700;
          letter-spacing: 0.10em;
          padding: 3px 8px 2px;
          border-radius: 4px;
          font-family: var(--primary-font-family, sans-serif);
          opacity: 0;
          pointer-events: none;
          transition: opacity 0.25s ease;
        }
        .live-badge.visible { opacity: 1; }

        /* ── Controls ─────────────────────────────────── */
        .controls {
          position: absolute;
          bottom: 14px;
          left: 0;
          right: 0;
          display: ${showControls ? 'flex' : 'none'};
          justify-content: center;
          gap: 16px;
          opacity: 0;
          pointer-events: none;
          transition: opacity 0.25s ease;
        }
        .controls.visible {
          opacity: 1;
          pointer-events: auto;
        }

        .btn {
          width: 52px;
          height: 52px;
          border-radius: 50%;
          border: none;
          cursor: pointer;
          font-size: 22px;
          display: flex;
          align-items: center;
          justify-content: center;
          box-shadow: 0 2px 8px rgba(0,0,0,0.45);
          transition: transform 0.1s, background 0.15s;
        }
        .btn:active { transform: scale(0.91); }

        .mic-btn  { background: rgba(255,255,255,0.88); }
        .mic-btn.muted { background: rgba(215, 45, 45, 0.88); }
        .hangup-btn { background: rgba(215, 30, 30, 0.92); }
      </style>

      <ha-card>
        <div class="wrapper">
          <img  class="snapshot" src="" alt="Last visitor snapshot" />
          <video class="stream" autoplay playsinline></video>

          <div class="overlay" id="overlay">
            <div class="spinner"></div>
            <span>Connecting…</span>
          </div>

          <div class="live-badge" id="live-badge">● LIVE</div>

          <div class="controls" id="controls">
            <button class="btn mic-btn"    id="mic-btn"    title="Mute / unmute microphone">🎤</button>
            <button class="btn hangup-btn" id="hangup-btn" title="End call">📵</button>
          </div>
        </div>
      </ha-card>
    `;

    this.shadowRoot.getElementById('mic-btn')
      .addEventListener('click', () => this._toggleMic());
    this.shadowRoot.getElementById('hangup-btn')
      .addEventListener('click', () => this._hangup());
  }

  /** Apply visual state without losing the current snapshot image. */
  _applyState(state) {
    this._state = state;

    const snapshot  = this.shadowRoot?.querySelector('.snapshot');
    const stream    = this.shadowRoot?.querySelector('.stream');
    const overlay   = this.shadowRoot?.getElementById('overlay');
    const liveBadge = this.shadowRoot?.getElementById('live-badge');
    const controls  = this.shadowRoot?.getElementById('controls');

    if (!overlay) return; // Not yet rendered

    const cls = (el, name, on) => el.classList.toggle(name, on);

    if (state === 'idle') {
      cls(snapshot,  'dim',     false);
      cls(stream,    'visible', false);
      cls(overlay,   'visible', false);
      cls(liveBadge, 'visible', false);
      cls(controls,  'visible', false);

    } else if (state === 'connecting') {
      cls(snapshot,  'dim',     true);
      cls(stream,    'visible', false);
      cls(overlay,   'visible', true);
      cls(liveBadge, 'visible', false);
      cls(controls,  'visible', false);

    } else if (state === 'live') {
      cls(snapshot,  'dim',     false);
      cls(stream,    'visible', true);
      cls(overlay,   'visible', false);
      cls(liveBadge, 'visible', true);
      cls(controls,  'visible', true);
    }
  }

  // ── Retry scheduling ─────────────────────────────────────────────────────────

  _scheduleRetry(delay = NO_STREAM_RETRY_MS) {
    if (this._retryTimer) return; // Already pending
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      if (this._state !== 'live') this._connect();
    }, delay);
  }

  // ── WebRTC session ───────────────────────────────────────────────────────────

  async _connect() {
    if (!this._hass || !this._config) return;
    if (this._pc) return; // Already connecting

    try {
      // ── 1. Request mic — non-fatal if denied ──────────────────────────────
      let micStream = null;
      try {
        micStream = await navigator.mediaDevices.getUserMedia({
          audio: true,
          video: false,
        });
      } catch (_) {
        // Mic not available or permission denied — continue without mic
      }

      // ── 2. Create RTCPeerConnection ───────────────────────────────────────
      const pc = new RTCPeerConnection({ iceServers: STUN });
      this._pc = pc;
      this._pendingCandidates = [];

      // Audio transceiver: sendrecv if mic available, recvonly otherwise
      if (micStream) {
        this._micStream = micStream;
        pc.addTransceiver(micStream.getAudioTracks()[0], { direction: 'sendrecv' });
      } else {
        pc.addTransceiver('audio', { direction: 'recvonly' });
      }

      // Video transceiver: always receive-only (we don't send camera from browser)
      pc.addTransceiver('video', { direction: 'recvonly' });

      // ── 3. Route incoming tracks to <video> ───────────────────────────────
      const videoEl = this.shadowRoot.querySelector('.stream');
      const remoteStream = new MediaStream();
      videoEl.srcObject = remoteStream;

      pc.addEventListener('track', ev => {
        remoteStream.addTrack(ev.track);
      });

      // ── 4. PC connection state → card state ──────────────────────────────
      pc.addEventListener('connectionstatechange', () => {
        const s = pc.connectionState;
        if (this._pc !== pc) return; // Stale PC

        if (s === 'connected') {
          clearTimeout(this._connectingTimer);
          this._connectingTimer = null;
          this._applyState('live');
        } else if (['failed', 'disconnected', 'closed'].includes(s)) {
          this._cleanup();
          this._applyState('idle');
          this._scheduleRetry(RECONNECT_DELAY_MS);
        }
      });

      // ── 5. Buffer client ICE candidates until session_id known ───────────
      pc.addEventListener('icecandidate', ev => {
        if (!ev.candidate) return;
        const c = {
          candidate:     ev.candidate.candidate,
          sdpMid:        ev.candidate.sdpMid,
          sdpMLineIndex: ev.candidate.sdpMLineIndex,
        };
        if (this._sessionId) {
          this._sendCandidate(c);
        } else {
          this._pendingCandidates.push(c);
        }
      });

      // ── 6. Build offer ────────────────────────────────────────────────────
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      // ── 7. Subscribe to HA WebRTC session ────────────────────────────────
      //
      // HA sends back events as subscription messages:
      //   {type:"session",  session_id:"..."}   — arrives immediately
      //   {type:"answer",   answer:"<SDP>"}      — after relay is ready
      //   {type:"error",    code:"no_stream", …} — no active stream
      //   {type:"candidate",candidate:{…}}       — server trickle ICE (rare)
      this._unsub = await this._hass.connection.subscribeMessage(
        (event) => this._handleEvent(pc, event),
        {
          type:      'camera/webrtc/offer',
          entity_id: this._config.entity,
          offer:     pc.localDescription.sdp,
        },
      );

      // Show the connecting spinner only if the server doesn't respond
      // immediately (avoids a flash for fast no_stream rejections).
      this._connectingTimer = setTimeout(() => {
        if (this._state === 'idle') this._applyState('connecting');
      }, CONNECTING_REVEAL_MS);

    } catch (err) {
      console.warn('[fermax-intercom-card] _connect error:', err);
      this._cleanup();
      this._applyState('idle');
      this._scheduleRetry(ERROR_RETRY_MS);
    }
  }

  // ── HA event handler ─────────────────────────────────────────────────────────

  async _handleEvent(pc, event) {
    if (this._pc !== pc) return; // Stale

    switch (event.type) {

      case 'session': {
        // Session id received — flush any buffered ICE candidates
        this._sessionId = event.session_id;
        for (const c of this._pendingCandidates) {
          await this._sendCandidate(c);
        }
        this._pendingCandidates = [];
        break;
      }

      case 'answer': {
        // SDP answer from server — complete the negotiation
        clearTimeout(this._connectingTimer);
        this._connectingTimer = null;
        try {
          await pc.setRemoteDescription({ type: 'answer', sdp: event.answer });
        } catch (err) {
          console.warn('[fermax-intercom-card] setRemoteDescription failed:', err);
          this._cleanup();
          this._applyState('idle');
          this._scheduleRetry(ERROR_RETRY_MS);
        }
        break;
      }

      case 'candidate': {
        // Server-side ICE candidate (trickle from server — uncommon in our impl)
        const c = event.candidate;
        if (c?.candidate) {
          try { await pc.addIceCandidate(c); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'error': {
        clearTimeout(this._connectingTimer);
        this._connectingTimer = null;
        const code = event.code ?? '';
        this._cleanup();
        this._applyState('idle');
        if (code === 'no_stream') {
          // Normal polling — stream not active yet; retry quietly
          this._scheduleRetry(NO_STREAM_RETRY_MS);
        } else {
          console.warn('[fermax-intercom-card] server WebRTC error:', event);
          this._scheduleRetry(ERROR_RETRY_MS);
        }
        break;
      }

      default:
        break;
    }
  }

  // ── ICE candidate helper ─────────────────────────────────────────────────────

  async _sendCandidate(c) {
    if (!this._sessionId || !this._hass) return;
    try {
      await this._hass.callWS({
        type:       'camera/webrtc/candidate',
        entity_id:  this._config.entity,
        session_id: this._sessionId,
        candidate:  c,
      });
    } catch (_) { /* session may already be gone on server */ }
  }

  // ── Controls ─────────────────────────────────────────────────────────────────

  _toggleMic() {
    if (!this._micStream) return;
    this._micMuted = !this._micMuted;
    this._micStream.getAudioTracks().forEach(t => {
      t.enabled = !this._micMuted;
    });
    const btn = this.shadowRoot.getElementById('mic-btn');
    if (btn) {
      btn.classList.toggle('muted', this._micMuted);
      btn.textContent = this._micMuted ? '🔇' : '🎤';
    }
  }

  async _hangup() {
    this._cleanup();
    this._applyState('idle');
    try {
      await this._hass.callService('camera', 'turn_off', {
        entity_id: this._config.entity,
      });
    } catch (_) { /* ignore */ }
    this._scheduleRetry(RECONNECT_DELAY_MS);
  }

  // ── Cleanup ───────────────────────────────────────────────────────────────────

  _cleanup() {
    clearTimeout(this._retryTimer);
    clearTimeout(this._connectingTimer);
    this._retryTimer      = null;
    this._connectingTimer = null;

    if (this._unsub) {
      try { this._unsub(); } catch (_) {}
      this._unsub = null;
    }
    if (this._pc) {
      try { this._pc.close(); } catch (_) {}
      this._pc = null;
    }
    if (this._micStream) {
      this._micStream.getTracks().forEach(t => t.stop());
      this._micStream = null;
    }

    this._sessionId         = null;
    this._pendingCandidates = [];
    this._micMuted          = false;

    // Reset mic button appearance
    const btn = this.shadowRoot?.getElementById('mic-btn');
    if (btn) {
      btn.classList.remove('muted');
      btn.textContent = '🎤';
    }
  }
}

// ── Register ──────────────────────────────────────────────────────────────────

customElements.define('fermax-intercom-card', FermaxIntercardCard);

window.customCards ??= [];
window.customCards.push({
  type:        'fermax-intercom-card',
  name:        'Fermax Intercom Card',
  description: 'Live WebRTC intercom card with two-way audio for Fermax Blue',
  preview:     false,
});

console.info(
  `%c FERMAX-INTERCOM-CARD %c v${CARD_VERSION} `,
  'background:#111;color:#ff9800;font-weight:bold;padding:2px 4px;',
  'background:#ff9800;color:#fff;font-weight:bold;padding:2px 4px;',
);
