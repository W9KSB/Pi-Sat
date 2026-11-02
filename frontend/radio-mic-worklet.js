// Persistent microphone capture. Rate conversion and Int16 packing run on the
// audio rendering thread, so a busy main thread (waterfall, spectrum, DOM) can
// only delay the socket write, never the capture itself. The blocks that leave
// here are 20 ms mono LPCM16 frames, one WebSocket message every 20 ms.
class RadioMicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.alive = true;
    this.gain = 1;
    this.captureGeneration = 0;
    this.captureSequence = 0;
    this.pendingClockRequest = null;
    this.syntheticFrame = 0;
    this.processFrame = 0;
    this.captureFrameEnd = 0;
    this.rmsSum = 0;
    this.clipped = 0;
    this.setRates(sampleRate, 16000);
    this.port.onmessage = ({ data }) => {
      if (!data) return;
      if (data.type === 'stop') { this.alive = false; this.block = null; return; }
      if (data.type === 'capture_generation') {
        const nextGeneration = Number(data.capture_generation) >>> 0;
        if (nextGeneration !== this.captureGeneration) {
          const outputRate = this.outputRate;
          this.setRates(sampleRate, outputRate);
        }
        this.captureGeneration = nextGeneration;
        this.captureSequence = 0;
        this.rmsSum = 0;
        this.clipped = 0;
        return;
      }
      if (data.type === 'clock_request') {
        // Answer at the next render quantum, where currentFrame identifies the
        // audio clock without involving the main browser thread.
        this.pendingClockRequest = Number(data.id);
        return;
      }
      if (data.type === 'config') {
        if (Number.isFinite(data.gain)) this.gain = Math.max(0, Math.min(4, data.gain));
        if (Number.isFinite(data.outputRate) && data.outputRate >= 8000 && data.outputRate <= 96000) {
          this.setRates(sampleRate, data.outputRate);
        }
      }
    };
  }

  setRates(inputRate, outputRate) {
    this.inputRate = inputRate;
    this.outputRate = outputRate;
    // Input frames consumed per transmit frame. Both rates are fixed for the
    // life of a session, so the phase never has to be reset mid-stream.
    this.step = inputRate / outputRate;
    // One transmit frame is 20 ms of mono LPCM16 at the radio's rate.
    this.frameSamples = Math.max(1, Math.round(outputRate / 50));
    this.block = new Int16Array(this.frameSamples);
    this.filled = 0;
    this.level = 0;
    // Running interval accumulator: each output sample is the mean of the input
    // interval that covers it, carried across render quanta instead of restarted
    // with each buffer.
    this.position = 0;
    this.intervalEnd = this.step;
    this.sum = 0;
    this.weight = 0;
  }

  process(inputs, outputs) {
    const output = outputs && outputs[0];
    if (output) {
      // Capture is input-only; the node still has an output so the graph keeps
      // pulling it every quantum.
      for (const channel of output) channel.fill(0);
    }
    if (!this.alive) return false;
    this.processFrame = typeof currentFrame === 'number' ? currentFrame : this.syntheticFrame;
    if (this.pendingClockRequest !== null) {
      this.port.postMessage({
        type: 'clock_response',
        id: this.pendingClockRequest,
        context_frame: this.processFrame,
        sample_rate: sampleRate,
      });
      this.pendingClockRequest = null;
    }
    const input = inputs && inputs[0];
    const channel = input && input[0];
    if (channel) {
      for (let index = 0; index < channel.length; index++) {
        this.captureFrameEnd = this.processFrame + index + 1;
        this.accumulate(channel[index]);
      }
    }
    this.syntheticFrame += (channel && channel.length) || (outputs[0]?.[0]?.length || 0);
    return true;
  }

  accumulate(value) {
    // The incoming input frame covers [position, position + 1).
    const start = this.position;
    const end = start + 1;
    let cursor = start;
    while (cursor < end) {
      const room = this.intervalEnd - cursor;
      const take = room < end - cursor ? room : end - cursor;
      this.sum += value * take;
      this.weight += take;
      cursor += take;
      if (cursor >= this.intervalEnd) {
        this.emit(this.weight > 0 ? this.sum / this.weight : 0);
        this.sum = 0;
        this.weight = 0;
        this.intervalEnd += this.step;
      }
    }
    this.position = end;
  }

  emit(value) {
    const scaled = value * this.gain;
    const sample = scaled < -1 ? -1 : scaled > 1 ? 1 : scaled;
    const magnitude = sample < 0 ? -sample : sample;
    if (magnitude > this.level) this.level = magnitude;
    this.rmsSum += sample * sample;
    if (scaled <= -1 || scaled >= 1) this.clipped += 1;
    this.block[this.filled++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
    if (this.filled >= this.frameSamples) this.flush();
  }

  flush() {
    if (!this.filled || !this.block) return;
    // The common case fills the block exactly, so the buffer is handed over as
    // it stands. The message carries the level for this frame, so the meter
    // costs no extra messages on the main thread.
    const pcm = this.filled === this.frameSamples ? this.block : this.block.slice(0, this.filled);
    this.block = new Int16Array(this.frameSamples);
    this.filled = 0;
    const level = this.level;
    const peak = level;
    const rms = Math.sqrt(this.rmsSum / this.frameSamples);
    const clipped = this.clipped;
    this.level = 0;
    this.rmsSum = 0;
    this.clipped = 0;
    this.port.postMessage({
      type: 'pcm',
      pcm: pcm.buffer,
      level,
      peak,
      rms,
      clipped,
      capture_generation: this.captureGeneration,
      capture_sequence: this.captureSequence++,
      capture_frame_end: this.captureFrameEnd,
      sample_rate: this.outputRate,
      full_capture_age: true,
    }, [pcm.buffer]);
  }
}

registerProcessor('radio-mic', RadioMicProcessor);
