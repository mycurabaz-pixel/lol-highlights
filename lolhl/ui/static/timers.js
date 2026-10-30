// Video izlerken objektif ve orman zamanlayıcıları (T tuşu). Oyun sırasında değil, maç
// sonrası videonun üstünde çalışır. Ejder/Baron/Herald/Kurtçuk anları Riot verisinden
// otomatik gelir. Küçük kampların kesilme anı Riot verisinde yok; analysis.py dakikalık
// orman CS'i ve konumdan tahmin eder ("~" ile gösterilir). Kampa tıklamak kesin anı
// işaretler, sağ tık işareti/tahmini siler; işaretler maç klasörüne (camps.json) kaydedilir.
const Timers = (() => {
  // Kurallar: Patch 26.1 (Atakhan kaldırıldı, Baron 20:00, kamplar 0:55 / 1:07).
  // Riot süreleri değiştirirse yalnızca bu tablo güncellenir. Saniye cinsinden.
  const RULES = {
    dragon: { first: 300, respawn: 300 },
    elder: { afterSoul: 360, respawn: 360 },
    baron: { first: 1200, respawn: 360 },
    herald: { first: 900, despawn: 1185 },   // Baron çukurunda; Baron doğmadan kaybolur
    grubs: { first: 480, despawn: 885, count: 3 },  // tek dalga, Herald'dan önce kaybolur
  };
  const CAMPS = [  // [id, ad (oyundaki adıyla), ilk doğma, yeniden doğma]
    ["blue", "Blue", 55, 300], ["gromp", "Gromp", 67, 135], ["wolves", "Wolves", 55, 135],
    ["raptors", "Raptors", 55, 135], ["red", "Red", 55, 300], ["krugs", "Krugs", 67, 135],
  ];
  const SCUTTLES = [["scuttle_top", "Scuttle · top", 175, 150], ["scuttle_bot", "Scuttle · bot", 175, 150]];
  const DRAGON_NAMES = { fire: "Infernal", ocean: "Ocean", mountain: "Mountain", cloud: "Cloud",
    hextech: "Hextech", chemtech: "Chemtech", elder: "Elder" };

  const el = (id) => document.getElementById(id);
  let on = false;
  let marks = [];      // [{camp, t}] oyun saati
  let folder = null;
  let timer = null;

  const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const ev = (type) => Analysis.data().events.filter((e) => e.type === type);

  // ---------- epik canavarlar ----------
  function dragon(t) {
    const drakes = ev("dragon");
    const done = drakes.filter((e) => e.t <= t);
    const soul = ev("soul").find((e) => e.t <= t);
    let spawn;
    if (soul) {
      const lastElder = done.filter((e) => e.sub === "elder").pop();
      spawn = lastElder ? lastElder.t + RULES.elder.respawn : soul.t + RULES.elder.afterSoul;
    } else {
      spawn = done.length ? done[done.length - 1].t + RULES.dragon.respawn : RULES.dragon.first;
    }
    // maç sonrası olduğumuz için sıradaki ejderin türü bilinir
    const kind = soul ? "elder" : drakes.find((e) => e.t > t)?.sub;
    return { name: kind ? `${DRAGON_NAMES[kind]} Drake` : "Drake", spawn, cls: kind || "dragon" };
  }

  function baron(t) {
    const last = ev("baron").filter((e) => e.t <= t).pop();
    return { name: "Baron", spawn: last ? last.t + RULES.baron.respawn : RULES.baron.first, cls: "baron" };
  }

  function minor(t) {
    const out = [];
    const grubs = ev("grubs").filter((e) => e.t <= t).length;
    if (t < RULES.grubs.despawn && grubs < RULES.grubs.count) {
      out.push(t < RULES.grubs.first ? `Void Grubs ${mmss(RULES.grubs.first - t)}`
        : `Void Grubs up${grubs ? ` (${RULES.grubs.count - grubs} left)` : ""}`);
    }
    const herald = ev("herald").some((e) => e.t <= t);
    if (!herald && t < RULES.herald.despawn) {
      out.push(t < RULES.herald.first ? `Rift Herald ${mmss(RULES.herald.first - t)}` : "Rift Herald up");
    }
    return out;
  }

  // ---------- kamplar ----------
  // Kesimler: Riot verisinden tahmin (analysis.camps) + elle işaretler. Elle işaret,
  // aynı kampın ±90 sn içindeki tahminini geçersiz kılar; {veto} işareti tahmini siler.
  const NEAR = 90;
  function kills(id) {
    const own = marks.filter((m) => m.camp === id);
    const est = (Analysis.data().camps || []).filter((e) => e.camp === id &&
      !own.some((m) => Math.abs(m.t - e.t) <= NEAR)).map((e) => ({ t: e.t, estimated: true }));
    return [...own.filter((m) => !m.veto), ...est].sort((a, b) => a.t - b.t);
  }

  function campLeft(id, first, respawn, t) {
    const last = kills(id).filter((k) => k.t <= t).pop();
    return { left: (last ? last.t + respawn : first) - t, estimated: !!last?.estimated };
  }

  // Panel bir kez kurulur, sonra yalnızca süreler güncellenir (yeniden kurmak tıklamaları yutar)
  let builtFor = null;
  let rows = [];  // [{id, first, respawn, node}]

  function buildCamps(myTeam) {
    const row = (id, name) => `<button class="tm-camp" data-camp="${id}"
      title="~ = estimated from Riot data · Click: mark as taken now · Right-click: remove last kill"><span>${name}</span><b></b></button>`;
    const side = (team, label) => `<div class="tm-side"><div class="tm-side-h">${label}</div>
      ${CAMPS.map(([id, name]) => row(`${id}_${team}`, name)).join("")}</div>`;
    el("tm-camps").innerHTML = side(myTeam, "Our jungle") + side(300 - myTeam, "Enemy jungle") +
      `<div class="tm-side">${SCUTTLES.map(([id, name]) => row(id, name)).join("")}</div>`;
    const defs = [...[myTeam, 300 - myTeam].flatMap((team) => CAMPS.map(([id, , f, r]) => [`${id}_${team}`, f, r])),
      ...SCUTTLES.map(([id, , f, r]) => [id, f, r])];
    rows = defs.map(([id, first, respawn]) => ({ id, first, respawn, node: el("tm-camps").querySelector(`[data-camp="${id}"]`) }));
    builtFor = myTeam;
  }

  function renderCamps(t) {
    const d = Analysis.data();
    if (builtFor !== d.my_team) buildCamps(d.my_team);
    for (const r of rows) {
      const { left, estimated } = campLeft(r.id, r.first, r.respawn, t);
      r.node.classList.toggle("up", left <= 0);
      r.node.querySelector("b").textContent = left <= 0 ? "Up" : (estimated ? "~" : "") + mmss(left);
    }
  }

  // ---------- çizim ----------
  // Dragon/Baron videonun altında küçük, oyunu kapatmadan; kamplar sağ üstte
  function pill(o, t) {
    const left = o.spawn - t;
    return `<span class="tm-pill ${o.cls} ${left <= 0 ? "alive" : ""}"><i></i>${o.name}<b>${left <= 0 ? "Up" : mmss(left)}</b></span>`;
  }

  function tick() {
    if (!on) return;
    const d = Analysis.data();
    const t = currentGameTime();
    if (!d || t == null || t < 0) {
      const msg = !d ? "Loading match data..." : t == null ? "Game time unknown for this video"
        : "Game hasn't started yet (loading screen)";
      el("tm-bottom").innerHTML = `<span class="tm-pill">${msg}</span>`;
      el("tm-camps").innerHTML = "";
      builtFor = null;
      return;
    }
    el("tm-bottom").innerHTML = pill(dragon(t), t) + pill(baron(t), t) +
      minor(t).map((s) => `<span class="tm-pill minor">${s}</span>`).join("");
    renderCamps(t);
  }

  async function loadMarks() {
    if (typeof current === "undefined" || !current || current.external) { folder = null; marks = []; return; }
    if (folder === current.folder) return;
    folder = current.folder;
    try { marks = await window.pywebview.api.camps_load(folder); } catch { marks = []; }
  }

  function save() {
    if (folder) window.pywebview.api.camps_save(folder, marks).catch(() => {});
  }

  function setOn(v) {
    on = v;
    el("timers").classList.toggle("hidden", !on);
    el("timer-toggle").classList.toggle("on", on);
    clearInterval(timer);
    if (on) { loadMarks().then(tick); timer = setInterval(tick, 250); }
  }

  el("tm-camps").addEventListener("click", (e) => {
    const b = e.target.closest("[data-camp]");
    const t = currentGameTime();
    if (!b || t == null) return;
    // aşağı yuvarla: yukarı yuvarlanırsa duran videoda işaret "henüz olmamış" sayılır
    marks.push({ camp: b.dataset.camp, t: Math.floor(t * 10) / 10 });
    marks.sort((a, c) => a.t - c.t);
    save(); tick();
  });
  el("tm-camps").addEventListener("contextmenu", (e) => {
    const b = e.target.closest("[data-camp]");
    const t = currentGameTime();
    if (!b || t == null) return;
    e.preventDefault();
    const id = b.dataset.camp;
    const last = kills(id).filter((k) => k.t <= t).pop();
    if (!last) return;
    if (last.estimated) marks.push({ camp: id, t: last.t, veto: true });  // yanlış tahmini sil
    else marks.splice(marks.indexOf(last), 1);
    save(); tick();
  });
  el("timer-toggle").onclick = () => setOn(!on);
  document.addEventListener("analysis-changed", () => { folder = null; if (on) loadMarks().then(tick); });
  document.addEventListener("keydown", (e) => {
    if (el("view-match").classList.contains("hidden") || e.target.matches?.("input, textarea, select")) return;
    if (e.key.toLowerCase() === "t" && !e.ctrlKey && !e.metaKey) { e.preventDefault(); setOn(!on); }
  });

  return { off: () => setOn(false) };
})();
