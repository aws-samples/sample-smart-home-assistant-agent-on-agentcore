/**
 * VoiceClient — manages the browser side of the /ws voice session:
 *
 *   1. Opens a WebSocket to the CloudFront proxy (which injects the
 *      Authorization header server-side from the ?token query param).
 *   2. Starts an AudioWorklet that captures 16 kHz mono Int16 PCM from the
 *      microphone and sends it as base64 `audio_input` messages.
 *   3. Queues inbound audio frames (welcome MP3 + Nova Sonic PCM) and plays
 *      them through a single shared AudioContext so they don't overlap.
 *   4. Surfaces transcript + status callbacks to the React UI.
 */

type StatusEvent =
  | { kind: 'connecting' }
  | { kind: 'connected' }
  | { kind: 'disconnected' }
  | { kind: 'error'; message: string };

type TranscriptEvent = {
  role: 'user' | 'assistant';
  text: string;
  /**
   * Nova Sonic emits a transcript twice per utterance: a SPECULATIVE pass
   * then a FINAL pass (`generationStage` in the raw event, exposed as
   * `is_final` on `bidi_transcript_stream`). The UI should replace the
   * speculative message with the final one rather than appending both.
   */
  isFinal: boolean;
};

export interface VoiceClientOptions {
  wsUrl: string;          // full wss:// URL including ?token&sessionId
  inputSampleRate?: number;
  outputSampleRate?: number;
  voice?: string;
  onStatus?: (event: StatusEvent) => void;
  onTranscript?: (event: TranscriptEvent) => void;
}

function base64Encode(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + chunkSize)));
  }
  return btoa(binary);
}

