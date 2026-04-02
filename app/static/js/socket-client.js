const MAX_RECONNECT_DELAY_MS = 5000;
const INITIAL_RECONNECT_DELAY_MS = 800;

export function createSocketClient({ url, onMessage, onOpen, onStateChange }) {
  let socket = null;
  let reconnectTimer = null;
  let reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
  let manuallyClosed = false;

  const setState = (state) => {
    onStateChange?.(state);
  };

  const clearReconnect = () => {
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  const connect = () => {
    clearReconnect();
    setState(socket ? "reconnecting" : "connecting");
    socket = new WebSocket(url);

    socket.addEventListener("open", () => {
      reconnectDelay = INITIAL_RECONNECT_DELAY_MS;
      setState("connected");
      onOpen?.();
    });

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        onMessage?.(payload);
      } catch (_error) {
        // Ignore malformed payloads and keep the socket alive.
      }
    });

    socket.addEventListener("close", () => {
      if (manuallyClosed) {
        setState("offline");
        return;
      }
      setState("reconnecting");
      clearReconnect();
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, reconnectDelay);
      reconnectDelay = Math.min(Math.round(reconnectDelay * 1.5), MAX_RECONNECT_DELAY_MS);
    });

    socket.addEventListener("error", () => {
      setState("reconnecting");
    });
  };

  connect();

  return {
    send(payload) {
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        return false;
      }
      socket.send(JSON.stringify(payload));
      return true;
    },
    close() {
      manuallyClosed = true;
      clearReconnect();
      socket?.close();
    },
    get readyState() {
      return socket?.readyState ?? WebSocket.CLOSED;
    },
  };
}
