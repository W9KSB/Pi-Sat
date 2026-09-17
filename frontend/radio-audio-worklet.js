// Persistent RX renderer. All PCM decoding, buffering and resampling stays on
// the audio rendering thread rather than competing with the waterfall/UI.
class RadioPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacity = 32768;
    this.left = new Float32Array(this.capacity);
    this.right = new Float32Array(this.capacity);
    this.read = 0; this.count = 0; this.phase = 0;
    this.rate = 16000; this.channels = 2;
    this.listen = 'SUB'; this.volume = 1;
    this.playing = false; this.alive = true;
    this.underruns = 0; this.trimmed = 0; this.peak = 0;
    this.reportFrames = 0; this.fade = 0;
    this.lastLeft = 0; this.lastRight = 0;
    // Jitter buffer target and the backlog ceiling that trims back to it.
    // Latency must stay bounded, not merely stable: a stall can leave the
    // buffer large, so trim excess backlog instead of relying on clock drift.
    this.targetMs = 45; this.ceilingMs = 110;
    this.port.onmessage = ({ data }) => {
      if (data.type === 'stop') { this.alive = false; this.count = 0; return; }
      if (data.type === 'reset') { this.count = 0; this.phase = 0; this.playing = false; this.fade = 0; return; }
      if (data.type === 'pcm') this.receive(data);
    };
  }

  receive(message) {
    const { packet, rate, channels } = message;
    if (!(packet instanceof ArrayBuffer) || packet.byteLength <= 24 || ![1, 2].includes(channels)
        || !Number.isFinite(rate) || rate < 8000 || rate > 96000) return;
    const view = new DataView(packet);
    const bytes = view.getUint16(22, false);
    if (view.getUint32(0, true) !== packet.byteLength || view.getUint16(4, true) !== 0
        || bytes !== packet.byteLength - 24 || bytes % (channels * 2)) return;
    if (rate !== this.rate || channels !== this.channels) {
      this.count = 0; this.phase = 0; this.playing = false; this.fade = 0;
      this.rate = rate; this.channels = channels;
    }
    this.listen = message.listen || (channels === 2 ? 'BOTH' : 'MAIN');
    this.volume = Number.isFinite(message.volume) ? Math.max(0, Math.min(1, message.volume)) : 1;
    const frames = bytes / (channels * 2);
    // A stalled tab/socket must catch up to live audio, not play the whole old
    // burst. Normal jitter stays in the target buffer; a larger backlog is
    // trimmed to the target so listening latency returns to normal at once.
    const maximum = Math.min(this.capacity - 1, Math.round(rate * this.ceilingMs / 1000));
    if (this.count + frames > maximum) {
      const retain = Math.max(0, Math.round(rate * this.targetMs / 1000) - frames);
      const drop = Math.max(0, this.count - retain);
      this.read = (this.read + drop) % this.capacity;
      this.count -= drop; this.phase = 0; this.fade = 0;
      this.trimmed += drop;
    }
    const first = Math.max(0, frames - maximum);
    this.trimmed += first;
    for (let frame = first; frame < frames; frame++) {
      const at = (this.read + this.count) % this.capacity;
      this.left[at] = view.getInt16(24 + frame * channels * 2, true) / 32768;
      this.right[at] = channels === 2 ? view.getInt16(26 + frame * channels * 2, true) / 32768 : this.left[at];
      this.count++;
    }
  }

  process(inputs, outputs) {
    const output = outputs[0];
    if (!output?.length) return this.alive;
    const left = output[0], right = output[1] || left;
    left.fill(0); right.fill(0);
    if (!this.alive) return false;
    const target = Math.round(this.rate * this.targetMs / 1000);
    if (!this.playing && this.count >= target) { this.playing = true; this.fade = 0; }
    // Clock correction prevents independent radio/output clocks from gradually
    // exhausting or growing the buffer during long listening runs. It stays
    // small near the target so steady-state pitch is untouched, and gets a
    // little stronger when the buffer is well off target so a small backlog
    // drains in seconds instead of a minute.
    const correction = 1 + Math.max(-0.005, Math.min(0.005, (this.count - target) / (this.rate * 4)));
    const step = this.rate / sampleRate * correction;
    const audible = this.channels === 2 || this.listen === 'MAIN';
    for (let frame = 0; frame < left.length; frame++) {
      if (!this.playing) {
        // Short decay avoids a hard edge when the network really starves us.
        this.lastLeft *= 0.95; this.lastRight *= 0.95;
        left[frame] = this.lastLeft; right[frame] = this.lastRight;
        continue;
      }
      const advance = Math.floor(this.phase + step);
      if (this.count < Math.max(2, advance + 1)) {
        this.playing = false; this.phase = 0; this.underruns++;
        continue;
      }
      const next = (this.read + 1) % this.capacity;
      const l = this.left[this.read] + (this.left[next] - this.left[this.read]) * this.phase;
      const r = this.right[this.read] + (this.right[next] - this.right[this.read]) * this.phase;
      this.fade = Math.min(1, this.fade + 1 / (sampleRate * 0.005));
      const a = this.listen === 'SUB' ? r : l;
      const b = this.listen === 'BOTH' ? r : a;
      const gain = audible ? this.volume * this.fade : 0;
      left[frame] = a * gain; right[frame] = b * gain;
      this.lastLeft = left[frame]; this.lastRight = right[frame];
      this.peak = Math.max(this.peak, Math.abs(a), Math.abs(b));
      this.phase += step;
      this.read = (this.read + advance) % this.capacity;
      this.count -= advance; this.phase -= advance;
    }
    this.reportFrames += left.length;
    if (this.reportFrames >= sampleRate / 2) {
      this.reportFrames = 0;
      this.port.postMessage({ type: 'stats', bufferedMs: this.count / this.rate * 1000,
        underruns: this.underruns, trimmedMs: this.trimmed / this.rate * 1000,
        peak: this.peak, playing: this.playing, needsStereo: !audible });
      this.peak = 0;
    }
    return true;
  }
}

registerProcessor('radio-pcm', RadioPcmProcessor);
