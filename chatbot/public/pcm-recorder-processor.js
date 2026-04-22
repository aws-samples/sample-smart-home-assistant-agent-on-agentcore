// AudioWorklet processor that downsamples the browser's native sample rate
// (usually 44.1 or 48 kHz) to 16 kHz mono Int16 PCM — the format Nova Sonic
// expects on its input stream. Chunks are posted back to the main thread as
// ArrayBuffers so the WS client can base64-encode and send them.
class PcmRecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = (options && options.processorOptions && options.processorOptions.targetRate) || 16000;
    this.ratio = sampleRate / this.targetRate;
    this.buffer = [];
    this.bufferedSamples = 0;
    // Emit ~100 ms chunks at 16 kHz = 1600 samples per chunk.
    this.flushAt = Math.floor(this.targetRate * 0.1);
  }

  downsample(input) {
    const outLen = Math.floor(input.length / this.ratio);
    const out = new Int16Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const sample = input[Math.floor(i * this.ratio)];
      const clamped = Math.max(-1, Math.min(1, sample));
      out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    }
    return out;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const channel = input[0];
    const downsampled = this.downsample(channel);
    this.buffer.push(downsampled);
    this.bufferedSamples += downsampled.length;
    if (this.bufferedSamples >= this.flushAt) {
      const merged = new Int16Array(this.bufferedSamples);
      let offset = 0;
      for (const chunk of this.buffer) {
        merged.set(chunk, offset);
        offset += chunk.length;
      }
      this.buffer = [];
      this.bufferedSamples = 0;
      this.port.postMessage(merged.buffer, [merged.buffer]);
    }
    return true;
  }
}

registerProcessor('pcm-recorder-processor', PcmRecorderProcessor);
