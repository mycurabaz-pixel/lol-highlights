// Live sekmesi: şampiyon seçiminde rün/item önerisi (client'a içe aktarma) ve oyun
// yüklenirken 10 oyuncunun lig/LP/kazanma oranı. Oyunun üstünde overlay yoktur; oyun
// sırasında öneri verilmez.
const Live = (() => {
  const el = (id) => document.getElementById(id);
  const PHASES = { ChampSelect: ["Champion select", "on"], InProgress: ["In game", "on"], Reconnect: ["In game", "on"],
    Lobby: ["Lobby", ""], Matchmaking: ["Matchmaking", ""], ReadyCheck: ["Ready check", ""],
    GameStart: ["Loading", "on"], EndOfGame: ["Game over", ""], WaitingForStats: ["Game over", ""] };
  const ROLES = { TOP: "Top", JUNGLE: "Jungle", MIDDLE: "Mid", BOTTOM: "ADC", UTILITY: "Support" };
  const TIERS = { IRON: "Iron", BRONZE: "Bronze", SILVER: "Silver", GOLD: "Gold", PLATINUM: "Platinum",
    EMERALD: "Emerald", DIAMOND: "Diamond", MASTER: "Master", GRANDMASTER: "Grandmaster", CHALLENGER: "Challenger" };
  const COLLECTOR = { collecting: "collecting", idle: "starting", "key expired": "paused: API key expired",
    "no players": "paused", off: "off" };

  let champs = [];        // [{id, key, name}]
  let ver = "";
  let timer = null;
  let shown = null;       // ekrandaki öneri: "champId|role"
  let pickedAuto = null;  // şampiyon seçiminden gelen son seçim
  let current = null;     // gösterilen öneri
  let pageIndex = 0;      // aktarılacak rün sayfası (listeden tıklanarak seçilir)
  let tab = "general";    // "general" ya da "matchup" (rakibe özel öneri)
  // Rakip: "auto" = şampiyon seçiminden tahmin edilir, "manual" = elle yazılır (tahmin üstüne yazmaz)
  let oppMode = "auto";
  try { oppMode = localStorage.getItem("lv-opp") || "auto"; } catch {}
  let lastPick = null;    // şampiyon seçiminden gelen son durum
  let enemies = [];       // şampiyon seçiminde rakip takımın kilitlediği şampiyonlar

  const cdn = (path) => `https://ddragon.leagueoflegends.com/cdn/${path}`;
  const champImg = (key) => cdn(`${ver}/img/champion/${key}.png`);
  const itemImg = (id) => cdn(`${ver}/img/item/${id}.png`);
  const runeImg = (icon) => cdn(`img/${icon}`);
  const pct = (v) => (v == null ? "—" : `${v}%`);
  const wrOf = (w, l) => (w + l ? Math.round((100 * w) / (w + l)) : null);

  async function open() {
    if (!champs.length) {
      try {
        champs = await api.champions();
        el("lv-champs").innerHTML = champs.map((c) => `<option value="${esc(c.name)}">`).join("");
      } catch (e) { el("lv-suggest").innerHTML = `<div class="r-empty">${esc(String(e))}</div>`; }
    }
    clearInterval(timer);
    tick();
    timer = setInterval(() => (el("view-live").classList.contains("hidden") ? clearInterval(timer) : tick()), 2000);
  }

  async function tick() {
    let s;
    try { s = await api.live(); } catch (e) { el("lv-phase").textContent = "Error"; return; }
    ver = s.ddragon;
    const [label, cls] = PHASES[s.phase] || [s.phase ? s.phase : "Client closed", ""];
    el("lv-phase").textContent = label;
    el("lv-phase").className = `state-pill ${cls}`;
    const st = s.stats;
    const regions = Object.entries(st.regions || {}).map(([r, n]) => `${r} ${n}`).join(" · ");
    el("lv-stats").textContent = `Stats: ${st.matches} high-elo matches${regions ? ` (${regions})` : ""}${st.patch ? `, ${st.patch_matches} on patch ${st.patch}` : ""} · ${COLLECTOR[st.collector] || st.collector}`;
    // şampiyon seçimindeki seçimi otomatik göster (kullanıcı elle başka şampiyon seçmediyse)
    if (s.pick) {
      if (String(s.pick.enemies) !== String(enemies)) { enemies = s.pick.enemies || []; renderEnemies(); }
      lastPick = s.pick;
      // rakip kilitledikçe AP/AD grubu değişebilir, o yüzden rakip listesi de anahtarda
      const k = `${s.pick.champion_id}|${s.pick.role}|${oppMode === "auto" ? s.pick.opponent || "" : ""}|${enemies}`;
      if (k !== pickedAuto) {
        pickedAuto = k;
        const c = champs.find((x) => x.id === s.pick.champion_id);
        if (c) el("lv-champ").value = c.name;
        el("lv-role").value = s.pick.role;
        if (oppMode === "auto") setAutoOpponent();
        load();
      }
    }
    renderScout(s.scout);
  }

  // ---------- öneri ----------
  function setAutoOpponent() {
    const o = champs.find((x) => x.id === lastPick?.opponent);
    el("lv-vs").value = o ? o.name : "";
    if (o) tab = "matchup";
  }

  function renderOppMode() {
    document.querySelectorAll("#lv-oppmode .chip").forEach((b) => b.classList.toggle("on", b.dataset.mode === oppMode));
    el("lv-vs").readOnly = oppMode === "auto";
    el("lv-vs").placeholder = oppMode === "auto" ? "Detected in champion select" : "Type the opponent";
  }

  const byName = (input) => champs.find((x) => x.name.toLowerCase() === el(input).value.trim().toLowerCase());

  // ---------- karşı pick'ler: koridor rakibine karşı en iyi / en kötü ----------
  let countersKey = "";
  async function loadCounters() {
    const opp = byName("lv-vs");
    const role = el("lv-role").value;
    const box = el("lv-counters");
    if (!opp || !role) { countersKey = ""; box.innerHTML = ""; return; }
    const key = `${opp.id}|${role}`;
    if (key === countersKey) return;
    countersKey = key;
    try {
      const c = await api.counters(opp.id, role);
      if (countersKey === key) renderCounters(c);
    } catch (e) { box.innerHTML = `<div class="r-empty">${esc(String(e))}</div>`; }
  }

  function renderCounters(c) {
    const guessed = oppMode === "auto" ? `<span class="muted small" title="Enemy roles are hidden in champion select; guessed from how often each enemy champion is played in each role">guessed lane opponent</span>` : "";
    const list = (rows, cls) => rows.length ? rows.map((r) => `
      <button class="lv-counter ${cls}" data-champ="${esc(r.name)}" title="Show runes and items for ${esc(r.name)}">
        <img src="${champImg(r.key)}" alt=""><span class="lv-c-name">${esc(r.name)}</span>
        <b>${r.wr}%</b><span class="muted small">${r.games} games</span></button>`).join("")
      : `<div class="muted small">Not enough games yet.</div>`;
    const thin = c.best.length && c.best.every((r) => r.games < 30)
      ? `<div class="muted small lv-c-note">Small samples (under 30 games each): treat these as hints, not rules. They firm up as the collector adds matches.</div>` : "";
    el("lv-counters").innerHTML = `
      <div class="lv-c-head"><img src="${champImg(c.opponent_key)}" alt="">
        <h3 class="lv-h">Picks vs ${esc(c.opponent_name)} · ${esc(c.role_name)}</h3>${guessed}
        <span class="spacer"></span><span class="muted small" title="Ranked solo queue games of Challenger, Grandmaster and Master players on TR, EUW and KR, collected in the background. Your own games are not used here.">${c.games} Master+ solo queue games (TR/EUW/KR, not yours) · ${/^\d/.test(c.scope) ? `patch ${esc(c.scope)}` : esc(c.scope)}</span></div>
      ${c.champions ? `<div class="lv-c-cols">
        <div><div class="lv-c-title good">Best picks</div>${list(c.best, "good")}</div>
        <div><div class="lv-c-title bad">Worst picks</div>${list(c.worst, "bad")}</div></div>${thin}`
      : `<div class="r-empty">No high-elo games against ${esc(c.opponent_name)} in this role yet.</div>`}`;
  }

  el("lv-counters").addEventListener("click", (e) => {
    const b = e.target.closest("[data-champ]");
    if (!b) return;
    el("lv-champ").value = b.dataset.champ;
    load();
  });

  // ---------- item gezgini: koridor rakibi + rakip takımın AP oranı aralığı ----------
  let xOpp = "lane";            // "lane" = koridor rakibine karşı, "any" = hepsi
  let xPreset = "any";
  let xMin = 0, xMax = 1;       // rakip takımın büyü hasarı oranı aralığı
  let xLast = null;             // son yanıt (eşikler, şu anki takım, kaydırıcı aralığı)
  let xTimer = null;
  let xKey = "";

  const xPresets = (t) => ({
    any: [0, 1], ad: [0, t.thresholds[0]], mixed: [t.thresholds[0], t.thresholds[1]], ap: [t.thresholds[1], 1],
    now: t.ap_now != null ? [Math.max(0, t.ap_now - 0.06), Math.min(1, t.ap_now + 0.06)] : null,
  });

  async function loadExplore() {
    const c = byName("lv-champ");
    const box = el("lv-explore");
    if (!c) { box.classList.add("hidden"); return; }
    box.classList.remove("hidden");
    const opp = byName("lv-vs");
    el("lv-x-oppname").textContent = opp ? opp.name : "—";
    el("lv-x-opp").querySelector('[data-xopp="lane"]').disabled = !opp;
    const useOpp = xOpp === "lane" && opp ? opp.id : null;
    const key = `${c.id}|${el("lv-role").value}|${useOpp}|${xMin}|${xMax}|${enemies}`;
    if (key === xKey) return;
    xKey = key;
    try {
      const t = await api.item_table(c.id, el("lv-role").value, useOpp, xMin, xMax, enemies);
      if (xKey !== key) return;
      xLast = t;
      // "şu anki takım" seçiliyse rakipler kilitledikçe aralık takımla birlikte kayar
      if (xPreset === "now" && t.ap_now != null) {
        const [a, b] = xPresets(t).now;
        if (Math.abs(a - xMin) > 0.005) { xMin = a; xMax = b; xKey = ""; return loadExplore(); }
      }
      renderExplore(t);
    } catch (e) { el("lv-x-body").innerHTML = `<div class="r-empty">${esc(String(e))}</div>`; }
  }

  function syncRange(t) {
    const [lo, hi] = t.ap_span.map((v) => Math.round(v * 100));
    for (const id of ["lv-x-min", "lv-x-max"]) { el(id).min = lo; el(id).max = hi; }
    const clamp = (v) => Math.min(hi, Math.max(lo, Math.round(v * 100)));
    el("lv-x-min").value = clamp(xMin);
    el("lv-x-max").value = clamp(xMax);
    const pos = (v) => `${((clamp(v) - lo) / (hi - lo)) * 100}%`;
    el("lv-x-fill").style.left = pos(xMin);
    el("lv-x-fill").style.right = `calc(100% - ${pos(xMax)})`;
    const now = el("lv-x-nowmark");
    now.classList.toggle("hidden", t.ap_now == null);
    if (t.ap_now != null) now.style.left = pos(t.ap_now);
    const show = (v, edge) => (v <= 0 || v >= 1 ? edge : `${Math.round(v * 100)}%`);
    el("lv-x-readout").textContent = xMin <= 0 && xMax >= 1 ? "any"
      : `${show(xMin, "0%")} – ${show(xMax, "100%")} magic`;
    el("lv-x-now").disabled = t.ap_now == null;
    el("lv-x-now").textContent = t.ap_now == null ? "Current enemy team"
      : `Current enemy team (${Math.round(t.ap_now * 100)}% magic${t.partial ? ", partial" : ""})`;
    document.querySelectorAll("#lv-x-presets .chip").forEach((b) => b.classList.toggle("on", b.dataset.preset === xPreset));
    document.querySelectorAll("#lv-x-opp .chip").forEach((b) => b.classList.toggle("on", b.dataset.xopp === (xOpp === "lane" && !b.disabled ? "lane" : "any") ));
  }

  function renderExplore(t) {
    syncRange(t);
    const delta = (v) => (Math.abs(v) < 1 ? `<span class="muted">±0</span>`
      : `<span class="lv-delta ${v > 0 ? "up" : "down"}">${v > 0 ? "▲" : "▼"}${Math.abs(v).toFixed(0)}</span>`);
    const rows = (list) => list.map((it) => `<tr>
      <td><div class="lv-x-item"><img src="${itemImg(it.id)}" alt="">${esc(it.name)}</div></td>
      <td class="num">${pct(it.pick)}</td><td class="num">${delta(it.delta)}</td>
      <td class="num">${pct(it.wr)}</td><td class="num muted">${it.games}</td></tr>`).join("");
    const filters = [t.opponent ? `vs ${esc(t.opponent_name)}` : "any opponent",
      xMin <= 0 && xMax >= 1 ? "any enemy damage" : el("lv-x-readout").textContent];
    const head = `<div class="lv-x-head"><img src="${champImg(t.champion_key)}" alt="">
      <div><b>${t.games}</b> of ${t.all_games} ${esc(t.champion_name)} games match (${filters.join(", ")})
        · ${pct(t.wr)} win · ${t.scope === "all patches" ? "all patches" : `patch ${esc(t.scope)}`}
        ${t.enough ? "" : `<div class="warn small">Under 20 games: percentages swing a lot. Widen the range or pick Any opponent.</div>`}</div></div>`;
    if (!t.games) {
      el("lv-x-body").innerHTML = head + `<div class="r-empty">No games with these filters. Widen the damage range or choose Any opponent.</div>`;
      return;
    }
    const more = t.more.length ? `<div class="lv-x-more"><span class="muted small">Bought more often than in all ${esc(t.champion_name)} games:</span>
      ${t.more.map((it) => `<span class="lv-x-tag"><img src="${itemImg(it.id)}" alt="">${esc(it.name)} <b class="lv-delta up">▲${it.delta.toFixed(0)}</b></span>`).join("")}</div>` : "";
    el("lv-x-body").innerHTML = head + more + `<table class="lv-x-table">
      <thead><tr><th>Item</th><th class="num">Bought</th><th class="num" title="Difference from all of this champion's games">vs all games</th><th class="num">Win rate</th><th class="num">Games</th></tr></thead>
      <tbody>${t.boots.length ? `<tr class="lv-x-sec"><td colspan="5">Boots</td></tr>${rows(t.boots)}` : ""}
      <tr class="lv-x-sec"><td colspan="5">Items</td></tr>${rows(t.items)}</tbody></table>`;
  }

  const xReload = () => { clearTimeout(xTimer); xTimer = setTimeout(loadExplore, 200); };
  el("lv-x-opp").addEventListener("click", (e) => {
    const b = e.target.closest("[data-xopp]");
    if (!b || b.disabled) return;
    xOpp = b.dataset.xopp;
    loadExplore();
  });
  el("lv-x-presets").addEventListener("click", (e) => {
    const b = e.target.closest("[data-preset]");
    if (!b || b.disabled || !xLast) return;
    const r = xPresets(xLast)[b.dataset.preset];
    if (!r) return;
    xPreset = b.dataset.preset;
    [xMin, xMax] = r;
    loadExplore();
  });
  for (const id of ["lv-x-min", "lv-x-max"]) {
    el(id).addEventListener("input", () => {
      let a = +el("lv-x-min").value, b = +el("lv-x-max").value;
      if (a > b) id === "lv-x-min" ? (b = a, el("lv-x-max").value = b) : (a = b, el("lv-x-min").value = a);
      // kaydırıcı ucuna çekilirse o taraf sınırsız sayılır
      xMin = a <= +el(id).min ? 0 : a / 100;
      xMax = b >= +el(id).max ? 1 : b / 100;
      xPreset = "custom";
      if (xLast) syncRange(xLast);
      xReload();
    });
  }

  async function load() {
    loadCounters();
    loadExplore();
    const c = byName("lv-champ");
    if (!c) return;
    const role = el("lv-role").value;
    const opp = byName("lv-vs");
    if (!opp) tab = "general";
    const key = `${c.id}|${role}|${opp ? opp.id : ""}|${enemies}`;
    shown = key;
    el("lv-suggest").innerHTML = `<div class="r-empty">Loading...</div>`;
    try {
      const s = await api.suggest(c.id, role, opp ? opp.id : null, enemies);
      if (shown !== key) return;
      current = s;
      pageIndex = 0;
      renderSuggest(s);
    } catch (e) { el("lv-suggest").innerHTML = `<div class="r-empty">${esc(String(e))}</div>`; }
  }

  // şampiyon seçiminde rakip takımın şampiyonları; tıklayınca rakip olarak seçilir
  function renderEnemies() {
    el("lv-enemies").innerHTML = enemies.length ? `<span class="muted small">Enemy picks:</span>` + enemies.map((id) => {
      const c = champs.find((x) => x.id === id);
      return c ? `<button class="chip lv-enemy" data-enemy="${id}" title="Use as opponent"><img src="${champImg(c.key)}" alt="">${esc(c.name)}</button>` : "";
    }).join("") : "";
  }

  // Genel öneri ile rakibe özel öneri arasında sekme; içe aktarma açık sekmeden yapılır
  function renderSuggest(full) {
    const m = full.matchup;
    if (!m) tab = "general";
    const tabs = m ? `<div class="chip-row lv-tabs">
      <button class="chip ${tab === "general" ? "on" : ""}" data-tab="general">General · ${full.games} games</button>
      <button class="chip ${tab === "matchup" ? "on" : ""}" data-tab="matchup">vs ${esc(m.champion_name)} · ${m.games} games</button></div>` : "";
    const s = tab === "matchup" ? m : full;
    el("lv-imp-runes").disabled = !s.runes.length;
    el("lv-imp-items").disabled = !s.core.length;
    const title = `${esc(full.champion_name)} ${esc(full.role_name)}${tab === "matchup" ? ` vs ${esc(m.champion_name)}` : ""}`;
    if (!s.games) {
      el("lv-suggest").innerHTML = tabs + `<div class="r-empty">${tab === "matchup"
        ? `No high-elo games of ${title} yet. Matchups fill slower than champions; the General tab still works.`
        : `No high-elo games for ${title} yet. The collector keeps adding matches while the app is open.`}</div>`;
      return;
    }
    const head = tabs + `<div class="lv-head"><img src="${champImg(full.champion)}" alt="">
      ${tab === "matchup" ? `<span class="lv-vs-sep">vs</span><img src="${champImg(m.champion)}" alt="">` : ""}<div>
      <b>${title}</b>
      <div class="muted small">${s.games} high-elo games · ${s.scope === "all patches" ? "all patches" : `patch ${esc(s.scope)}`} · ${pct(s.wr)} win rate
      ${s.enough ? "" : ` · <span class="warn">few games, take it with a grain of salt</span>`}</div></div></div>`;
    const pages = s.runes.map((p, i) => `<div class="lv-page ${i === pageIndex ? "best" : ""}" data-page="${i}"
      title="Click to choose this page, then Import runes">
      <div class="lv-runes">
        <img class="tree" src="${runeImg(p.tree_icons[0])}" title="${esc(p.tree_names[0])}" alt="">
        ${p.icons.slice(0, 4).map((ic, j) => `<img src="${runeImg(ic)}" title="${esc(p.names[j])}" alt="">`).join("")}
        <span class="sep"></span>
        <img class="tree" src="${runeImg(p.tree_icons[1])}" title="${esc(p.tree_names[1])}" alt="">
        ${p.icons.slice(4).map((ic, j) => `<img src="${runeImg(ic)}" title="${esc(p.names[j + 4])}" alt="">`).join("")}
      </div>
      <div class="lv-page-meta"><b>${esc(p.names[0])}</b> · ${esc(p.tree_names[1])}: ${p.names.slice(4).map(esc).join(", ")}
        <div class="muted small">${p.shard_names.map(esc).join(" · ")}</div></div>
      <div class="lv-num"><b>${pct(p.pick)}</b><span class="muted small">pick</span></div>
      <div class="lv-num"><b>${pct(p.wr)}</b><span class="muted small">win · ${p.games}</span></div>
    </div>`).join("");
    const items = (title, list) => list.length ? `<div class="lv-items"><h3>${title}</h3><div class="lv-item-row">
      ${list.map((it) => `<div class="lv-item" title="${esc(it.name)} · ${it.games} games">
        <img src="${itemImg(it.id)}" alt=""><span>${pct(it.pick)}</span><span class="muted">${pct(it.wr)}</span></div>`).join("")}
      </div></div>` : "";
    // timeline'dan gelen gruplar: başlangıç seti (adetli) ve item sırası (oklarla)
    const groups = (title, list, arrows) => list.length ? `<div class="lv-items"><h3>${title}</h3>
      ${list.map((g, i) => `<div class="lv-group ${i ? "" : "top"}"><div class="lv-group-items">
        ${g.items.map((it) => `<span class="lv-gi" title="${esc(it.name)}"><img src="${itemImg(it.id)}" alt="">${it.count > 1 ? `<b>${it.count}</b>` : ""}</span>`)
          .join(arrows ? `<span class="lv-arrow">›</span>` : "")}</div>
        <div class="lv-num"><b>${pct(g.pick)}</b><span class="muted small">pick</span></div>
        <div class="lv-num"><b>${pct(g.wr)}</b><span class="muted small">win · ${g.games}</span></div></div>`).join("")}</div>` : "";
    const timedNote = s.timed ? "" : `<div class="muted small lv-note">Starting items and build order appear once matches with purchase history are collected.</div>`;
    el("lv-suggest").innerHTML = head + `<div class="lv-grid"><div><h3 class="lv-h">Rune pages <span class="muted small">click one to choose what Import runes copies</span></h3>${pages}</div>
      <div><h3 class="lv-h">Items <span class="muted small">pick rate / win rate</span></h3>
      ${groups("Starting items", s.start, false)}${groups("Build order (first 3 items)", s.order, true)}${timedNote}
      ${items(s.order.length ? "Most built" : "Core", s.core)}${items("Boots", s.boots)}${items("Situational", s.situational)}</div></div>`
      + renderDamage(full);
  }

  // Rakip takımın hasar tipine göre (AD ağırlıklı / karma / AP ağırlıklı) item tercihleri.
  // Ok: o gruptaki alınma oranının, tüm maçlardaki orana göre farkı.
  function renderDamage(full) {
    const d = full.vs_damage;
    if (!d || !d.groups.some((g) => g.games)) return "";
    const delta = (v) => (Math.abs(v) < 1 ? "" : `<span class="lv-delta ${v > 0 ? "up" : "down"}">${v > 0 ? "▲" : "▼"}${Math.abs(v).toFixed(0)}</span>`);
    const row = (it) => `<div class="lv-d-item" title="${esc(it.name)}: bought in ${pct(it.pick)} of these games${it.delta != null ? ` (${it.delta > 0 ? "+" : ""}${it.delta} pts vs all games)` : ""}">
      <img src="${itemImg(it.id)}" alt=""><span class="lv-d-name">${esc(it.name)}</span><span>${pct(it.pick)}</span>${delta(it.delta)}</div>`;
    const note = d.current
      ? `Enemy team so far looks <b>${esc(d.groups.find((g) => g.key === d.current).label)}</b>${d.partial ? " (not all picks locked yet)" : ""}.`
      : "Highlights the enemy team's group once they lock champions.";
    const cols = d.groups.map((g) => `<div class="lv-d-col ${g.key === d.current ? "lv-current" : ""}">
      <div class="lv-d-title">vs ${esc(g.label)} <span class="muted small">${g.games} games · ${pct(g.wr)} win</span></div>
      ${g.games ? `${g.more.length ? `<div class="lv-d-sub">Built more often here</div>${g.more.map(row).join("")}` : ""}
        <div class="lv-d-sub">Boots</div>${g.boots.map(row).join("")}
        <div class="lv-d-sub">Core</div>${g.core.map(row).join("")}
        <div class="lv-d-sub">Situational</div>${g.situational.slice(0, 4).map(row).join("")}
        ${g.enough ? "" : `<div class="warn small">few games</div>`}` : `<div class="muted small">No games yet.</div>`}
    </div>`).join("");
    return `<div class="lv-damage"><h3 class="lv-h">Items by enemy team damage
      <span class="muted small">${esc(full.champion_name)} games split by how much of the enemy team's damage was magic · ${d.scope === "all patches" ? "all patches" : `patch ${esc(d.scope)}`}</span></h3>
      <div class="muted small lv-d-note">${note}</div><div class="lv-d-cols">${cols}</div></div>`;
  }

  async function doImport(kind) {
    if (!current) return;
    const btn = el(kind === "runes" ? "lv-imp-runes" : "lv-imp-items");
    const was = btn.textContent;
    btn.disabled = true;
    try {
      const opp = tab === "matchup" && current.matchup ? current.matchup.opponent : null;
      const title = kind === "runes" ? await api.import_runes(current.champion_id, current.role, pageIndex, opp)
        : await api.import_items(current.champion_id, current.role, opp);
      el("lv-pick-note").textContent = `Added to the client: "${title}"`;
    } catch (e) { el("lv-pick-note").textContent = String(e).replace(/^Error: /, ""); }
    btn.textContent = was;
    btn.disabled = false;
  }

  // ---------- oyuncular ----------
  // Solo/Duo ↔ Flex: lig, LP, kazanma oranı ve şampiyon istatistiği seçilen kuyruktan
  let queue = "solo";
  try { queue = localStorage.getItem("lv-queue") || "solo"; } catch {}
  let lastScout = null;
  const QNAME = { solo: "Solo/Duo", flex: "Flex" };

  function renderScout(sc) {
    lastScout = sc;
    document.querySelectorAll("#lv-queue .chip").forEach((b) => b.classList.toggle("on", b.dataset.q === queue));
    if (!sc) {
      el("lv-scout").innerHTML = `<div class="r-empty">No game in progress.</div>`;
      el("lv-scout-note").textContent = "Fills in when the loading screen starts.";
      return;
    }
    el("lv-scout-note").textContent = sc.status === "error" ? sc.error
      : `${sc.source === "preview" ? "Your last match (preview)" : "Current game"}${sc.status === "done" ? ""
        : sc.status === "season games" ? " · counting this season's games..." : " · loading..."}`;
    const rank = (r) => r ? `<b>${TIERS[r.tier] || r.tier}${["MASTER", "GRANDMASTER", "CHALLENGER"].includes(r.tier) ? "" : " " + r.rank}</b>
      <span class="muted">${r.lp} LP</span>` : `<span class="muted">Unranked</span>`;
    const wr = (r) => r ? `<b class="${wrOf(r.wins, r.losses) >= 50 ? "good" : "bad"}">${wrOf(r.wins, r.losses)}%</b>
      <span class="muted">${r.wins}W ${r.losses}L</span>` : `<span class="muted">—</span>`;
    // bu sezon o şampiyonla; tüm maçlar henüz incelenmediyse kaçının incelendiği yazar
    const onChamp = (p) => {
      const c = p.champ?.[queue];
      const total = p.season?.[queue];
      if (!c || total == null) return `<span class="muted">...</span>`;
      const progress = c.of < total ? ` · ${c.of}/${total} checked` : "";
      if (!c.games) return `<span class="muted">No games${c.of < total ? "" : " this season"}${progress}</span>`;
      return `<b class="${c.wins * 2 >= c.games ? "good" : "bad"}">${Math.round((100 * c.wins) / c.games)}%</b>
        <span class="muted">${c.wins}W ${c.games - c.wins}L${progress}</span>`;
    };
    const mastery = (m) => !m ? `<span class="muted">...</span>`
      : `<b>${m.level ? "M" + m.level : "—"}</b> <span class="muted">${m.points >= 1000 ? Math.round(m.points / 1000) + "k" : m.points} pts</span>`;
    const loaded = (p) => p.solo !== null || p.flex !== null || p.mastery;
    const team = (id, label) => {
      const ps = sc.players.filter((p) => p.team === id);
      return `<div class="lv-team ${id === 100 ? "blue" : "red"}"><div class="lv-row lv-th">
        <span>${label}</span><span>${QNAME[queue]}</span><span>Win rate</span><span>On this champion (${QNAME[queue]}, this season)</span><span>Mastery</span></div>
        ${ps.map((p) => `<div class="lv-row ${p.me ? "me" : ""}">
          <span class="lv-who"><img src="${champImg(p.champion)}" alt="">
            <span><b>${esc(p.name || "?")}${p.me ? ` <span class="you">You</span>` : ""}</b>
            <span class="muted small">${esc(p.champion_name)}${p.role ? " · " + (ROLES[p.role] || p.role) : ""}</span></span></span>
          <span>${loaded(p) ? rank(p[queue]) : `<span class="muted">...</span>`}</span>
          <span>${loaded(p) ? wr(p[queue]) : ""}</span>
          <span>${onChamp(p)}</span>
          <span>${mastery(p.mastery)}</span></div>`).join("")}</div>`;
    };
    el("lv-scout").innerHTML = team(100, "Blue team") + team(200, "Red team");
  }

  el("lv-queue").addEventListener("click", (e) => {
    const b = e.target.closest("[data-q]");
    if (!b) return;
    queue = b.dataset.q;
    try { localStorage.setItem("lv-queue", queue); } catch {}
    renderScout(lastScout);
  });
  el("lv-champ").addEventListener("change", load);
  el("lv-role").addEventListener("change", load);
  // elle: ad bir şampiyonla tam eşleştiği anda yükler (Enter beklemez); silinince genel öneriye döner
  let typed = "";
  function onTyped() {
    if (oppMode !== "manual") return;
    const o = byName("lv-vs");
    const k = o ? String(o.id) : el("lv-vs").value.trim() ? typed : "";
    if (k === typed) return;
    typed = k;
    if (o) tab = "matchup";
    load();
  }
  el("lv-vs").addEventListener("input", onTyped);
  el("lv-vs").addEventListener("change", onTyped);
  function setMode(mode) {
    oppMode = mode;
    try { localStorage.setItem("lv-opp", mode); } catch {}
    renderOppMode();
    typed = "";
    if (mode === "auto") { setAutoOpponent(); load(); } else { el("lv-vs").focus(); el("lv-vs").select(); }
  }
  el("lv-oppmode").addEventListener("click", (e) => {
    const b = e.target.closest("[data-mode]");
    if (b && b.dataset.mode !== oppMode) setMode(b.dataset.mode);
  });
  // rakip takımdan bir şampiyona tıklamak = elle seçmek
  el("lv-enemies").addEventListener("click", (e) => {
    const b = e.target.closest("[data-enemy]");
    if (!b) return;
    if (oppMode !== "manual") { oppMode = "manual"; try { localStorage.setItem("lv-opp", "manual"); } catch {} renderOppMode(); }
    el("lv-vs").value = champs.find((x) => x.id === +b.dataset.enemy)?.name || "";
    onTyped();
  });
  renderOppMode();
  el("lv-imp-runes").onclick = () => doImport("runes");
  el("lv-suggest").addEventListener("click", (e) => {
    const t = e.target.closest("[data-tab]");
    if (t && current) {
      tab = t.dataset.tab;
      pageIndex = 0;
      renderSuggest(current);
      return;
    }
    const row = e.target.closest("[data-page]");
    if (!row) return;
    pageIndex = +row.dataset.page;
    el("lv-suggest").querySelectorAll("[data-page]").forEach((r) => r.classList.toggle("best", r === row));
  });
  el("lv-imp-items").onclick = () => doImport("items");
  el("lv-preview").onclick = async () => {
    el("lv-scout-note").textContent = "Loading your last match...";
    try { await api.scout_preview(); tick(); } catch (e) { el("lv-scout-note").textContent = String(e).replace(/^Error: /, ""); }
  };

  return { open };
})();
