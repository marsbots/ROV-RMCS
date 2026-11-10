/* ROVOps pilot console — no build step, no framework. */

const $ = (id) => document.getElementById(id);

const state = {
  base: defaultBase(),
  token: null,
  user: null,
  role: null,
  vehicles: [],
  vehicleId: null,
  sessionId: null,
  session: null,
  sessionState: null,
  telemetry: {},
  video: {},
  vehicleOnline: false,
  pilotId: null,
  leaseHolder: null,
  leased: false,
  estop: false,
  commandTtl: 500,
  ws: null,
  wsReady: false,
  closedByUs: false,
  reconnectTimer: null,
  reconnectDelay: 1000,
  pingTimer: null,
  manualTimer: null,
  wasSending: false,
  padActive: false,
  padX: 0,
  padY: 0,
  keys: new Set(),
  z: 500,
  lastRtt: null,
  whep: null,
  whepUrl: null,
  whepRetry: null,
  loginError: "",
};

function defaultBase() {
  if (location.protocol === "http:" || location.protocol === "https:") return location.origin;
  return "http://localhost:8080";
}

function baseUrl() {
  return state.base.replace(/\/+$/, "");
}

class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(baseUrl() + path, { ...options, headers });
  let body = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }
  if (!res.ok) {
    const detail = body && typeof body === "object" ? body.detail : body;
    throw new ApiError(res.status, typeof detail === "string" ? detail : res.statusText);
  }
  return body;
}

/* ------------------------------------------------------------------ */
/* boot / auth                                                          */
/* ------------------------------------------------------------------ */

async function init() {
  const savedBase = localStorage.getItem("rovops.base");
  if (savedBase) state.base = savedBase;
  $("server-url").value = baseUrl();

  const token = sessionStorage.getItem("rovops.token");
  if (token) {
    state.token = token;
    try {
      await bootstrap();
      return;
    } catch {
      state.token = null;
      sessionStorage.removeItem("rovops.token");
    }
  }
  showLogin();
}

function showLogin() {
  $("login-view").hidden = false;
  $("app-view").hidden = true;
  $("password").focus();
}

async function bootstrap() {
  const me = await api("/api/me");
  state.user = me.user;
  state.role = me.role;
  state.token = sessionStorage.getItem("rovops.token") || state.token;
  $("user-label").textContent = `${state.user}${state.role === "admin" ? " · admin" : ""}`;
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  await loadVehicles();
  startManualLoop();
}

async function login(event) {
  event.preventDefault();
  $("login-error").textContent = "";
  state.base = $("server-url").value.trim() || defaultBase();
  localStorage.setItem("rovops.base", state.base);
  try {
    const result = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: $("username").value.trim(),
        password: $("password").value,
      }),
    });
    state.token = result.token;
    sessionStorage.setItem("rovops.token", result.token);
    await bootstrap();
  } catch (err) {
    $("login-error").textContent =
      err instanceof ApiError && err.status === 401 ? "Invalid username or password." : `Sign-in failed: ${err.message}`;
  }
}

function logout() {
  closeSocket();
  stopPad(true);
  stopWhep();
  state.token = null;
  state.user = null;
  state.sessionId = null;
  state.session = null;
  sessionStorage.removeItem("rovops.token");
  $("app-view").hidden = true;
  $("login-view").hidden = false;
}

/* ------------------------------------------------------------------ */
/* vehicles / session                                                   */
/* ------------------------------------------------------------------ */

async function loadVehicles() {
  const data = await api("/api/vehicles");
  state.vehicles = data.vehicles || [];
  renderVehicles();
}

function renderVehicles() {
  const list = $("vehicle-list");
  list.innerHTML = "";
  if (!state.vehicles.length) {
    list.innerHTML = '<p class="muted">No vehicles provisioned.</p>';
    return;
  }
  for (const vehicle of state.vehicles) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "vehicle" + (vehicle.id === state.vehicleId ? " active" : "");
    const telemetry = state.vehicleId === vehicle.id ? state.telemetry : vehicle.telemetry || {};
    const online = state.vehicleId === vehicle.id ? state.vehicleOnline : vehicle.online;
    button.innerHTML = `
      <span class="name">${escapeHtml(vehicle.name || vehicle.id)} <span class="dot ${online ? "online" : ""}"></span></span>
      <span class="meta">${escapeHtml(vehicle.model || "unknown")} · ${escapeHtml(vehicle.site || "no site")}</span>
      <span class="meta mono">${escapeHtml(vehicle.id)} ${telemetry.mode ? "· " + escapeHtml(telemetry.mode) : ""}</span>
    `;
    button.addEventListener("click", () => joinSession(vehicle.id));
    list.appendChild(button);
  }
}

