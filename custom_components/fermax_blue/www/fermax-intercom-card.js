/**
 * Fermax Intercom Card  v1.1.1
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

const CARD_VERSION = '1.1.1';

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
    this._connecting        = false;  // Synchronous in-flight re-entrancy lock
    this._connectEpoch      = 0;      // Generation counter — bumped on cleanup
                                      // to invalidate any in-flight _connect()
    this._userHungUp        = false;  // true after an explicit user hangup —
                                      // suppresses auto-reconnect until re-open

    // Timers
    this._retryTimer      = null;
    this._connectingTimer = null;

    // Mic
    this._micStream   = null;   // MediaStream from getUserMedia
    this._micMuted    = false;
    this._micDenied   = false;  // true after getUserMedia was denied

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
        // A fresh doorbell ring (new snapshot) is a new event — clear any
        // prior user hangup so the card connects again.
        this._userHungUp = false;
        this._scheduleRetry(ENTITY_CHANGE_RETRY);
      }
    }

    // First hass assignment — start polling
    if (!prevHass) {
      this._scheduleRetry(0);
    }
  }

  connectedCallback() {
    // Re-attaching the card (user re-opening the view) is an explicit re-open —
    // clear any prior user hangup so polling/connecting resumes.
    this._userHungUp = false;
    if (this._hass && this._state === 'idle' && !this._retryTimer) {
      this._scheduleRetry(0);
    }
  }

  disconnectedCallback() {
    this._cleanup();
  }

  // ── Rendering ────────────────────────────────────────────────────────────────

  /**
   * Validate the author-controlled aspect_ratio config value before it is
   * interpolated into a <style> block. Accepts only ratio forms like "16/9",
   * "4/3", "1.777", or percentages like "75%". Anything else falls back to the
   * default "4/3" to prevent CSS injection.
   */
  _sanitizeAspectRatio(value) {
    const DEFAULT = '4/3';
    if (typeof value !== 'string') return DEFAULT;
    const trimmed = value.trim();
    const RATIO_RE = /^\d+(\.\d+)?(\s*\/\s*\d+(\.\d+)?)?$|^\d+(\.\d+)?%$/;
    return RATIO_RE.test(trimmed) ? trimmed : DEFAULT;
  }

  _render() {
    const aspectRatio = this._sanitizeAspectRatio(this._config?.aspect_ratio);
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

        /* mic states:
             default  = white  = active, unmuted
             .muted   = red    = active, muted by user
             .no-mic  = grey   = permission denied / unavailable  */
        .mic-btn          { background: rgba(255,255,255,0.88); }
        .mic-btn.muted    { background: rgba(215,  45,  45, 0.88); }
        .mic-btn.no-mic   { background: rgba(120, 120, 120, 0.60); cursor: not-allowed; }
        .mic-btn.flash    { background: rgba(215,  45,  45, 0.88); }
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
      // Sync mic button to reflect actual permission state now that controls are visible
      this._updateMicButton();
    }
  }

  // ── Retry scheduling ─────────────────────────────────────────────────────────

  _scheduleRetry(delay = NO_STREAM_RETRY_MS) {
    if (this._userHungUp) return; // User hung up — stay hung up, no auto-reconnect
    if (this._retryTimer) return; // Already pending
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      if (this._userHungUp) return; // Re-check in case hangup happened meanwhile
      if (this._state !== 'live') this._connect();
    }, delay);
  }

  // ── WebRTC session ───────────────────────────────────────────────────────────

  /**
   * Establish a WebRTC session with HA.
   *
   * @param {boolean} withMic — when false (default: idle poll + initial
   *   connect) NO microphone is acquired and the audio m-line is negotiated
   *   recvonly (receive intercom audio only). The OS mic indicator never
   *   lights up while the dashboard sits idle.
   *   When true (user pressed the mic button) the mic is acquired FIRST and
   *   added as a sendrecv transceiver so the INITIAL offer carries the
   *   outbound audio m-line — required because the HA camera WebRTC server
   *   does a single offer/answer with no renegotiation, so a mic added after
   *   the answer would never be transmitted.
   */
  async _connect(withMic = false) {
    if (!this._hass || !this._config) return;
    // Synchronous re-entrancy lock — set BEFORE the first await so two
    // overlapping _connect() calls can't both create a PC / subscription.
    if (this._pc || this._connecting) return;
    this._connecting = true;

    // Generation guard — capture the current epoch. Any cleanup (card removed,
    // view switched, superseding connect) bumps _connectEpoch; when we resume
    // after an await and the epoch no longer matches, we abort and tear down
    // anything we created so it can't leak.
    const epoch = ++this._connectEpoch;

    let pc = null;
    let micStream = null;
    try {
      // ── 1. (Optional) acquire the mic BEFORE building the offer ───────────
      // Only when the user explicitly asked to talk. Never during idle polling.
      if (withMic) {
        try {
          micStream = await navigator.mediaDevices.getUserMedia({
            audio: true,
            video: false,
          });
          this._micDenied = false;
        } catch (err) {
          // Permission denied / no device — fall back to recvonly (receive
          // only). Talkback stays off but the call still connects.
          this._micDenied = true;
          micStream = null;
          console.info('[fermax-intercom-card] Mic unavailable:', err.name, err.message);
        }
        // The permission prompt is awaited above — re-check the epoch before
        // we build anything so a teardown during the prompt can't leak.
        if (epoch !== this._connectEpoch) {
          if (micStream) micStream.getTracks().forEach(t => t.stop());
          return;
        }
      }

      // ── 2. Create RTCPeerConnection ───────────────────────────────────────
      pc = new RTCPeerConnection({ iceServers: STUN });
      this._pendingCandidates = [];

      // Audio transceiver:
      //   • with a mic  → sendrecv WITH the mic track, so the initial offer
      //     carries the outbound m-line (talkback works on a single-offer
      //     server).
      //   • without     → recvonly, so we still negotiate an audio m-line for
      //     RECEIVING intercom audio.
      if (micStream) {
        this._micStream = micStream;
        this._micMuted  = false;
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
      if (epoch !== this._connectEpoch) { // Superseded / destroyed during await
        try { pc.close(); } catch (_) {}
        if (micStream) micStream.getTracks().forEach(t => t.stop());
        return;
      }
      await pc.setLocalDescription(offer);
      if (epoch !== this._connectEpoch) { // Superseded / destroyed during await
        try { pc.close(); } catch (_) {}
        if (micStream) micStream.getTracks().forEach(t => t.stop());
        return;
      }

      // Publish the PC only now that it survived the awaits above.
      this._pc = pc;

      // ── 7. Subscribe to HA WebRTC session ────────────────────────────────
      //
      // HA sends back events as subscription messages:
      //   {type:"session",  session_id:"..."}   — arrives immediately
      //   {type:"answer",   answer:"<SDP>"}      — after relay is ready
      //   {type:"error",    code:"no_stream", …} — no active stream
      //   {type:"candidate",candidate:{…}}       — server trickle ICE (rare)
      const unsub = await this._hass.connection.subscribeMessage(
        (event) => this._handleEvent(pc, event),
        {
          type:      'camera/webrtc/offer',
          entity_id: this._config.entity,
          offer:     pc.localDescription.sdp,
        },
      );
      if (epoch !== this._connectEpoch) {
        // Cleanup ran (or a newer connect superseded us) while subscribing —
        // immediately cancel this subscription and close the PC so neither
        // leaks; stop the mic tracks too.
        try { unsub(); } catch (_) {}
        try { pc.close(); } catch (_) {}
        if (micStream) micStream.getTracks().forEach(t => t.stop());
        if (this._pc === pc) this._pc = null;
        return;
      }
      this._unsub = unsub;

      // Show the connecting spinner only if the server doesn't respond
      // immediately (avoids a flash for fast no_stream rejections).
      this._connectingTimer = setTimeout(() => {
        if (this._state === 'idle') this._applyState('connecting');
      }, CONNECTING_REVEAL_MS);

    } catch (err) {
      console.warn('[fermax-intercom-card] _connect error:', err);
      if (pc && this._pc !== pc) { try { pc.close(); } catch (_) {} }
      if (micStream && this._micStream !== micStream) {
        micStream.getTracks().forEach(t => t.stop());
      }
      this._cleanup();
      this._applyState('idle');
      this._scheduleRetry(ERROR_RETRY_MS);
    } finally {
      this._connecting = false;
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
        // SDP answer from server — the offer was ACCEPTED. Complete the
        // negotiation. (The mic, if any, was already part of the offer.)
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

  async _toggleMic() {
    // ── Mic already active → just mute/unmute locally (no reconnect) ─────────
    if (this._micStream) {
      this._micMuted = !this._micMuted;
      this._micStream.getAudioTracks().forEach(t => { t.enabled = !this._micMuted; });
      this._updateMicButton();
      return;
    }

    // ── Mic off → reconnect WITH the mic ────────────────────────────────────
    // The HA camera WebRTC server does a single offer/answer with no
    // renegotiation, so enabling talkback means re-offering with a sendrecv
    // audio m-line. Tear down the current session in-place (stay on the live
    // view, don't drop to the snapshot) and reconnect with the mic.
    this._micDenied = false;

    // In-place teardown of the current session, keeping the visual state on
    // 'connecting' and NOT scheduling an idle retry (mirrors part of _cleanup
    // but preserves _userHungUp = false and skips the retry timer).
    this._connectEpoch++;                     // invalidate any in-flight connect
    clearTimeout(this._connectingTimer);
    this._connectingTimer = null;
    if (this._unsub) { try { this._unsub(); } catch (_) {} this._unsub = null; }
    if (this._pc)    { try { this._pc.close(); } catch (_) {} this._pc = null; }
    this._sessionId         = null;
    this._pendingCandidates = [];

    this._applyState('connecting');
    await this._connect(true);

    // If getUserMedia was denied during that reconnect, give visible feedback.
    if (this._micDenied) {
      this._flashMicDenied();
    } else {
      this._updateMicButton();
    }
  }

  /** Flash the mic button red briefly to indicate permission was denied. */
  _flashMicDenied() {
    const btn = this.shadowRoot?.getElementById('mic-btn');
    if (!btn) return;
    btn.classList.add('flash');
    btn.textContent = '🚫';
    setTimeout(() => {
      btn.classList.remove('flash');
      this._updateMicButton();
    }, 900);
  }

  /** Sync mic button appearance to current state. */
  _updateMicButton() {
    const btn = this.shadowRoot?.getElementById('mic-btn');
    if (!btn) return;

    btn.classList.remove('muted', 'no-mic', 'flash');

    if (!this._micStream || this._micDenied) {
      btn.classList.add('no-mic');
      btn.textContent = '🎤';
      btn.title = 'Microphone unavailable — check browser permissions';
    } else if (this._micMuted) {
      btn.classList.add('muted');
      btn.textContent = '🔇';
      btn.title = 'Microphone muted — click to unmute';
    } else {
      btn.textContent = '🎤';
      btn.title = 'Microphone active — click to mute';
    }
  }

  async _hangup() {
    // Mark this as a user-initiated hangup so the retry/reconnect scheduler
    // (and the connectionstatechange reconnect path) bail out — hanging up
    // must stay hung up until the user re-opens or a fresh doorbell ring.
    this._userHungUp = true;
    this._cleanup();
    this._applyState('idle');
    try {
      await this._hass.callService('camera', 'turn_off', {
        entity_id: this._config.entity,
      });
    } catch (_) { /* ignore */ }
    // NOTE: intentionally no _scheduleRetry here — see _userHungUp above.
  }

  // ── Cleanup ───────────────────────────────────────────────────────────────────

  _cleanup() {
    // Invalidate any in-flight _connect(): bumping the epoch makes the resumed
    // _connect() see a mismatch after its next await and tear down (close PC /
    // call unsub) anything it created instead of leaking it.
    this._connectEpoch++;

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

    // Sync mic button to post-cleanup state
    this._updateMicButton();
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
