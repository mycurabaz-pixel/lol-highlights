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
      const k = `${s.pick.champion_id}|${s.pick.role}|${oppMode === "auto" ? s.pick.opponent || "" : ""}`;
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

  async function load() {
    const c = byName("lv-champ");
    if (!c) return;
    const role = el("lv-role").value;
    const opp = byName("lv-vs");
    if (!opp) tab = "general";
    const key = `${c.id}|${role}|${opp ? opp.id : ""}`;
    shown = key;
    el("lv-suggest").innerHTML = `<div class="r-empty">Loading...</div>`;
    try {
      const s = await api.suggest(c.id, role, opp ? opp.id : null);
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
      ${items(s.order.length ? "Most built" : "Core", s.core)}${items("Boots", s.boots)}${items("Situational", s.situational)}</div></div>`;
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