async function joinSession(vehicleId) {
  if (!vehicleId) return;
  if (state.sessionId && state.vehicleId === vehicleId && state.ws) return;
  closeSocket();
  stopPad(true);
  state.vehicleId = vehicleId;
  state.sessionId = null;
  state.session = null;
  state.sessionState = null;
  state.telemetry = {};
  state.video = {};
  state.vehicleOnline = false;
  renderVehicles();
  updateSessionInfo("Opening session…");
  try {
    const info = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ vehicle_id: vehicleId }),
    });
    state.sessionId = info.session_id;
    state.commandTtl = info.command_ttl_ms || 500;
    state.telemetry = info.vehicle?.telemetry || {};
    updateTelemetry();
    updateSessionInfo(`Session <b class="mono">${info.session_id}</b><br>Connecting…`);
    connectSocket(info);
  } catch (err) {
    updateSessionInfo(`Could not open session: ${escapeHtml(err.message)}`);
    toast(`Could not open session: ${err.message}`, "error");
  }
}

function wsUrlFor(info) {
  let url;
  const raw = info.ws_url || "";
  if (/^wss?:\/\//i.test(raw)) {
    url = new URL(raw);
  } else {
    url = new URL(raw || `${location.pathname}`, baseUrl());
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  }
  url.searchParams.set("token", info.token);
  return url.toString();
}

function connectSocket(info) {
  closeSocket();
  state.closedByUs = false;
  const url = wsUrlFor(info);
  setNet("connecting", "offline");
  let ws;
  try {
    ws = new WebSocket(url);
  } catch (err) {
    toast(`WebSocket error: ${err.message}`, "error");
    scheduleReconnect();
    return;
  }
  state.ws = ws;

  ws.addEventListener("open", () => {
    state.wsReady = true;
    state.reconnectDelay = 1000;
    setNet("connected", "online");
    send({ type: "ping", ts: Date.now() });
    clearInterval(state.pingTimer);
    state.pingTimer = setInterval(() => send({ type: "ping", ts: Date.now() }), 5000);
    renderControls();
  });

  ws.addEventListener("message", (event) => {
    if (typeof event.data !== "string") return;
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    handleServerMessage(message);
  });

  ws.addEventListener("close", (event) => {
    state.wsReady = false;
    state.ws = null;
    clearInterval(state.pingTimer);
    setNet("disconnected", "offline");
    renderControls();
    if (state.closedByUs) return;
    if (event.code === 4401 || event.code === 4404) {
      toast("Session token rejected — re-opening session…", "warn");
      refreshSession();
      return;
    }
    scheduleReconnect();
  });

  ws.addEventListener("error", () => {
    /* close handler drives reconnect */
  });
}

function closeSocket() {
  state.closedByUs = true;
  clearTimeout(state.reconnectTimer);
  clearInterval(state.pingTimer);
  const ws = state.ws;
  state.ws = null;
  state.wsReady = false;
  if (ws) {
    try {
      ws.close();
    } catch {
      /* ignore */
    }
  }
}

function scheduleReconnect() {
  if (state.closedByUs || !state.sessionId) return;
  clearTimeout(state.reconnectTimer);
  const delay = state.reconnectDelay;
  state.reconnectDelay = Math.min(state.reconnectDelay * 2, 15000);
  updateSessionInfo(`Disconnected. Reconnecting in ${Math.round(delay / 1000)}s…`);
  state.reconnectTimer = setTimeout(async () => {
    if (!state.sessionId) return;
    try {
      const info = await createSessionForVehicle(state.vehicleId);
      state.sessionId = info.session_id;
      connectSocket(info);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        toast("Sign-in expired. Please sign in again.", "error");
        logout();
        return;
      }
      scheduleReconnect();
    }
  }, delay);
}

async function refreshSession() {
  if (!state.vehicleId) return;
  try {
    const info = await createSessionForVehicle(state.vehicleId);
    state.sessionId = info.session_id;
    connectSocket(info);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      toast("Sign-in expired. Please sign in again.", "error");
      logout();
      return;
    }
    toast(`Could not re-open session: ${err.message}`, "error");
    scheduleReconnect();
  }
}

