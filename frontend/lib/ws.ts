/**
 * WebSocket helper for live control-plane updates.
 *
 * The backend exposes (or will expose) a `/ws` endpoint that pushes
 * room-state, approval, and event deltas so the operator UI does not
 * have to poll aggressively. This module is a deliberately minimal
 * client: connect, auto-reconnect with backoff, dispatch typed messages
 * to subscribers. It is a *stub* in the Phase-10 sense — the dashboard
 * works on TanStack Query polling alone; a live WS feed is an
 * enhancement that can be wired in without touching callers.
 *
 * Ingress note: like the REST client, the URL is built relative to the
 * current document so the HA Ingress session prefix is preserved. The
 * scheme is upgraded `http(s)` -> `ws(s)`.
 */

/** A message pushed from the backend over the live channel. */
export interface WsMessage {
  /** Discriminator — e.g. `room_state`, `approval`, `event`. */
  type: string;
  /** Optional room scope. */
  room_id?: string;
  /** Arbitrary payload; consumers narrow on `type`. */
  payload?: unknown;
}

export type WsListener = (msg: WsMessage) => void;
export type WsStatus = "connecting" | "open" | "closed";
export type WsStatusListener = (status: WsStatus) => void;

/** Build the `ws(s)://` URL for the live endpoint, ingress-relative. */
export function wsUrl(path = "/ws"): string {
  if (typeof window === "undefined") return path;
  const base =
    process.env.NEXT_PUBLIC_API_BASE?.replace(/\/+$/, "") ||
    window.location.href;
  const u = new URL(path.replace(/^\//, ""), base);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  return u.toString();
}

/** Reconnecting WebSocket client. Construct, {@link connect}, subscribe. */
export class LiveClient {
  private socket: WebSocket | null = null;
  private readonly listeners = new Set<WsListener>();
  private readonly statusListeners = new Set<WsStatusListener>();
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private closedByUser = false;

  constructor(private readonly path: string = "/ws") {}

  /** Open the connection (no-op if already open / connecting). */
  connect(): void {
    if (typeof window === "undefined") return;
    if (this.socket && this.socket.readyState <= WebSocket.OPEN) return;
    this.closedByUser = false;
    this.emitStatus("connecting");

    try {
      this.socket = new WebSocket(wsUrl(this.path));
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.socket.onopen = () => {
      this.reconnectAttempts = 0;
      this.emitStatus("open");
    };
    this.socket.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as WsMessage;
        if (msg && typeof msg.type === "string") {
          for (const l of this.listeners) l(msg);
        }
      } catch {
        // Ignore non-JSON frames — the server may send keep-alives.
      }
    };
    this.socket.onclose = () => {
      this.emitStatus("closed");
      if (!this.closedByUser) this.scheduleReconnect();
    };
    this.socket.onerror = () => {
      this.socket?.close();
    };
  }

  /** Subscribe to messages; returns an unsubscribe function. */
  subscribe(listener: WsListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Subscribe to connection-status changes; returns unsubscribe. */
  onStatus(listener: WsStatusListener): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  /** Send a JSON message if the socket is open. */
  send(msg: unknown): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(msg));
    }
  }

  /** Permanently close the connection (no reconnect). */
  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.socket?.close();
    this.socket = null;
  }

  private emitStatus(status: WsStatus): void {
    for (const l of this.statusListeners) l(status);
  }

  private scheduleReconnect(): void {
    if (this.closedByUser) return;
    this.reconnectAttempts += 1;
    // Exponential backoff capped at 30s.
    const delay = Math.min(30_000, 1000 * 2 ** (this.reconnectAttempts - 1));
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }
}

/** Process-wide singleton, lazily created. */
let singleton: LiveClient | null = null;

/** Get (and lazily create) the shared {@link LiveClient}. */
export function getLiveClient(): LiveClient {
  if (!singleton) singleton = new LiveClient();
  return singleton;
}
