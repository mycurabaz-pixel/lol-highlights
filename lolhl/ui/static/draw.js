// Oynatıcının üstüne çizim (kalem, ok, daire). Çizimler yalnızca bu uygulamada, videonun
// üstünde durur; oyunun üstüne hiçbir şey çizilmez. Her çizim çizildiği saniyeye bağlanır,
// video oynarken o anda görünür ve videonun yanına (klip.mp4.notes.json) kaydedilir.
//
// Bir çizim: { id, t, d, tool, color, w, pts }
//   t: göründüğü video saniyesi, d: ekranda kalma süresi (0 = sona kadar)
//   pts: videoya göre 0..1 arası noktalar, w: 1080p'deki kalınlık
(() => {
  const COLORS = ["#ff3b3b", "#ffd23f", "#21d4fd", "#3fe87f", "#ffffff"];
  const FADE = 0.4;
  const REF_H = 1080;

  const video = document.getElementById("video");
  const canvas = document.getElementById("draw-canvas");
  const ctx = canvas.getContext("2d");
  const el = (id) => document.getElementById(id);

  let url = null;
  let items = [];
  let draft = null;
  let on = false;
  let tool = "pen";
  let color = COLORS[0];
  let saveTimer = null;

  const api = () => window.pywebview && window.pywebview.api;

  // ---------- çizim ----------

  // object-fit: contain ile videonun kutu içindeki gerçek yeri
  function frameRect(vw, vh, bw, bh) {
    if (!vw || !vh) return { x: 0, y: 0, w: bw, h: bh };
    const s = Math.min(bw / vw, bh / vh);
    return { x: (bw - vw * s) / 2, y: (bh - vh * s) / 2, w: vw * s, h: vh * s };
  }

  function endOf(a) {
    return a.d ? a.t + a.d : video.duration || Infinity;
  }

  function opacity(a, t) {
    if (t < a.t - 0.05 || t > endOf(a)) return 0;
    return a.d ? Math.min(1, (endOf(a) - t) / FADE) : 1;
  }

  function stroke(c, width, col, path) {
    c.lineCap = "round";
    c.lineJoin = "round";
    c.strokeStyle = "rgba(0,0,0,.55)";  // koyu kenar: açık zeminde de okunsun
    c.lineWidth = width + Math.max(2, width * 0.5);
    path(); c.stroke();
    c.strokeStyle = col;
    c.lineWidth = width;
    path(); c.stroke();
  }

  function drawOne(c, a, r, alpha = 1) {
    if (!a.pts.length || alpha <= 0) return;
    const px = (p) => [r.x + p[0] * r.w, r.y + p[1] * r.h];
    const width = Math.max(1.5, (a.w * r.h) / REF_H);
    c.save();
    c.globalAlpha = alpha;
    if (a.tool === "pen") {
      const p = a.pts.map(px);
      stroke(c, width, a.color, () => {
        c.beginPath();
        c.moveTo(p[0][0], p[0][1]);
        if (p.length === 1) c.lineTo(p[0][0] + 0.1, p[0][1]);
        for (let i = 1; i < p.length - 1; i++) {
          c.quadraticCurveTo(p[i][0], p[i][1], (p[i][0] + p[i + 1][0]) / 2, (p[i][1] + p[i + 1][1]) / 2);
        }
        if (p.length > 1) c.lineTo(p[p.length - 1][0], p[p.length - 1][1]);
      });
    } else {
      const [x0, y0] = px(a.pts[0]);
      const [x1, y1] = px(a.pts[a.pts.length - 1]);
      if (a.tool === "arrow") {
        const ang = Math.atan2(y1 - y0, x1 - x0);
        const head = Math.max(12, width * 3.2);
        stroke(c, width, a.color, () => {
          c.beginPath();
          c.moveTo(x0, y0); c.lineTo(x1, y1);
          c.moveTo(x1 - head * Math.cos(ang - 0.45), y1 - head * Math.sin(ang - 0.45));
          c.lineTo(x1, y1);
          c.lineTo(x1 - head * Math.cos(ang + 0.45), y1 - head * Math.sin(ang + 0.45));
        });
      } else {
        stroke(c, width, a.color, () => {
          c.beginPath();
          c.ellipse((x0 + x1) / 2, (y0 + y1) / 2, Math.abs(x1 - x0) / 2 || 1, Math.abs(y1 - y0) / 2 || 1, 0, 0, Math.PI * 2);
        });
      }
    }
    c.restore();
  }

  function frame() {
    requestAnimationFrame(frame);
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(canvas.clientWidth * dpr);
    const h = Math.round(canvas.clientHeight * dpr);
    if (!w || !h) return;
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
    if (!url) return;
    const r = frameRect(video.videoWidth, video.videoHeight, canvas.clientWidth, canvas.clientHeight);
    for (const a of items) drawOne(ctx, a, r, opacity(a, video.currentTime));
    if (draft) drawOne(ctx, draft, r);
  }
  requestAnimationFrame(frame);

  // ---------- fare ----------

  function toNorm(e) {
    const box = canvas.getBoundingClientRect();
    const r = frameRect(video.videoWidth, video.videoHeight, box.width, box.height);
    const clamp = (v) => Math.min(1, Math.max(0, v));
    return [clamp((e.clientX - box.left - r.x) / r.w), clamp((e.clientY - box.top - r.y) / r.h)];
  }

  canvas.addEventListener("pointerdown", (e) => {
    if (!on || !url || e.button !== 0) return;
    try { canvas.setPointerCapture(e.pointerId); } catch {}
    video.pause();
    const p = toNorm(e);
    draft = {
      id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      t: Math.round(video.currentTime * 100) / 100,
      d: +el("draw-duration").value, tool, color, w: +el("draw-width").value, pts: [p, p],
    };
  });

  canvas.addEventListener("pointermove", (e) => {
    if (!draft) return;
    const p = toNorm(e);
    if (draft.tool === "pen") draft.pts.push(p);
    else draft.pts[1] = p;
  });

  function finish() {
    const a = draft;
    draft = null;
    if (!a) return;
    if (a.tool === "pen") a.pts = simplify(a.pts.slice(1));
    else if (extent(a.pts) < 0.004) return;  // yanlışlıkla tıklama
    items.push(a);
    items.sort((x, y) => x.t - y.t);
    changed();
  }
  canvas.addEventListener("pointerup", finish);
  canvas.addEventListener("pointercancel", finish);

  function extent(pts) {
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    return Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
  }

  // Çok yakın noktaları atar, dosya küçük kalsın
  function simplify(pts) {
    const out = [pts[0]];
    for (const p of pts.slice(1)) {
      const q = out[out.length - 1];
      if (Math.hypot(p[0] - q[0], p[1] - q[1]) > 0.0015) out.push(p);
    }
    if (out.length === 1 && pts.length > 1) out.push(pts[pts.length - 1]);
    return out.map((p) => [Math.round(p[0] * 1e4) / 1e4, Math.round(p[1] * 1e4) / 1e4]);
  }

  // ---------- yükleme / kaydetme ----------

  function srcPath() {
    const s = video.currentSrc || video.getAttribute("src");
    if (!s) return null;
    try { return decodeURIComponent(new URL(s, location.href).pathname); } catch { return null; }
  }

  async function load() {
    const next = srcPath();
    if (next === url) return;
    if (saveTimer) await save();
    url = next;
    items = [];
    draft = null;
    renderCount();
    if (!url || !api()) return;
    const loading = url;
    try {
      const data = await api().notes_load(url);
      if (loading === url) items = data.annotations || [];
    } catch {}
    renderCount();
    notify();
  }
  video.addEventListener("loadstart", load);
  video.addEventListener("emptied", load);

  function changed() {
    renderCount();
    notify();
    el("draw-status").textContent = "Saving...";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, 600);
  }

  async function save() {
    clearTimeout(saveTimer);
    saveTimer = null;
    if (!url || !api()) return;
    try {
      await api().notes_save(url, { annotations: items });
      el("draw-status").textContent = "Saved";
    } catch (e) {
      el("draw-status").textContent = "Could not save: " + e;
    }
  }

  // tam maç çizelgesindeki işaretler için
  const notify = () => document.dispatchEvent(new CustomEvent("drawings-changed"));

  function renderCount() {
    el("draw-count").textContent = items.length ? `${items.length} drawings` : "";
    el("draw-export").disabled = !items.length;
  }

  // ---------- araç çubuğu ----------

  function setOn(v) {
    on = v;
    canvas.classList.toggle("active", on);
    el("draw-tools").classList.toggle("hidden", !on);
    el("draw-toggle").classList.toggle("on", on);
    el("draw-toggle").textContent = on ? "✎ Drawing on" : "✎ Draw";
    if (on) video.pause();
  }

  function setTool(t) {
    tool = t;
    document.querySelectorAll("[data-draw-tool]").forEach((b) => b.classList.toggle("on", b.dataset.drawTool === t));
  }

  function undo() {
    if (!items.length) return;
    const last = items.reduce((a, b) => (b.id > a.id ? b : a));
    items = items.filter((a) => a !== last);
    changed();
  }

  function clearNow() {
    const t = video.currentTime;
    const n = items.length;
    items = items.filter((a) => !(t >= a.t - 0.05 && t <= endOf(a)));
    if (items.length !== n) changed();
  }

  // Aynı anda görünen çizimleri tek PNG'de toplar; ffmpeg her PNG'yi kendi zaman
  // aralığında videonun üstüne koyar.
  async function exportVideo() {
    if (!items.length || !url) return;
    if (!video.videoWidth) return;
    if (saveTimer) await save();
    const W = video.videoWidth, H = video.videoHeight;
    const groups = new Map();
    for (const a of items) {
      const key = `${a.t}|${endOf(a)}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(a);
    }
    const layers = [];
    for (const list of groups.values()) {
      const c = document.createElement("canvas");
      c.width = W; c.height = H;
      const g = c.getContext("2d");
      list.forEach((a) => drawOne(g, a, { x: 0, y: 0, w: W, h: H }));
      layers.push({ png: c.toDataURL("image/png"), start: list[0].t, end: Math.min(endOf(list[0]), video.duration || 1e9) });
    }
    const btn = el("draw-export");
    btn.disabled = true;
    el("draw-status").textContent = "Exporting...";
    try {
      const path = await api().export_drawn(url, layers);
      el("draw-status").textContent = "Saved: " + path.split(/[\\/]/).pop();
    } catch (e) {
      el("draw-status").textContent = "Export failed: " + e;
    } finally {
      btn.disabled = false;
    }
  }

  el("draw-colors").innerHTML = COLORS.map((c, i) =>
    `<button class="swatch${i ? "" : " on"}" data-color="${c}" style="background:${c}" title="${c}"></button>`).join("");
  el("draw-colors").querySelectorAll(".swatch").forEach((b) => (b.onclick = () => {
    color = b.dataset.color;
    el("draw-colors").querySelectorAll(".swatch").forEach((s) => s.classList.toggle("on", s === b));
  }));
  document.querySelectorAll("[data-draw-tool]").forEach((b) => (b.onclick = () => setTool(b.dataset.drawTool)));
  el("draw-toggle").onclick = () => setOn(!on);
  el("draw-undo").onclick = undo;
  el("draw-clear").onclick = clearNow;
  el("draw-export").onclick = exportVideo;

  document.addEventListener("keydown", (e) => {
    if (document.getElementById("view-match").classList.contains("hidden") || e.target.matches?.("input, textarea, select")) return;
    const k = e.key.toLowerCase();
    if (k === "d" && !e.ctrlKey) { e.preventDefault(); setOn(!on); }
    else if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); undo(); }
    else if (on && ["1", "2", "3"].includes(k)) setTool(["pen", "arrow", "circle"][+k - 1]);
    else if (on && k === "delete") clearNow();
  });

  window.Draw = { list: () => items, off: () => setOn(false) };
})();