function createSessionForVehicle(vehicleId) {
  return api("/api/sessions", {
    method: "POST",
    body: JSON.stringify({ vehicle_id: vehicleId }),
  });
}

function handleServerMessage(message) {
  switch (message.type) {
    case "session_state":
      state.sessionState = message.session;
      state.vehicleOnline = Boolean(message.vehicle?.online);
      if (message.vehicle?.telemetry) state.telemetry = message.vehicle.telemetry;
      state.video = message.video || {};
      syncSession(message.session);
      if (message.vehicle) renderVehicles();
      updateTelemetry();
      updateVideo();
      break;

    case "state":
      syncSession(message.session);
      break;

    case "lease":
      if (message.granted) toast("Control granted.", "ok");
      else if (message.reason === "held_by_another_pilot") toast("Another pilot currently has control.", "warn");
      else if (message.reason === "released") toast("Control released.");
      break;

    case "telemetry":
      state.telemetry = message.data || {};
      state.vehicleOnline = Boolean(state.telemetry.mavlink_online ?? state.vehicleOnline);
      updateTelemetry();
      renderVehicles();
      break;

    case "vehicle":
      state.vehicleOnline = Boolean(message.online);
      if (message.vehicle?.telemetry) state.telemetry = message.vehicle.telemetry;
      updateTelemetry();
      renderVehicles();
      if (!message.online) {
        updateSessionInfo("Vehicle offline — commands are blocked.");
        stopPad(true);
      } else {
        updateSessionInfo();
      }
      break;

    case "vehicle_heartbeat":
      state.vehicleOnline = true;
      updateTelemetry();
      break;

    case "estop":
      state.estop = Boolean(message.active);
      updateEstopUi();
      renderControls();
      if (state.estop) {
        stopPad(true);
        toast(`E-STOP latched${message.by ? " by " + message.by : ""}.`, "error");
      } else {
        toast("E-stop reset. Vehicle still requires a fresh command.", "warn");
      }
      break;

    case "estop_ack":
      break;

    case "pilot_joined":
      toast(`${message.pilot.user} joined the session.`);
      break;

    case "pilot_left":
      break;

    case "pong": {
      if (typeof message.ts === "number") state.lastRtt = Math.max(0, Date.now() - message.ts);
      $("latency").textContent = state.lastRtt === null ? "—" : `${state.lastRtt} ms`;
      break;
    }

    case "error":
      toast(message.message || message.code || "Server error", "error");
      if (message.code === "session_expired" || message.code === "session_closed") {
        closeSocket();
        state.sessionId = null;
        state.session = null;
        state.leased = false;
        updateSessionInfo("Session ended. Select a vehicle to start a new one.");
        renderControls();
      }
      break;

    default:
      break;
  }
}

function syncSession(session) {
  if (!session) return;
  state.sessionState = session;
  state.pilotId = session.you;
  state.leaseHolder = session.lease_holder;
  state.leased = Boolean(session.you_have_lease);
  state.estop = Boolean(session.estop);
  state.commandTtl = session.command_ttl_ms || state.commandTtl;
  renderPilots(session);
  updateEstopUi();
  renderControls();
  updateSessionInfo();
}

function renderPilots(session) {
  const list = $("pilot-list");
  list.innerHTML = "";
  const pilots = session.pilots || [];
  if (!pilots.length) {
    list.innerHTML = '<li class="muted">No pilots connected</li>';
    return;
  }
  for (const pilot of pilots) {
    const li = document.createElement("li");
    const label = pilot.user + (pilot.id === session.you ? " (you)" : "");
    li.innerHTML = `<span>${escapeHtml(label)}</span>${
      pilot.is_controller ? '<span class="controller">control</span>' : ""
    }`;
    list.appendChild(li);
  }
}

function updateSessionInfo(override) {
  const el = $("session-info");
  if (override !== undefined) {
    el.innerHTML = override;
    return;
  }
  if (!state.sessionId) {
    el.textContent = "Select a vehicle to open a session.";
    return;
  }
  const expires = state.sessionState?.expires_at
    ? new Date(state.sessionState.expires_at * 1000).toLocaleTimeString()
    : "—";
  el.innerHTML = `
    Session <b class="mono">${escapeHtml(state.sessionId)}</b><br>
    Vehicle <span class="${state.vehicleOnline ? "ok" : "muted"}">${state.vehicleOnline ? "online" : "offline"}</span>
    · expires ${escapeHtml(expires)}<br>
    <span class="muted">Commands neutral after ${state.commandTtl} ms without input.</span>
  `;
}

