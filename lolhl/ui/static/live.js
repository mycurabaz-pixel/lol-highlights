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
    el("lv-stats").textContent = `Stats: ${st.matches} high-elo matches${st.patch ? ` (${st.patch_matches} on patch ${st.patch})` : ""} · ${COLLECTOR[st.collector] || st.collector}`;
    // şampiyon seçimindeki seçimi otomatik göster (kullanıcı elle başka şampiyon seçmediyse)
    if (s.pick) {
      const k = `${s.pick.champion_id}|${s.pick.role}`;
      if (k !== pickedAuto) {
        pickedAuto = k;
        const c = champs.find((x) => x.id === s.pick.champion_id);
        if (c) el("lv-champ").value = c.name;
        el("lv-role").value = s.pick.role;
        load();
      }
    }
    renderScout(s.scout);
  }

  // ---------- öneri ----------
  async function load() {
    const c = champs.find((x) => x.name.toLowerCase() === el("lv-champ").value.trim().toLowerCase());
    if (!c) return;
    const role = el("lv-role").value;
    shown = `${c.id}|${role}`;
    el("lv-suggest").innerHTML = `<div class="r-empty">Loading...</div>`;
    try {
      const s = await api.suggest(c.id, role);
      if (shown !== `${c.id}|${role}`) return;
      current = s;
      renderSuggest(s);
    } catch (e) { el("lv-suggest").innerHTML = `<div class="r-empty">${esc(String(e))}</div>`; }
  }

  function renderSuggest(s) {
    const ok = s.runes.length > 0;
    el("lv-imp-runes").disabled = !ok;
    el("lv-imp-items").disabled = !s.core.length;
    if (!s.games) {
      el("lv-suggest").innerHTML = `<div class="r-empty">No high-elo games for ${esc(s.champion_name)} ${esc(s.role_name)} yet.
        The collector keeps adding matches while the app is open.</div>`;
      return;
    }
    const head = `<div class="lv-head"><img src="${champImg(s.champion)}" alt=""><div>
      <b>${esc(s.champion_name)} ${esc(s.role_name)}</b>
      <div class="muted small">${s.games} high-elo games · ${s.scope === "all patches" ? "all patches" : `patch ${esc(s.scope)}`} · ${pct(s.wr)} win rate
      ${s.enough ? "" : ` · <span class="warn">few games, take it with a grain of salt</span>`}</div></div></div>`;
    const pages = s.runes.map((p, i) => `<div class="lv-page ${i ? "" : "best"}">
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
    el("lv-suggest").innerHTML = head + `<div class="lv-grid"><div><h3 class="lv-h">Rune pages</h3>${pages}</div>
      <div><h3 class="lv-h">Items <span class="muted small">pick rate / win rate</span></h3>
      ${items("Core", s.core)}${items("Boots", s.boots)}${items("Situational", s.situational)}</div></div>`;
  }

  async function doImport(kind) {
    if (!current) return;
    const btn = el(kind === "runes" ? "lv-imp-runes" : "lv-imp-items");
    const was = btn.textContent;
    btn.disabled = true;
    try {
      const title = kind === "runes" ? await api.import_runes(current.champion_id, current.role)
        : await api.import_items(current.champion_id, current.role);
      el("lv-pick-note").textContent = `Added to the client: "${title}"`;
    } catch (e) { el("lv-pick-note").textContent = String(e).replace(/^Error: /, ""); }
    btn.textContent = was;
    btn.disabled = false;
  }

  // ---------- oyuncular ----------
  function renderScout(sc) {
    if (!sc) {
      el("lv-scout").innerHTML = `<div class="r-empty">No game in progress.</div>`;
      el("lv-scout-note").textContent = "Fills in when the loading screen starts.";
      return;
    }
    el("lv-scout-note").textContent = sc.status === "error" ? sc.error
      : `${sc.source === "preview" ? "Your last match (preview)" : "Current game"}${sc.status === "done" ? "" : " · loading..."}`;
    const rank = (r) => r ? `<b>${TIERS[r.tier] || r.tier}${["MASTER", "GRANDMASTER", "CHALLENGER"].includes(r.tier) ? "" : " " + r.rank}</b>
      <span class="muted">${r.lp} LP</span>` : `<span class="muted">Unranked</span>`;
    const wr = (r) => r ? `<b class="${wrOf(r.wins, r.losses) >= 50 ? "good" : "bad"}">${wrOf(r.wins, r.losses)}%</b>
      <span class="muted">${r.wins}W ${r.losses}L</span>` : `<span class="muted">—</span>`;
    const onChamp = (c) => !c ? `<span class="muted">...</span>` : !c.games ? `<span class="muted">0 of last ${c.of}</span>`
      : `<b class="${c.wins * 2 >= c.games ? "good" : "bad"}">${Math.round((100 * c.wins) / c.games)}%</b>
        <span class="muted">${c.wins}W ${c.games - c.wins}L of last ${c.of}</span>`;
    const mastery = (m) => !m ? `<span class="muted">...</span>`
      : `<b>${m.level ? "M" + m.level : "—"}</b> <span class="muted">${m.points >= 1000 ? Math.round(m.points / 1000) + "k" : m.points} pts</span>`;
    const team = (id, label) => {
      const ps = sc.players.filter((p) => p.team === id);
      return `<div class="lv-team ${id === 100 ? "blue" : "red"}"><div class="lv-row lv-th">
        <span>${label}</span><span>Solo/Duo</span><span>Win rate</span><span>On this champion (Solo+Flex)</span><span>Mastery</span></div>
        ${ps.map((p) => `<div class="lv-row ${p.me ? "me" : ""}">
          <span class="lv-who"><img src="${champImg(p.champion)}" alt="">
            <span><b>${esc(p.name || "?")}${p.me ? ` <span class="you">You</span>` : ""}</b>
            <span class="muted small">${esc(p.champion_name)}${p.role ? " · " + (ROLES[p.role] || p.role) : ""}</span></span></span>
          <span>${p.solo === null && !p.mastery ? `<span class="muted">...</span>` : rank(p.solo)}</span>
          <span>${p.solo === null && !p.mastery ? "" : wr(p.solo)}</span>
          <span>${onChamp(p.champ)}</span>
          <span>${mastery(p.mastery)}</span></div>`).join("")}</div>`;
    };
    el("lv-scout").innerHTML = team(100, "Blue team") + team(200, "Red team");
  }

  el("lv-champ").addEventListener("change", load);
  el("lv-role").addEventListener("change", load);
  el("lv-imp-runes").onclick = () => doImport("runes");
  el("lv-imp-items").onclick = () => doImport("items");
  el("lv-preview").onclick = async () => {
    el("lv-scout-note").textContent = "Loading your last match...";
    try { await api.scout_preview(); tick(); } catch (e) { el("lv-scout-note").textContent = String(e).replace(/^Error: /, ""); }
  };

  return { open };
})();