function base64Decode(b64: string): ArrayBuffer {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export class VoiceClient {
  private ws: WebSocket | null = null;
  private audioCtx: AudioContext | null = null;
  private playbackCtx: AudioContext | null = null;
  private workletNode: AudioWorkletNode | null = null;
  private micStream: MediaStream | null = null;
  private micSource: MediaStreamAudioSourceNode | null = null;
  private playbackCursor = 0;
  private stopped = false;
  private inputRate: number;
  private outputRate: number;
  private voice: string;
  private welcomeChunks: string[] = [];
  private welcomeTotal = 0;

  constructor(private opts: VoiceClientOptions) {
    this.inputRate = opts.inputSampleRate ?? 16000;
    this.outputRate = opts.outputSampleRate ?? 16000;
    this.voice = opts.voice ?? 'matthew';
  }

  async start(): Promise<void> {
    this.opts.onStatus?.({ kind: 'connecting' });

    try {
      await this.openMic();
    } catch (e: any) {
      this.opts.onStatus?.({ kind: 'error', message: e?.message || String(e) });
      throw e;
    }

    this.ws = new WebSocket(this.opts.wsUrl);
    this.ws.binaryType = 'arraybuffer';

    this.ws.onopen = () => {
      this.ws?.send(JSON.stringify({
        type: 'config',
        voice: this.voice,
        input_sample_rate: this.inputRate,
        output_sample_rate: this.outputRate,
        model_id: 'amazon.nova-2-sonic-v1:0',
      }));
      this.opts.onStatus?.({ kind: 'connected' });
    };

    this.ws.onmessage = (ev) => {
      // Text (JSON) frames only — audio is embedded base64 inside JSON.
      if (typeof ev.data !== 'string') return;
      try {
        const msg = JSON.parse(ev.data);
        this.handleServerMessage(msg);
      } catch (e) {
        console.warn('VoiceClient: failed to parse message', e);
      }
    };

    this.ws.onerror = () => {
      this.opts.onStatus?.({ kind: 'error', message: 'WebSocket error' });
    };

    this.ws.onclose = () => {
      this.opts.onStatus?.({ kind: 'disconnected' });
      this.teardownAudio();
    };
  }

  stop(): void {
    this.stopped = true;
    try { this.ws?.close(); } catch {}
    this.teardownAudio();
  }

  private async openMic(): Promise<void> {
    this.micStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        channelCount: 1,
      },
    });
    this.audioCtx = new AudioContext();
    await this.audioCtx.audioWorklet.addModule('/pcm-recorder-processor.js');
    this.micSource = this.audioCtx.createMediaStreamSource(this.micStream);
    this.workletNode = new AudioWorkletNode(this.audioCtx, 'pcm-recorder-processor', {
      processorOptions: { targetRate: this.inputRate },
    });
    this.workletNode.port.onmessage = (e) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
      const audio = base64Encode(e.data as ArrayBuffer);
      // Strands BidiAgent expects the `bidi_audio_input` event type.
      this.ws.send(JSON.stringify({
        type: 'bidi_audio_input',
        audio,
        format: 'pcm',
        sample_rate: this.inputRate,
        channels: 1,
      }));
    };
    this.micSource.connect(this.workletNode);
    // Worklet doesn't need to reach the destination — it only posts data.
  }

  private handleServerMessage(msg: any): void {
    const t = msg?.type;
    // Welcome clip: agent streams it as chunked `bidi_audio_stream` frames
    // tagged with `is_welcome: true`. Reassemble, then decode as MP3.
    if (t === 'bidi_audio_stream' && msg.is_welcome && msg.audio) {
      this.welcomeTotal = msg.total ?? 1;
      const seq = msg.seq ?? this.welcomeChunks.length;
      this.welcomeChunks[seq] = msg.audio;
      const received = this.welcomeChunks.filter(Boolean).length;
      if (received >= this.welcomeTotal) {
        const full = this.welcomeChunks.join('');
        this.welcomeChunks = [];
        this.welcomeTotal = 0;
        this.playEncodedMedia(full, msg.format || 'mp3');
      }
      return;
    }
    if ((t === 'audio_output' || t === 'bidi_audio_output' || t === 'bidi_audio_stream') && msg.audio) {
      // Nova Sonic output is Int16 PCM at output_sample_rate.
      this.playPcm(msg.audio, msg.sample_rate || this.outputRate);
    } else if (t === 'transcript' || t === 'bidi_transcript_stream') {
      const role = msg.role === 'user' ? 'user' : 'assistant';
      const text = msg.text || (typeof msg.delta === 'string' ? msg.delta : msg.delta?.text) || '';
      const isFinal = Boolean(msg.is_final ?? msg.isFinal);
      if (text) this.opts.onTranscript?.({ role, text, isFinal });
    } else if (t === 'error') {
      this.opts.onStatus?.({ kind: 'error', message: msg.message || 'Agent error' });
    }
  }

  private getPlaybackCtx(): AudioContext {
    if (!this.playbackCtx) {
      this.playbackCtx = new AudioContext();
      this.playbackCursor = this.playbackCtx.currentTime;
    }
    return this.playbackCtx;
  }

  private async playEncodedMedia(b64: string, _format: string): Promise<void> {
    const ctx = this.getPlaybackCtx();
    const buf = base64Decode(b64);
    try {
      const audioBuffer = await ctx.decodeAudioData(buf.slice(0));
      this.scheduleBuffer(audioBuffer);
    } catch (e) {
      console.warn('VoiceClient: decodeAudioData failed', e);
    }
  }

  private playPcm(b64: string, sampleRate: number): void {
    const ctx = this.getPlaybackCtx();
    const raw = new Int16Array(base64Decode(b64));
    if (raw.length === 0) return;
    const float = new Float32Array(raw.length);
    for (let i = 0; i < raw.length; i++) float[i] = raw[i] / 0x8000;
    const audioBuffer = ctx.createBuffer(1, float.length, sampleRate);
    audioBuffer.copyToChannel(float, 0);
    this.scheduleBuffer(audioBuffer);
  }

  private scheduleBuffer(audioBuffer: AudioBuffer): void {
    const ctx = this.getPlaybackCtx();
    const src = ctx.createBufferSource();
    src.buffer = audioBuffer;
    src.connect(ctx.destination);
    const startAt = Math.max(ctx.currentTime, this.playbackCursor);
    src.start(startAt);
    this.playbackCursor = startAt + audioBuffer.duration;
  }

  private teardownAudio(): void {
    try { this.workletNode?.disconnect(); } catch {}
    try { this.micSource?.disconnect(); } catch {}
    try { this.micStream?.getTracks().forEach(t => t.stop()); } catch {}
    try { this.audioCtx?.close(); } catch {}
    this.workletNode = null;
    this.micSource = null;
    this.micStream = null;
    this.audioCtx = null;
  }
}

// buildVoiceWsUrl was removed — the chatbot now presigns the WS URL with SigV4
// via presignWsUrl() in ./sigv4.ts. See ChatInterface.tsx for the call site.