/* ------------------------------------------------------------------ */
/* controls                                                             */
/* ------------------------------------------------------------------ */

function renderControls() {
  const connected = state.wsReady;
  const hasSession = Boolean(state.sessionId);
  const canControl = state.leased && !state.estop && connected && state.vehicleOnline;

  $("take-control").disabled = !(connected && hasSession && !state.leased);
  $("release-control").disabled = !(connected && state.leased);
  $("estop").disabled = !(connected && hasSession);
  $("estop-reset").disabled = !(connected && state.leased && state.estop);
  $("arm").disabled = !canControl;
  $("disarm").disabled = !(connected && state.leased);
  $("mode").disabled = !canControl;
  $("pad").classList.toggle("locked", !canControl);

  const leasePill = $("lease-pill");
  if (state.estop) {
    leasePill.textContent = "E-STOP latched";
    leasePill.className = "pill offline";
  } else if (state.leased) {
    leasePill.textContent = "you have control";
    leasePill.className = "pill lease";
  } else if (state.leaseHolder) {
    leasePill.textContent = "held by another pilot";
    leasePill.className = "pill";
  } else {
    leasePill.textContent = "no control";
    leasePill.className = "pill";
  }
}

function updateEstopUi() {
  const button = $("estop");
  button.textContent = state.estop ? "E-STOP ACTIVE" : "E-STOP";
  button.classList.toggle("active", state.estop);
  $("hud-estop").hidden = !state.estop;
  renderControls();
}

async function acquireLease() {
  send({ type: "lease_acquire" });
}

async function releaseLease() {
  stopPad(true);
  send({ type: "lease_release" });
}

function triggerEstop() {
  if (!state.wsReady) return;
  stopPad(true);
  send({ type: "estop" });
}

function resetEstop() {
  if (!state.leased) return;
  if (!confirm("Reset the emergency stop? Make sure the area around the ROV is clear.")) return;
  send({ type: "estop_reset" });
}

function armVehicle() {
  if (!state.leased || state.estop) return;
  if (!confirm("Arm the thrusters? The ROV can move immediately.")) return;
  send({ type: "arm", arm: true });
}

function disarmVehicle() {
  if (!state.leased) return;
  send({ type: "arm", arm: false });
}

function changeMode(event) {
  const mode = event.target.value;
  if (!state.leased || state.estop) return;
  send({ type: "mode", mode });
}

/* ------------------------------------------------------------------ */
/* manual control                                                       */
/* ------------------------------------------------------------------ */

function send(payload) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return false;
  try {
    state.ws.send(JSON.stringify(payload));
    return true;
  } catch {
    return false;
  }
}

function inputVector() {
  let x = 0;
  let y = 0;
  const keys = state.keys;
  if (keys.has("KeyW") || keys.has("ArrowUp")) x += 1;
  if (keys.has("KeyS") || keys.has("ArrowDown")) x -= 1;
  if (keys.has("KeyD")) y += 1;
  if (keys.has("KeyA")) y -= 1;
  if (state.padActive) {
    x += -state.padY;
    y += state.padX;
  }
  const length = Math.hypot(x, y);
  if (length > 1) {
    x /= length;
    y /= length;
  }
  const yaw = (keys.has("KeyE") ? 1 : 0) - (keys.has("KeyQ") ? 1 : 0);
  return { x, y, yaw };
}

function hasInput() {
  const { x, y, yaw } = inputVector();
  return state.padActive || Math.abs(x) > 0.001 || Math.abs(y) > 0.001 || yaw !== 0 ||
    state.keys.has("KeyR") || state.keys.has("KeyF");
}

function startManualLoop() {
  clearInterval(state.manualTimer);
  state.manualTimer = setInterval(manualTick, 100);
}

