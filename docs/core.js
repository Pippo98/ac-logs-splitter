// Pure data helpers (no DOM, no network) so they can also be tested from node.
(function (root) {
  const NAN_TOKENS = new Set(['nan', 'NaN', 'NAN', 'inf', '-inf', 'Inf', '-Inf', 'None', 'null']);

  function splitQuoted(line) {
    const out = []; let cur = '', q = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (q) { if (c === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
      else if (c === '"') q = true;
      else if (c === ',') { out.push(cur); cur = ''; }
      else cur += c;
    }
    out.push(cur);
    return out;
  }
  const split = line => line.indexOf('"') < 0 ? line.split(',') : splitQuoted(line);

  // CSV text -> { columns, n, data:{name: Float64Array | string[]}, numeric:[names] }
  // Column type is decided on the first data row: numeric unless that cell is a non-numeric string.
  function parseCsv(text) {
    let pos = 0;
    const nextLine = () => {
      while (pos < text.length) {
        let e = text.indexOf('\n', pos); if (e < 0) e = text.length;
        let line = text.slice(pos, e); pos = e + 1;
        if (line.endsWith('\r')) line = line.slice(0, -1);
        if (line.length) return line;
      }
      return null;
    };
    const head = nextLine();
    if (head == null) throw new Error('empty csv');
    const columns = split(head).map(s => s.trim());
    let n = 0; for (let i = pos; i < text.length; i++) if (text.charCodeAt(i) === 10) n++;
    n += 1;                                                  // possible last line without newline
    const data = new Array(columns.length), isStr = new Array(columns.length);
    let row = 0, line;
    const numeric = [];
    while ((line = nextLine()) != null) {
      const f = split(line);
      if (row === 0) {
        for (let j = 0; j < columns.length; j++) {
          const s = (f[j] ?? '').trim();
          isStr[j] = s !== '' && !NAN_TOKENS.has(s) && !Number.isFinite(+s);
          data[j] = isStr[j] ? new Array(n).fill('') : new Float64Array(n).fill(NaN);
        }
      }
      for (let j = 0; j < columns.length; j++) {
        const s = f[j] ?? '';
        if (isStr[j]) data[j][row] = s;
        else { const v = s === '' ? NaN : +s; data[j][row] = v; }   // non-numeric junk -> NaN
      }
      row++;
    }
    const out = {};
    columns.forEach((c, j) => {
      out[c] = row === n ? data[j] : (isStr[j] ? data[j].slice(0, row) : data[j].subarray(0, row));
      if (!isStr[j]) numeric.push(c);
    });
    return { columns, n: row, data: out, numeric };
  }

  // numeric column -> plain array with null for gaps (what uPlot wants)
  function toSeries(col) {
    const a = new Array(col.length);
    for (let i = 0; i < col.length; i++) a[i] = Number.isFinite(col[i]) ? col[i] : null;
    return a;
  }

  // video_time = sim_time + offset, from the metadata wall-clock timestamps (0 if unavailable)
  function videoOffset(df, meta) {
    try {
      const v = df.data.time[0] - df.data.sim_time[0] - meta.video.video_start_wall_time;
      return Number.isFinite(v) ? v : 0;
    } catch { return 0; }
  }

  const esc = s => /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;

  // columns: [{name, rename}] chunks: [{start,end}] in sim_time seconds -> [{rows, text}]
  function exportChunks(df, columns, chunks) {
    if (!columns.length) throw new Error('no columns selected');
    if (!chunks.length) throw new Error('no chunks defined');
    const outNames = columns.map(c => c.rename || c.name);
    const dup = outNames.filter((n, i) => outNames.indexOf(n) !== i);
    if (dup.length) throw new Error('duplicate output column names: ' + [...new Set(dup)].sort().join(', '));
    const missing = columns.filter(c => !(c.name in df.data)).map(c => c.name);
    if (missing.length) throw new Error('unknown columns: ' + missing.join(', '));
    const st = df.data.sim_time, cols = columns.map(c => df.data[c.name]);
    const header = outNames.map(esc).join(',');
    return chunks.map(ch => {
      const lo = Math.min(ch.start, ch.end), hi = Math.max(ch.start, ch.end);
      const lines = [header]; let rows = 0;
      for (let i = 0; i < df.n; i++) {
        if (!(st[i] >= lo && st[i] <= hi)) continue;
        lines.push(cols.map(c => typeof c[i] === 'string' ? esc(c[i]) : (Number.isNaN(c[i]) ? '' : String(c[i]))).join(','));
        rows++;
      }
      return { rows, text: lines.join('\n') + '\n' };
    });
  }

  // minimal store-only (uncompressed) zip writer: files = [{name, text}] -> Uint8Array
  const CRC = (() => { const t = new Uint32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; t[n] = c >>> 0; } return t; })();
  function crc32(b) { let c = 0xFFFFFFFF; for (let i = 0; i < b.length; i++) c = CRC[(c ^ b[i]) & 255] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; }
  function makeZip(files) {
    const enc = new TextEncoder(), parts = [], central = [];
    let off = 0;
    for (const f of files) {
      const name = enc.encode(f.name), data = enc.encode(f.text), crc = crc32(data);
      const lh = new DataView(new ArrayBuffer(30));
      lh.setUint32(0, 0x04034b50, true); lh.setUint16(4, 20, true); lh.setUint16(6, 0x0800, true); lh.setUint16(8, 0, true);
      lh.setUint16(10, 0, true); lh.setUint16(12, 0x21, true); lh.setUint32(14, crc, true);
      lh.setUint32(18, data.length, true); lh.setUint32(22, data.length, true); lh.setUint16(26, name.length, true); lh.setUint16(28, 0, true);
      parts.push(new Uint8Array(lh.buffer), name, data);
      const ch = new DataView(new ArrayBuffer(46));
      ch.setUint32(0, 0x02014b50, true); ch.setUint16(4, 20, true); ch.setUint16(6, 20, true); ch.setUint16(8, 0x0800, true);
      ch.setUint16(14, 0x21, true); ch.setUint32(16, crc, true); ch.setUint32(20, data.length, true); ch.setUint32(24, data.length, true);
      ch.setUint16(28, name.length, true); ch.setUint32(42, off, true);
      central.push(new Uint8Array(ch.buffer), name);
      off += 30 + name.length + data.length;
    }
    const csize = central.reduce((a, b) => a + b.length, 0);
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true); end.setUint16(8, files.length, true); end.setUint16(10, files.length, true);
    end.setUint32(12, csize, true); end.setUint32(16, off, true);
    const all = [...parts, ...central, new Uint8Array(end.buffer)];
    const out = new Uint8Array(all.reduce((a, b) => a + b.length, 0)); let p = 0;
    for (const a of all) { out.set(a, p); p += a.length; }
    return out;
  }

  const api = { parseCsv, toSeries, videoOffset, exportChunks, makeZip };
  if (typeof module !== 'undefined') module.exports = api; else root.Core = api;
})(typeof window !== 'undefined' ? window : globalThis);
