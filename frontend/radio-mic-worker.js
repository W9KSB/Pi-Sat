'use strict';

const MIC_MAGIC = new Uint8Array([0x50, 0x49, 0x53, 0x41, 0x54, 0x4d, 0x49, 0x43]);
const MIC_VERSION = 1;
const MIC_HEADER_BYTES = 64;
const CLOCK_REQUEST_INTERVAL_MS = 2000;
const CLOCK_MAX_AGE_MS = 5000;
const CLOCK_MAX_RTT_MS = 250;
const FRAME_MAX_AGE_MS = 100;
const SERVER_SYNC_MAX_AGE_MS = 5000;
const SERVER_SYNC_MAX_RTT_MS = 250;
const CLOCK_REQUEST_TIMEOUT_MS = 750;
const RECONNECT_DELAY_MS = 2000;
const METER_INTERVAL_MS = 120;

let socket = null;
let reconnectTimer = 0;
let clockTimer = 0;
let transportId = 0;
let socketReady = false;
let shuttingDown = false;
let workletPort = null;
let captureGeneration = 0;
let requestedOutputRate = 16000;
let gain = 1;
let txActive = false;
let txGeneration = 0;
let txCaptureFloor = null;
let sequence = 0;
let workletClock = null;
let pendingWorkletClock = null;
let serverClock = null;
let pendingServerClock = null;
let syncRequestId = 0;
let counters = {
  captured: 0,
  sent: 0,
  dropped: 0,
  noPtt: 0,
  noClock: 0,
  stale: 0,
  backpressure: 0,
  legacy: 0,
  clipped: 0,
  peak: 0,
  rms: 0,
};
let statusAt = 0;
let meterAt = 0;
let clientDiagnosticsAt = 0;
let lastUplinkState = null;

function nowMs() {
  return performance.now();
}

function reportStatus(force = false) {
  const at = nowMs();
  if (!force && at - statusAt < 250) return;
  statusAt = at;
  self.postMessage({
    type: 'status',
    healthy: socketReady,
    mode: workletPort ? 'worklet' : 'fallback',
    fullCaptureAge: Boolean(workletPort && workletClock && serverClock),
    metrics: { ...counters },
  });
}

function reportUplinkState(healthy, detail) {
  if (lastUplinkState === healthy && !detail) return;
  lastUplinkState = healthy;
  self.postMessage({ type: 'uplink', healthy, detail });
}