function manualTick() {
  const keys = state.keys;
  if (keys.has("KeyR")) state.z = Math.min(1000, state.z + 30);
  if (keys.has("KeyF")) state.z = Math.max(0, state.z - 30);
  $("vertical").value = String(state.z);
  $("z-value").textContent = String(state.z);

  const allowed = state.wsReady && state.leased && !state.estop && state.vehicleOnline;
  if (!allowed) {
    if (state.wasSending) sendNeutral();
    state.wasSending = false;
    return;
  }

  if (!hasInput()) {
    if (state.wasSending) sendNeutral();
    state.wasSending = false;
    return;
  }

  const { x, y, yaw } = inputVector();
  send({
    type: "manual",
    x: Math.round(x * 1000),
    y: Math.round(y * 1000),
    z: Math.round(state.z),
    r: Math.round(yaw * 600),
    buttons: 0,
  });
  state.wasSending = true;
}

function sendNeutral() {
  if (!state.wsReady) return;
  send({ type: "manual", x: 0, y: 0, z: 500, r: 0, buttons: 0 });
}

function stopPad(neutral = false) {
  state.padActive = false;
  state.padX = 0;
  state.padY = 0;
  $("pad").classList.remove("active");
  $("pad-knob").style.transform = "translate(0px, 0px)";
  if (neutral) {
    sendNeutral();
    state.wasSending = false;
  }
}

function setupPad() {
  const pad = $("pad");
  const knob = $("pad-knob");

  const updateFromPointer = (event) => {
    const rect = pad.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    let dx = (event.clientX - cx) / (rect.width * 0.38);
    let dy = (event.clientY - cy) / (rect.height * 0.38);
    const length = Math.hypot(dx, dy);
    if (length > 1) {
      dx /= length;
      dy /= length;
    }
    if (Math.hypot(dx, dy) < 0.08) {
      dx = 0;
      dy = 0;
    }
    state.padX = dx;
    state.padY = dy;
    const max = rect.width * 0.38;
    knob.style.transform = `translate(${dx * max}px, ${dy * max}px)`;
  };

  pad.addEventListener("pointerdown", (event) => {
    if (!state.leased || state.estop || !state.wsReady || !state.vehicleOnline) return;
    event.preventDefault();
    pad.setPointerCapture(event.pointerId);
    state.padActive = true;
    pad.classList.add("active");
    updateFromPointer(event);
  });

  pad.addEventListener("pointermove", (event) => {
    if (!state.padActive) return;
    event.preventDefault();
    updateFromPointer(event);
  });

  const end = (event) => {
    if (!state.padActive) return;
    event.preventDefault();
    stopPad(true);
  };
  pad.addEventListener("pointerup", end);
  pad.addEventListener("pointercancel", end);
  pad.addEventListener("lostpointercapture", () => stopPad(true));
}

function setupKeyboard() {
  const controlKeys = new Set([
    "KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE", "KeyR", "KeyF",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
  ]);
  const isTyping = (target) =>
    target instanceof HTMLElement &&
    (target.tagName === "INPUT" || target.tagName === "SELECT" || target.tagName === "TEXTAREA" || target.isContentEditable);

  window.addEventListener("keydown", (event) => {
    if (isTyping(event.target)) return;
    if (event.code === "Escape") {
      stopPad(true);
      return;
    }
    if (!controlKeys.has(event.code)) return;
    event.preventDefault();
    state.keys.add(event.code);
    if (event.code === "ArrowLeft") state.keys.add("KeyA");
    if (event.code === "ArrowRight") state.keys.add("KeyD");
  });

  window.addEventListener("keyup", (event) => {
    state.keys.delete(event.code);
    if (event.code === "ArrowLeft") state.keys.delete("KeyA");
    if (event.code === "ArrowRight") state.keys.delete("KeyD");
  });

  window.addEventListener("blur", () => {
    state.keys.clear();
    stopPad(true);
  });
}

/* ------------------------------------------------------------------ */
/* telemetry / video                                                    */
/* ------------------------------------------------------------------ */

