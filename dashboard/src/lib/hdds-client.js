/**
 * HDDS WebSocket Client for aIRCp Dashboard
 * 
 * Connects to hdds-ws bridge (port 9090) and manages DDS topic subscriptions.
 * Single WS connection, multiple topic subscriptions.
 * 
 * Topics:
 *   aircp/messages/{room}  — Chat messages (subscribe + publish)
 *   aircp/presence          — Agent heartbeats (subscribe)
 *   aircp/tasks             — Task updates (subscribe)
 *   aircp/workflows         — Workflow state (subscribe)
 *   aircp/mode              — Coordination mode (subscribe)
 *   aircp/commands          — Dashboard → daemon commands (publish only)
 */

const RECONNECT_DELAYS = [1000, 2000, 5000, 10000, 30000];

export class HddsClient {
  constructor(url = 'ws://localhost:9090/ws') {
    this.url = url;
    this.ws = null;
    this.state = 'disconnected'; // disconnected | connecting | connected
    this.domain = null;
    this.reconnectAttempt = 0;
    this.reconnectTimer = null;
    this.subscriptions = new Map();   // topic → { id, handlers: Set<fn> }
    this.pendingSubs = [];            // topics to subscribe on reconnect
    this.listeners = new Map();       // event → Set<fn>
  }

  // --- Connection ---

  connect() {
    if (this.ws) return;
    this._setState('connecting');

    try {
      this.ws = new WebSocket(this.url);
      this.ws.onopen = () => this._onOpen();
      this.ws.onmessage = (e) => this._onMessage(e);
      this.ws.onclose = (e) => this._onClose(e);
      this.ws.onerror = () => {}; // onclose will fire
    } catch (err) {
      this._scheduleReconnect();
    }
  }

  disconnect() {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    if (this.ws) {
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    this._setState('disconnected');
  }

  // --- Pub/Sub ---

  subscribe(topic, handler, qos) {
    let sub = this.subscriptions.get(topic);
    if (!sub) {
      sub = { id: null, handlers: new Set() };
      this.subscriptions.set(topic, sub);
    }
    sub.handlers.add(handler);

    if (this.state === 'connected') {
      this._sendSubscribe(topic, qos);
    }

    return () => {
      sub.handlers.delete(handler);
      if (sub.handlers.size === 0) {
        this.subscriptions.delete(topic);
        if (this.state === 'connected') {
          this._send({ type: 'unsubscribe', topic });
        }
      }
    };
  }

  publish(topic, data) {
    if (this.state !== 'connected') return false;
    this._send({ type: 'publish', topic, data });
    return true;
  }

  listTopics() {
    if (this.state !== 'connected') return;
    this._send({ type: 'list_topics' });
  }

  // --- Events ---

  on(event, fn) {
    if (!this.listeners.has(event)) this.listeners.set(event, new Set());
    this.listeners.get(event).add(fn);
    return () => this.listeners.get(event)?.delete(fn);
  }

  _emit(event, data) {
    this.listeners.get(event)?.forEach(fn => fn(data));
  }

  // --- Internals ---

  _setState(s) {
    this.state = s;
    this._emit('state', s);
  }

  _send(msg) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  _sendSubscribe(topic, qos) {
    const msg = { type: 'subscribe', topic };
    if (qos) msg.qos = qos;
    this._send(msg);
  }

  _onOpen() {
    this.reconnectAttempt = 0;
    // Re-subscribe to all tracked topics
    for (const topic of this.subscriptions.keys()) {
      this._sendSubscribe(topic);
    }
    // Start keepalive
    this._pingInterval = setInterval(() => {
      this._send({ type: 'ping', id: Date.now() });
    }, 30000);
  }

  _onMessage(event) {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }

    switch (msg.type) {
      case 'welcome':
        this.domain = msg.domain;
        this._setState('connected');
        this._emit('welcome', msg);
        break;

      case 'subscribed':
        const sub = this.subscriptions.get(msg.topic);
        if (sub) sub.id = msg.subscription_id;
        break;

      case 'data':
        const s = this.subscriptions.get(msg.topic);
        if (s) {
          const sample = this._unwrapSample(msg.sample);
          s.handlers.forEach(fn => fn(sample, msg.info));
        }
        this._emit('data', msg);
        break;

      case 'published':
        this._emit('published', msg);
        break;

      case 'topics':
        this._emit('topics', msg.topics);
        break;

      case 'pong':
        break;

      case 'error':
        console.warn(`[hdds-ws] ${msg.code}: ${msg.message}`);
        this._emit('error', msg);
        break;
    }
  }

  _onClose(event) {
    this.ws = null;
    clearInterval(this._pingInterval);
    this._setState('disconnected');
    this._scheduleReconnect();
  }

  /**
   * Unwrap hdds-ws sample envelope.
   * hdds-ws sends: { _raw: "base64...", data: "{json_string}" }
   * Returns parsed JSON or original sample if no envelope.
   */
  _unwrapSample(sample) {
    if (sample && sample.data && typeof sample.data === 'string') {
      try { return JSON.parse(sample.data); } catch { return sample; }
    }
    return sample;
  }

  _scheduleReconnect() {
    const delay = RECONNECT_DELAYS[Math.min(this.reconnectAttempt, RECONNECT_DELAYS.length - 1)];
    this.reconnectAttempt++;
    this._setState('connecting');
    this.reconnectTimer = setTimeout(() => {
      this.ws = null;
      this.connect();
    }, delay);
  }
}

// Singleton
export const hdds = new HddsClient();