function socketUrl() {
  const scheme = self.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${self.location.host}/api/radio/mic`;
}

function openSocket() {
  if (shuttingDown) return;
  if (socket && [WebSocket.OPEN, WebSocket.CONNECTING].includes(socket.readyState)) return;
  self.clearTimeout(reconnectTimer);
  reconnectTimer = 0;
  const next = new WebSocket(socketUrl());
  socket = next;
  socketReady = false;
  transportId = 0;
  serverClock = null;
  pendingServerClock = null;
  next.binaryType = 'arraybuffer';
  next.onopen = () => {
    if (socket !== next) return;
    reportUplinkState(true, 'connected');
  };
  next.onmessage = event => handleSocketMessage(next, event.data);
  next.onerror = () => reportSocketLoss(next);
  next.onclose = () => reportSocketLoss(next);
}

function reportSocketLoss(candidate) {
  if (socket !== candidate) return;
  socket = null;
  socketReady = false;
  transportId = 0;
  serverClock = null;
  pendingServerClock = null;
  candidate.onopen = null;
  candidate.onmessage = null;
  candidate.onerror = null;
  candidate.onclose = null;
  try { candidate.close(); } catch { /* Socket already closed. */ }
  if (shuttingDown) return;
  reportUplinkState(false, 'reconnecting');
  reportStatus(true);
  self.clearTimeout(reconnectTimer);
  reconnectTimer = self.setTimeout(openSocket, RECONNECT_DELAY_MS);
}

function handleSocketMessage(candidate, data) {
  if (socket !== candidate || typeof data !== 'string') return;
  let message;
  try {
    message = JSON.parse(data);
  } catch {
    return;
  }
  if (message.type === 'mic_ready') {
    const nextTransportId = Number(message.transport_id);
    if (!Number.isInteger(nextTransportId) || nextTransportId <= 0) return;
    transportId = nextTransportId >>> 0;
    requestedOutputRate = Number(message.sample_rate) || requestedOutputRate;
    socketReady = true;
    // A replacement socket must not flush frames captured before its handshake.
    if (txActive) txCaptureFloor = nowMs();
    requestServerClock(true);
    sendClientDiagnostics(true);
    reportUplinkState(true, 'ready');
    reportStatus(true);
    return;
  }
  if (message.type === 'mic_sync') {
    const pending = pendingServerClock;
    if (!pending || Number(message.id) !== pending.id) return;
    const receivedAt = nowMs();
    const t0 = pending.sentAt;
    const t1 = Number(message.t1);
    const t2 = Number(message.t2);
    const roundTrip = receivedAt - t0 - Math.max(0, t2 - t1);
    if (!Number.isFinite(t1) || !Number.isFinite(t2) || roundTrip < 0 || roundTrip > SERVER_SYNC_MAX_RTT_MS) {
      pendingServerClock = null;
      return;
    }
    const offset = ((t1 - t0) + (t2 - receivedAt)) / 2;
    const uncertainty = roundTrip / 2 + Math.max(0, t2 - t1) / 2 + 2;
    serverClock = {
      offset,
      uncertainty,
      receivedAt,
      roundTrip,
    };
    pendingServerClock = null;
    reportStatus(true);
  }
}

function requestServerClock(force = false) {
  if (pendingServerClock && nowMs() - pendingServerClock.sentAt > CLOCK_REQUEST_TIMEOUT_MS) {
    pendingServerClock = null;
  }
  if (!socketReady || !socket || socket.readyState !== WebSocket.OPEN) return;
  if (!force && pendingServerClock) return;
  const sentAt = nowMs();
  const request = { type: 'mic_sync', id: ++syncRequestId, t0: sentAt };
  socket.send(JSON.stringify(request));
  pendingServerClock = { id: request.id, sentAt };
}

function sendClientDiagnostics(force = false) {
  const at = nowMs();
  if (!force && at - clientDiagnosticsAt < 5000) return;
  if (!socketReady || !socket || socket.readyState !== WebSocket.OPEN) return;
  clientDiagnosticsAt = at;
  socket.send(JSON.stringify({
    type: 'mic_diagnostics',
    counters: {
      ...counters,
      idle: counters.noPtt,
      dropped: Math.max(0, counters.dropped - counters.noPtt),
    },
  }));
}

function requestWorkletClock() {
  if (pendingWorkletClock && nowMs() - pendingWorkletClock.sentAt > CLOCK_REQUEST_TIMEOUT_MS) {
    pendingWorkletClock = null;
  }
  if (!workletPort || pendingWorkletClock) return;
  const id = ++syncRequestId;
  pendingWorkletClock = { id, sentAt: nowMs() };
  workletPort.postMessage({ type: 'clock_request', id });
}

function invalidateClocks() {
  workletClock = null;
  pendingWorkletClock = null;
  requestWorkletClock();
}

function toArrayBuffer(value) {
  if (value instanceof ArrayBuffer) return value;
  if (value && value.buffer instanceof ArrayBuffer) {
    return value.buffer.slice(value.byteOffset || 0, (value.byteOffset || 0) + value.byteLength);
  }
  return null;
}

function sendLegacyFrame(data) {
  const activeSocket = socket;
  const pcm = toArrayBuffer(data.pcm);
  if (!socketReady || !activeSocket || activeSocket.readyState !== WebSocket.OPEN || !pcm || !pcm.byteLength) {
    counters.dropped += 1;
    reportStatus();
    return;
  }
  const rate = Number(data.sample_rate) || requestedOutputRate;
  const maxBuffered = Math.max(3200, Math.round(rate * 0.1) * 2);
  if (activeSocket.bufferedAmount > maxBuffered) {
    counters.backpressure += 1;
    counters.dropped += 1;
    reportStatus();
    return;
  }
  activeSocket.send(pcm);
  counters.legacy += 1;
  counters.sent += 1;
  reportStatus();
}

function handleWorkletMessage(data) {
  if (!data) return;
  if (data.type === 'clock_response') {
    const pending = pendingWorkletClock;
    if (!pending || Number(data.id) !== pending.id) return;
    const receivedAt = nowMs();
    const contextFrame = Number(data.context_frame);
    const contextRate = Number(data.sample_rate);
    const roundTrip = receivedAt - pending.sentAt;
    if (!Number.isFinite(contextFrame) || !Number.isFinite(contextRate) || contextRate <= 0
        || roundTrip < 0 || roundTrip > CLOCK_MAX_RTT_MS) {
      pendingWorkletClock = null;
      return;
    }
    workletClock = {
      contextFrame,
      sampleRate: contextRate,
      workerAt: (pending.sentAt + receivedAt) / 2,
      uncertainty: roundTrip / 2 + 2,
      receivedAt,
    };
    pendingWorkletClock = null;
    reportStatus(true);
    return;
  }
  if (data.type === 'pcm') handleCaptureFrame(data);
}

function handleCaptureFrame(data) {
  if (!data) return;
  if (Number(data.capture_generation) !== captureGeneration) {
    counters.dropped += 1;
    reportStatus();
    return;
  }
  counters.captured += 1;
  counters.peak = Math.max(counters.peak, Number(data.peak) || 0);
  counters.rms = Math.max(counters.rms, Number(data.rms) || 0);
  counters.clipped += Number(data.clipped) || 0;
  const meterNow = nowMs();
  if (meterNow - meterAt >= METER_INTERVAL_MS) {
    meterAt = meterNow;
    self.postMessage({ type: 'meter', level: Number(data.level) || 0, peak: Number(data.peak) || 0 });
  }
  if (!txActive || !txGeneration) {
    counters.noPtt += 1;
    counters.dropped += 1;
    reportStatus();
    return;
  }
  if (!data.full_capture_age) {
    sendLegacyFrame(data);
    return;
  }
  const at = nowMs();
  if (!workletClock || at - workletClock.receivedAt > CLOCK_MAX_AGE_MS
      || !serverClock || at - serverClock.receivedAt > SERVER_SYNC_MAX_AGE_MS) {
    counters.noClock += 1;
    counters.dropped += 1;
    requestWorkletClock();
    requestServerClock();
    reportStatus();
    return;
  }
  const frameEnd = Number(data.capture_frame_end);
  if (!Number.isFinite(frameEnd)) {
    counters.noClock += 1;
    counters.dropped += 1;
    reportStatus();
    return;
  }
  const captureWorkerAt = workletClock.workerAt
    + (frameEnd - workletClock.contextFrame) / workletClock.sampleRate * 1000;
  const sampleRate = Number(data.sample_rate) || requestedOutputRate;
  const pcm = toArrayBuffer(data.pcm);
  const durationMs = pcm ? pcm.byteLength / 2 / sampleRate * 1000 : 20;
  const captureStartAt = captureWorkerAt - durationMs;
  const age = at - captureStartAt;
  if (age < -20 || age + workletClock.uncertainty > FRAME_MAX_AGE_MS) {
    counters.stale += 1;
    counters.dropped += 1;
    reportStatus();
    return;
  }
  if (txCaptureFloor !== null && captureStartAt - workletClock.uncertainty < txCaptureFloor) {
    counters.stale += 1;
    counters.dropped += 1;
    reportStatus();
    return;
  }
  const activeSocket = socket;
  if (!socketReady || !activeSocket || activeSocket.readyState !== WebSocket.OPEN) {
    counters.dropped += 1;
    reportStatus();
    return;
  }
  const maxBuffered = Math.max(3200, Math.round((Number(data.sample_rate) || requestedOutputRate) * 0.1) * 2);
  if (activeSocket.bufferedAmount > maxBuffered) {
    counters.backpressure += 1;
    counters.dropped += 1;
    reportStatus();
    return;
  }
  if (!pcm || pcm.byteLength < 2 || pcm.byteLength % 2) {
    counters.dropped += 1;
    reportStatus();
    return;
  }
  const packet = new ArrayBuffer(MIC_HEADER_BYTES + pcm.byteLength);
  const bytes = new Uint8Array(packet);
  const view = new DataView(packet);
  bytes.set(MIC_MAGIC, 0);
  view.setUint8(8, MIC_VERSION);
  view.setUint8(9, 1);
  view.setUint16(10, MIC_HEADER_BYTES, true);
  view.setUint32(12, transportId, true);
  view.setUint32(16, txGeneration >>> 0, true);
  view.setUint32(20, captureGeneration >>> 0, true);
  view.setUint32(24, sequence++ >>> 0, true);
  view.setFloat64(28, captureStartAt + serverClock.offset, true);
  view.setFloat32(36, workletClock.uncertainty + serverClock.uncertainty, true);
  view.setUint32(40, sampleRate, true);
  view.setUint32(44, pcm.byteLength / 2, true);
  view.setFloat32(48, Number(data.peak) || 0, true);
  view.setFloat32(52, Number(data.rms) || 0, true);
  view.setUint16(56, Math.min(65535, Number(data.clipped) || 0), true);
  bytes.set(new Uint8Array(pcm), MIC_HEADER_BYTES);
  activeSocket.send(packet);
  counters.sent += 1;
  reportStatus();
}

function attachWorklet(port, generation, sampleRate) {
  if (workletPort) {
    try { workletPort.postMessage({ type: 'stop' }); } catch { /* Port already closed. */ }
    try { workletPort.close(); } catch { /* Port already closed. */ }
  }
  workletPort = port;
  workletPort.onmessage = event => handleWorkletMessage(event.data);
  captureGeneration = generation >>> 0;
  sequence = 0;
  requestedOutputRate = Number(sampleRate) || requestedOutputRate;
  invalidateClocks();
  workletPort.postMessage({
    type: 'config',
    gain,
    outputRate: requestedOutputRate,
  });
  workletPort.postMessage({ type: 'capture_generation', capture_generation: captureGeneration });
  reportStatus(true);
}

function detachWorklet(generation) {
  if (generation !== undefined && Number(generation) !== captureGeneration) return;
  if (workletPort) {
    try { workletPort.postMessage({ type: 'stop' }); } catch { /* Port already closed. */ }
    try { workletPort.close(); } catch { /* Port already closed. */ }
  }
  workletPort = null;
  workletClock = null;
  pendingWorkletClock = null;
  sequence = 0;
  reportStatus(true);
}

function updateOutputRate(outputRate) {
  const rate = Number(outputRate);
  if (!Number.isFinite(rate) || rate < 8000 || rate > 96000 || rate === requestedOutputRate) return;
  requestedOutputRate = rate;
  workletClock = null;
  pendingWorkletClock = null;
  workletPort?.postMessage({
    type: 'config',
    outputRate: rate,
    reset_capture: true,
  });
  workletPort?.postMessage({ type: 'capture_generation', capture_generation: captureGeneration });
  requestWorkletClock();
  reportStatus(true);
}

function shutdown() {
  shuttingDown = true;
  self.clearTimeout(reconnectTimer);
  self.clearInterval(clockTimer);
  reconnectTimer = 0;
  clockTimer = 0;
  detachWorklet();
  const activeSocket = socket;
  socket = null;
  socketReady = false;
  transportId = 0;
  serverClock = null;
  pendingServerClock = null;
  if (activeSocket) {
    activeSocket.onopen = null;
    activeSocket.onmessage = null;
    activeSocket.onerror = null;
    activeSocket.onclose = null;
    try { activeSocket.close(); } catch { /* Socket already closed. */ }
  }
  self.postMessage({ type: 'stopped' });
  self.close();
}

self.onmessage = event => {
  const data = event.data;
  if (!data) return;
  if (data.type === 'attachWorklet') {
    attachWorklet(data.port, Number(data.capture_generation) || 0, Number(data.sample_rate) || requestedOutputRate);
    return;
  }
  if (data.type === 'detachWorklet') {
    detachWorklet(Number(data.capture_generation));
    return;
  }
  if (data.type === 'config') {
    if (Number.isFinite(data.gain)) {
      gain = Math.max(0, Math.min(4, Number(data.gain)));
      workletPort?.postMessage({ type: 'config', gain });
    }
    if (Number.isFinite(data.outputRate)) updateOutputRate(Number(data.outputRate));
    return;
  }
  if (data.type === 'captureGeneration') {
    captureGeneration = Number(data.capture_generation) >>> 0;
    sequence = 0;
    workletClock = null;
    pendingWorkletClock = null;
    workletPort?.postMessage({ type: 'capture_generation', capture_generation: captureGeneration });
    requestWorkletClock();
    return;
  }
  if (data.type === 'invalidateClock') {
    invalidateClocks();
    reportStatus(true);
    return;
  }
  if (data.type === 'pttState') {
    const nextActive = data.active === true;
    const nextGeneration = nextActive ? Number(data.generation) >>> 0 : 0;
    if (nextActive && (!txActive || nextGeneration !== txGeneration)) {
      txCaptureFloor = nowMs();
    }
    if (!nextActive && txActive) {
      txCaptureFloor = null;
    }
    txActive = nextActive;
    txGeneration = nextGeneration;
    reportStatus(true);
    return;
  }
  if (data.type === 'fallbackPcm') {
    handleCaptureFrame({
      type: 'pcm',
      pcm: data.pcm,
      level: data.level,
      peak: data.peak ?? data.level,
      rms: data.rms ?? 0,
      clipped: data.clipped ?? 0,
      capture_generation: captureGeneration,
      sample_rate: Number(data.sample_rate) || requestedOutputRate,
      full_capture_age: false,
    });
    return;
  }
  if (data.type === 'shutdown') shutdown();
};

clockTimer = self.setInterval(() => {
  requestWorkletClock();
  requestServerClock();
  sendClientDiagnostics();
}, CLOCK_REQUEST_INTERVAL_MS);
openSocket();