function updateTelemetry() {
  const t = state.telemetry || {};
  $("t-depth").textContent = t.depth_m == null ? "—" : `${Number(t.depth_m).toFixed(1)} m`;
  $("t-heading").textContent = t.heading == null ? "—" : `${Math.round(t.heading)}°`;
  const battery = t.battery || {};
  const volts = battery.voltage == null ? "—" : `${Number(battery.voltage).toFixed(1)} V`;
  const pct = battery.remaining == null ? "" : ` · ${battery.remaining}%`;
  $("t-battery").textContent = volts + pct;
  const attitude = t.attitude || {};
  $("t-attitude").textContent =
    attitude.roll == null ? "—" : `R ${Number(attitude.roll).toFixed(0)}° · P ${Number(attitude.pitch).toFixed(0)}°`;
  $("t-throttle").textContent = t.throttle == null ? "—" : `${Number(t.throttle).toFixed(0)}%`;
  $("t-link").textContent = t.mavlink_online ? `online${state.lastRtt != null ? " · " + state.lastRtt + " ms" : ""}` : "offline";
  $("cmd-age").textContent = t.command_age_ms == null ? "—" : `${t.command_age_ms} ms`;
  $("frames").textContent =
    t.frames_accepted == null ? "—" : `${t.frames_accepted} / ${t.frames_dropped ?? 0}`;

  $("hud-mode").textContent = `MODE ${t.mode || "—"}`;
  const armed = Boolean(t.armed);
  const armedChip = $("hud-armed");
  armedChip.textContent = armed ? "ARMED" : "DISARMED";
  armedChip.classList.toggle("armed", armed);
  if (t.mode) {
    const select = $("mode");
    if ([...select.options].some((o) => o.value === t.mode)) select.value = t.mode;
  }
}

function updateVideo() {
  const url = state.video.whep_url || "";
  if (!url) {
    $("video-empty").hidden = false;
    $("video-empty").querySelector("div:nth-child(2)").textContent = "No video stream";
    return;
  }
  if (url === state.whepUrl && state.whep) return;
  startWhep(url);
}

async function startWhep(url) {
  stopWhep();
  state.whepUrl = url;
  const absolute = resolveMediaUrl(url);
  const video = $("video");
  const pc = new RTCPeerConnection({ iceServers: [] });
  state.whep = pc;
  const stream = new MediaStream();
  video.srcObject = stream;

  pc.ontrack = (event) => {
    stream.addTrack(event.track);
    video.classList.add("live");
    $("video-empty").hidden = true;
  };
  pc.onconnectionstatechange = () => {
    if (["failed", "disconnected", "closed"].includes(pc.connectionState)) {
      if (state.whep === pc) {
        video.classList.remove("live");
        scheduleWhepRetry(url);
      }
    }
  };

  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const result = await fetch(absolute, {
      method: "POST",
      headers: { "Content-Type": "application/sdp", Accept: "application/sdp" },
      body: offer.sdp,
    });
    if (!result.ok) throw new Error(`WHEP ${result.status}`);
    const answer = await result.text();
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
  } catch (err) {
    $("video-empty").hidden = false;
    $("video-empty").querySelector("div:nth-child(2)").textContent = "Video unavailable";
    $("video-empty").querySelector("small").textContent = err.message;
    scheduleWhepRetry(url);
  }
}

function scheduleWhepRetry(url) {
  clearTimeout(state.whepRetry);
  state.whepRetry = setTimeout(() => {
    if (state.video.whep_url === url) startWhep(url);
  }, 5000);
}

function stopWhep() {
  clearTimeout(state.whepRetry);
  state.whepRetry = null;
  const pc = state.whep;
  state.whep = null;
  state.whepUrl = null;
  if (pc) {
    try {
      pc.close();
    } catch {
      /* ignore */
    }
  }
  const video = $("video");
  video.srcObject = null;
  video.classList.remove("live");
}

function resolveMediaUrl(url) {
  try {
    return new URL(url, baseUrl()).toString();
  } catch {
    return url;
  }
}

/* ------------------------------------------------------------------ */
/* misc                                                                 */
/* ------------------------------------------------------------------ */

function setNet(label, cls) {
  const pill = $("net-pill");
  pill.textContent = label;
  pill.className = `pill ${cls}`;
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/* ------------------------------------------------------------------ */
/* wiring                                                               */
/* ------------------------------------------------------------------ */

$("login-form").addEventListener("submit", login);
$("logout").addEventListener("click", logout);
$("take-control").addEventListener("click", acquireLease);
$("release-control").addEventListener("click", releaseLease);
$("estop").addEventListener("click", triggerEstop);
$("estop-reset").addEventListener("click", resetEstop);
$("arm").addEventListener("click", armVehicle);
$("disarm").addEventListener("click", disarmVehicle);
$("mode").addEventListener("change", changeMode);
$("vertical").addEventListener("input", (event) => {
  state.z = Number(event.target.value);
  $("z-value").textContent = String(state.z);
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    state.keys.clear();
    stopPad(true);
  }
});
window.addEventListener("beforeunload", () => {
  stopPad(false);
  closeSocket();
});

setupPad();
setupKeyboard();
init();
