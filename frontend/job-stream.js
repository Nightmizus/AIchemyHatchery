/* Account-scoped live updates. Commands retain their request IDs and HTTP API.

MIT License

Copyright (c) 2026 DeepSeek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
(() => {
  'use strict';

  // Adapted from DeepSeek Harness dsh-v0.1.5-rc.1, ordered-baseline.ts.
  // Copyright (c) 2026 DeepSeek. MIT; see deepseek_harness/LICENSE.deepseek-harness.txt.
  function mergeOrderedBaseline(current, baseline, keyOf) {
    const baselineByKey = new Map(baseline.map(value => [keyOf(value), value]));
    const merged = current.map(value => baselineByKey.get(keyOf(value))).filter(value => value !== undefined);
    const mergedKeys = new Set(merged.map(keyOf));
    for (let index = 0; index < baseline.length; index++) {
      const value = baseline[index];
      if (value === undefined || mergedKeys.has(keyOf(value))) continue;
      let insertion = merged.length;
      for (let following = index + 1; following < baseline.length; following++) {
        const candidate = baseline[following];
        if (candidate === undefined) continue;
        const known = merged.findIndex(item => keyOf(item) === keyOf(candidate));
        if (known !== -1) { insertion = known; break; }
      }
      merged.splice(insertion, 0, value);
      mergedKeys.add(keyOf(value));
    }
    return merged;
  }

  class Connection {
    constructor({url, snapshot, event, state = () => {}, WebSocketClass = globalThis.WebSocket}) {
      this.url = url; this.snapshot = snapshot; this.event = event; this.state = state;
      this.WebSocketClass = WebSocketClass;
      this.cursor = null; this.socket = null; this.stopped = true; this.connected = false;
      this.attempt = 0; this.generation = 0; this.lastMessage = 0;
    }
    start() {
      if (!this.stopped || !this.WebSocketClass) return;
      this.stopped = false;
      this.connect();
      this.watchdog = setInterval(() => {
        if (!this.stopped && this.socket && Date.now() - this.lastMessage > 45000) this.socket.close();
      }, 5000);
    }
    stop() {
      this.stopped = true; this.connected = false; this.generation++;
      clearTimeout(this.retry); clearInterval(this.watchdog);
      const old = this.socket; this.socket = null;
      old?.close();
    }
    connect() {
      if (this.stopped) return;
      const generation = ++this.generation;
      const ws = new this.WebSocketClass(this.url);
      this.socket = ws; this.connected = false; this.lastMessage = Date.now();
      let chain = Promise.resolve(), pending = 0, bytes = 0;
      const current = () => !this.stopped && this.socket === ws && this.generation === generation;
      this.refresh = () => {
        chain = chain.then(async () => { if (current()) await this.snapshot(); })
          .catch(() => { if (current()) { this.cursor = null; ws.close(); } });
        return chain;
      };
      ws.onopen = () => { if (current()) ws.send(JSON.stringify({type:'subscribe', cursor:this.cursor})); };
      ws.onmessage = message => {
        if (!current()) return;
        this.lastMessage = Date.now();
        let frame;
        try { frame = JSON.parse(message.data); } catch { ws.close(); return; }
        if (!frame || typeof frame !== 'object') { ws.close(); return; }
        if (frame.type === 'heartbeat') return;
        const size = String(message.data).length;
        if (++pending > 256 || (bytes += size) > 2_000_000) {
          this.cursor = null; ws.close(); return;
        }
        chain = chain.then(async () => {
          if (!current()) return;
          if (frame.type === 'ready') {
            if (frame.payload?.protocol !== 1) throw new Error('Unsupported event protocol');
            if (frame.payload.reset) {
              await this.snapshot();
              if (!current()) return;
              this.cursor = frame.payload.cursor;
            }
            this.connected = true; this.attempt = 0; this.state(true);
            return;
          }
          if (frame.type === 'resync') {
            await this.snapshot();
          } else if (frame.type === 'job' || frame.type === 'progress') {
            // Reject duplicates and gaps; a dropped frame must trigger a baseline.
            const [epoch, raw] = String(frame.id || '').split(':');
            const [previousEpoch, previousRaw] = String(this.cursor || '').split(':');
            const sequence = Number(raw), previous = Number(previousRaw);
            if (!epoch || !Number.isSafeInteger(sequence)) throw new Error('Invalid cursor');
            if (epoch === previousEpoch && sequence <= previous) return;
            if (epoch !== previousEpoch || sequence !== previous + 1) {
              this.cursor = null; throw new Error('Event gap');
            }
            await this.event(frame.type, frame.payload);
          } else throw new Error('Unknown event');
          if (current()) this.cursor = frame.id;
        }).catch(() => { if (current()) { this.cursor = null; ws.close(); } })
          .finally(() => { pending--; bytes -= size; });
      };
      ws.onerror = () => { if (current()) ws.close(); };
      ws.onclose = () => {
        if (!current()) return;
        this.socket = null; this.connected = false; this.state(false);
        const delay = Math.min(15000, 750 * 2 ** Math.min(this.attempt++, 5));
        this.retry = setTimeout(() => this.connect(), delay + Math.floor(Math.random() * 250));
      };
    }
  }
  const api = {Connection, mergeOrderedBaseline};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.HatcheryJobStream = Object.freeze(api);
})();
