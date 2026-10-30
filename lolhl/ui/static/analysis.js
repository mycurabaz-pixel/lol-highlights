// Maç analizi: gold, CS, hasar, orman, harita ve objektif dakikaları.
// Veri maç bittikten sonra Riot API'sinden gelir (lolhl/analysis.py); oyun sırasında
// hiçbir şey göstermez. Grafikler videoyla eşlenir: çizgi videonun anını gösterir,
// grafiğe tıklamak videoyu o ana götürür.
const Analysis = (() => {
  const US = "#4a8fe6", THEM = "#ef5b5b";  // koyu zeminde renk körlüğü testinden geçti
  // Oyun terimleri LoL'deki (İngilizce) adlarıyla
  const DRAGONS = {
    fire: ["Infernal", "#ff7a45"], ocean: ["Ocean", "#4fc3f7"], mountain: ["Mountain", "#b08d57"],
    cloud: ["Cloud", "#cfd8dc"], hextech: ["Hextech", "#26c6da"], chemtech: ["Chemtech", "#9ccc65"],
    elder: ["Elder", "#c8aa6e"],
  };
  // olay -> [kısa simge, ad, renk]
  const OBJ = {
    dragon: ["D", "Drake", "#ff7a45"], soul: ["S", "Dragon Soul", "#c8aa6e"], rift: ["R", "Rift", "#8a90a0"],
    baron: ["B", "Baron Nashor", "#b57edc"], herald: ["H", "Rift Herald", "#9f7aea"], grubs: ["G", "Void Grubs", "#7e57c2"],
    atakhan: ["A", "Atakhan", "#e57373"], tower: ["T", "Tower", "#8a90a0"], inhibitor: ["I", "Inhibitor", "#8a90a0"],
  };
  const GROUPS = {
    dragon: ["Dragon", ["dragon", "soul", "rift"]], baron: ["Baron", ["baron"]],
    other: ["Herald · Grubs · Atakhan", ["herald", "grubs", "atakhan"]],
    tower: ["Tower", ["tower", "inhibitor"]], kill: ["Kill", ["kill"]],
  };
  const ROLE = { TOP: "Top", JUNGLE: "Jungle", MIDDLE: "Mid", BOTTOM: "ADC", UTILITY: "Support" };
  const TIER = { OUTER_TURRET: "Outer", INNER_TURRET: "Inner", BASE_TURRET: "Inhibitor", NEXUS_TURRET: "Nexus" };
  const LANE = { top: "Top", mid: "Mid", bot: "Bot" };

  const el = (id) => document.getElementById(id);
  const cache = new Map();
  let data = null;
  let scope = "me";
  let shown = { dragon: true, baron: true, other: true, tower: false, kill: true };
  try { shown = { ...shown, ...JSON.parse(localStorage.getItem("an-shown") || "{}") }; } catch {}
  let charts = [];
  let gameTime = null;
  let mapFollow = true;
  const seek = (t) => seekGame(t);  // app.js

  // ---------- yardımcılar ----------
  const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const k = (v) => Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : `${Math.round(v)}`;
  const signed = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + k(Math.abs(v));
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const who = (id) => data.participants.find((p) => p.id === id);
  const us = (team) => team === data.my_team;
  const ddv = () => data.game_version.split(".").slice(0, 2).join(".") + ".1";
  const champImg = (c) => `https://ddragon.leagueoflegends.com/cdn/${ddv()}/img/champion/${c}.png`;

  function teamIds(mine) {
    return data.participants.filter((p) => us(p.team) === mine).map((p) => p.id);
  }
  function sum(ids, key) {
    return data.t.map((_, i) => ids.reduce((s, id) => s + (data.series[id][key][i] || 0), 0));
  }
  // Kişisel: sen ve koridor rakibin. Takım: iki takımın toplamı.
  function pair(key) {
    if (scope === "team") {
      return [{ label: "Your team", color: US, values: sum(teamIds(true), key) },
              { label: "Enemy team", color: THEM, values: sum(teamIds(false), key) }];
    }
    const out = [{ label: `You (${who(data.me).champion})`, color: US, values: data.series[data.me][key] }];
    if (data.opponent) out.push({ label: `Opponent (${who(data.opponent).champion})`, color: THEM, values: data.series[data.opponent][key] });
    return out;
  }

  function eventName(e) {
    if (e.type === "dragon") return `${(DRAGONS[e.sub] || [""])[0]} Drake`.trim();
    if (e.type === "soul") return `${(DRAGONS[e.sub] || [e.sub])[0]} Soul`;
    if (e.type === "rift") return `${(DRAGONS[e.sub] || [e.sub])[0]} Rift`;
    if (e.type === "tower") return `${LANE[e.lane] || ""} ${TIER[e.tier] || ""} Tower`.trim();
    if (e.type === "inhibitor") return `${LANE[e.lane] || ""} Inhibitor`.trim();
    return OBJ[e.type]?.[1] || e.type;
  }
  const eventColor = (e) => e.type === "dragon" ? (DRAGONS[e.sub] || OBJ.dragon)[1] : OBJ[e.type]?.[2] || "#8a90a0";
  const visibleTypes = () => Object.entries(GROUPS).filter(([g]) => shown[g]).flatMap(([, [, t]]) => t);
  const objectives = () => data.events.filter((e) => e.type !== "kill" && visibleTypes().includes(e.type));
  // kişiselde yalnızca senin ya da rakibinin içinde olduğu kill'ler
  function kills() {
    if (!shown.kill) return [];
    const ks = data.events.filter((e) => e.type === "kill");
    if (scope === "team") return ks;
    const mine = [data.me, data.opponent];
    return ks.filter((e) => mine.includes(e.killer) || mine.includes(e.victim) || e.assists.some((a) => mine.includes(a)));
  }

  function niceTicks(lo, hi, n = 4) {
    const span = hi - lo || 1;
    const step0 = span / n;
    const mag = 10 ** Math.floor(Math.log10(step0));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
    const out = [];
    for (let v = Math.floor(lo / step) * step; v <= hi + step * 0.001; v += step) out.push(Math.round(v * 1e6) / 1e6);
    if (out[out.length - 1] < hi) out.push(out[out.length - 1] + step);
    return out;
  }

  // ---------- çizgi grafiği ----------
  // opts: series [{label,color,values}], diff (fark grafiği: tek seri, 0 çizgisine göre iki renk), fmt
  function lineChart(host, opts) {
    const W = Math.max(320, host.clientWidth), H = 200;
    const m = { l: 46, r: 14, t: 20, b: 24 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const T = data.t, dur = data.duration;
    const x = (s) => m.l + (Math.min(s, dur) / dur) * iw;
    const all = opts.series.flatMap((s) => s.values);
    let lo = Math.min(...all), hi = Math.max(...all);
    if (opts.diff) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    else lo = Math.min(lo, 0);
    const ticks = niceTicks(lo, hi === lo ? lo + 1 : hi);
    lo = ticks[0]; hi = ticks[ticks.length - 1];
    const y = (v) => m.t + ih - ((v - lo) / (hi - lo)) * ih;
    const path = (vals) => vals.map((v, i) => `${i ? "L" : "M"}${x(T[i]).toFixed(1)},${y(v).toFixed(1)}`).join("");
    const id = host.id;

    let svg = `<svg width="${W}" height="${H}" class="an-svg">`;
    for (const v of ticks) {
      svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" class="${v === 0 && opts.diff ? "zero" : "grid"}"/>
        <text x="${m.l - 6}" y="${y(v) + 4}" class="ax" text-anchor="end">${opts.diff ? signed(v) : k(v)}</text>`;
    }
    for (let s = 300; s < dur; s += 300) {
      svg += `<text x="${x(s)}" y="${H - 6}" class="ax" text-anchor="middle">${s / 60} min</text>`;
    }
    for (const e of objectives()) {
      const c = eventColor(e);
      svg += `<g class="obj"><line x1="${x(e.t)}" x2="${x(e.t)}" y1="${m.t}" y2="${m.t + ih}" stroke="${c}"/>
        <text x="${x(e.t)}" y="${m.t - 6}" text-anchor="middle" fill="${c}">${OBJ[e.type][0]}</text>
        <title>${mmss(e.t)} · ${esc(eventName(e))} · ${e.team ? (us(e.team) ? "Us" : "Enemy") : ""}</title></g>`;
    }
    for (const e of kills()) {
      svg += `<circle cx="${x(e.t)}" cy="${m.t + ih - 3}" r="3" fill="${us(e.team) ? US : THEM}" class="kdot"/>`;
    }
    if (opts.diff) {
      const vals = opts.series[0].values;
      const area = `${path(vals)}L${x(T[T.length - 1])},${y(0)}L${x(T[0])},${y(0)}Z`;
      svg += `<clipPath id="${id}-up"><rect x="0" y="0" width="${W}" height="${y(0)}"/></clipPath>
        <clipPath id="${id}-dn"><rect x="0" y="${y(0)}" width="${W}" height="${H}"/></clipPath>
        <path d="${area}" fill="${US}" opacity=".22" clip-path="url(#${id}-up)"/>
        <path d="${area}" fill="${THEM}" opacity=".22" clip-path="url(#${id}-dn)"/>
        <path d="${path(vals)}" class="ln" stroke="${US}" clip-path="url(#${id}-up)"/>
        <path d="${path(vals)}" class="ln" stroke="${THEM}" clip-path="url(#${id}-dn)"/>`;
    } else {
      for (const s of opts.series) svg += `<path d="${path(s.values)}" class="ln" stroke="${s.color}"/>`;
    }
    svg += `<line class="cursor" y1="${m.t}" y2="${m.t + ih}" visibility="hidden"/>
      <g class="hover" visibility="hidden"><line class="cross" y1="${m.t}" y2="${m.t + ih}"/>
        ${opts.series.map((s, i) => `<circle r="4" data-i="${i}" fill="${opts.diff ? "var(--text)" : s.color}"/>`).join("")}</g>
      <rect x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent" class="hit"/></svg>`;

    const legend = opts.diff
      ? `<span><i style="background:${US}"></i>${opts.upLabel}</span><span><i style="background:${THEM}"></i>${opts.downLabel}</span>`
      : opts.series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("");
    host.innerHTML = `<div class="an-legend">${legend}</div>${svg}<div class="an-tip hidden"></div>`;

    const root = host.querySelector("svg");
    const hover = root.querySelector(".hover");
    const tip = host.querySelector(".an-tip");
    const cursor = root.querySelector(".cursor");
    const timeAt = (ev) => Math.max(0, Math.min(dur, ((ev.clientX - root.getBoundingClientRect().left - m.l) / iw) * dur));
    const idxAt = (ev) => {
      const s = timeAt(ev);
      let best = 0;
      T.forEach((t, i) => { if (Math.abs(t - s) < Math.abs(T[best] - s)) best = i; });
      return best;
    };
    const hit = root.querySelector(".hit");
    hit.addEventListener("mousemove", (ev) => {
      const i = idxAt(ev);
      const cx = x(T[i]);
      hover.setAttribute("visibility", "visible");
      hover.querySelector(".cross").setAttribute("x1", cx);
      hover.querySelector(".cross").setAttribute("x2", cx);
      hover.querySelectorAll("circle").forEach((c) => {
        c.setAttribute("cx", cx);
        c.setAttribute("cy", y(opts.series[+c.dataset.i].values[i]));
      });
      const rows = (opts.tipRows ? opts.tipRows(i) : opts.series.map((s) => [s.label, s.color, opts.fmt(s.values[i])]))
        .map(([l, c, v]) => `<div><i style="background:${c}"></i>${esc(l)}<b>${v}</b></div>`).join("");
      tip.innerHTML = `<div class="tt">${mmss(T[i])}</div>${rows}<div class="hint">Click: jump to ${mmss(timeAt(ev))} in the video</div>`;
      tip.classList.remove("hidden");
      const left = cx + 14 + tip.offsetWidth > W ? cx - tip.offsetWidth - 14 : cx + 14;
      tip.style.left = `${left}px`;
    });
    hit.addEventListener("mouseleave", () => { hover.setAttribute("visibility", "hidden"); tip.classList.add("hidden"); });
    hit.addEventListener("click", (ev) => seek(timeAt(ev)));
    charts.push({ setCursor(t) {
      if (t == null || t < 0 || t > dur) return cursor.setAttribute("visibility", "hidden");
      cursor.setAttribute("x1", x(t)); cursor.setAttribute("x2", x(t)); cursor.setAttribute("visibility", "visible");
    } });
  }

  // ---------- hasar dağılımı ----------
  function damageShare() {
    const ps = [...data.participants].sort((a, b) => (us(b.team) - us(a.team)) || b.damage - a.damage);
    const max = Math.max(...ps.map((p) => p.damage));
    el("an-share").innerHTML = ps.map((p) => {
      const d = p.damage_types;
      const tip = `Physical ${k(d.physical)} · Magic ${k(d.magic)} · True ${k(d.true)} · Taken ${k(p.taken)}`;
      return `<div class="bar-row${p.id === data.me ? " me" : ""}" title="${tip}">
        <span class="bn">${esc(p.champion)}${p.id === data.me ? " <em>You</em>" : ""}</span>
        <span class="bt"><span class="bf" style="width:${(p.damage / max) * 100}%;background:${us(p.team) ? US : THEM}"></span></span>
        <span class="bv">${k(p.damage)}</span></div>`;
    }).join("");
  }

  // ---------- objektif dakikaları ----------
  function table() {
    const rows = data.events.filter((e) => e.type !== "kill" && e.type !== "plate" && visibleTypes().includes(e.type));
    el("an-table").innerHTML = rows.length ? rows.map((e) => {
      const killer = e.killer && who(e.killer);
      return `<tr data-t="${e.t}"><td class="mono">${mmss(e.t)}</td>
        <td><span class="glyph" style="color:${eventColor(e)}">${OBJ[e.type][0]}</span>${esc(eventName(e))}</td>
        <td>${e.team ? `<span class="side ${us(e.team) ? "us" : "them"}">${us(e.team) ? "Us" : "Enemy"}</span>` : ""}</td>
        <td class="muted">${killer ? esc(killer.champion) : ""}</td></tr>`;
    }).join("") : `<tr><td class="muted">No objectives of the selected types.</td></tr>`;
    el("an-table").querySelectorAll("tr[data-t]").forEach((r) => (r.onclick = () => seek(+r.dataset.t)));
  }

  // ---------- harita ----------
  const MAP = 15000;
  function renderMap() {
    const i = +el("an-minute").value;
    const S = 300;
    const px = (v) => (v / MAP) * S, py = (v) => S - (v / MAP) * S;
    const t = data.t[i];
    el("an-minute-label").textContent = `${mmss(t)}`;
    let svg = `<svg viewBox="0 0 ${S} ${S}" class="an-map-svg">
      <rect width="${S}" height="${S}" fill="#0f1a14"/>
      <image href="https://ddragon.leagueoflegends.com/cdn/${ddv()}/img/map/map11.png" width="${S}" height="${S}" opacity=".85" onerror="this.remove()"/>`;
    // bu dakikanın ±30 sn'sindeki olaylar
    for (const e of data.events) {
      if (e.x == null || Math.abs(e.t - t) > 30) continue;
      if (e.type === "kill" && shown.kill) {
        svg += `<g class="ev"><path d="M${px(e.x) - 4},${py(e.y) - 4}l8,8m0,-8l-8,8" stroke="${us(e.team) ? US : THEM}" stroke-width="2.5"/>
          <title>${mmss(e.t)} · ${esc(who(e.killer)?.champion || "Minion/Tower")} → ${esc(who(e.victim)?.champion || "")}</title></g>`;
      } else if (OBJ[e.type] && visibleTypes().includes(e.type)) {
        svg += `<g class="ev"><circle cx="${px(e.x)}" cy="${py(e.y)}" r="8" fill="#0e1015" stroke="${eventColor(e)}" stroke-width="2"/>
          <text x="${px(e.x)}" y="${py(e.y) + 3.5}" text-anchor="middle" fill="${eventColor(e)}" class="g">${OBJ[e.type][0]}</text>
          <title>${mmss(e.t)} · ${esc(eventName(e))}</title></g>`;
      }
    }
    for (const p of data.participants) {
      const s = data.series[p.id];
      if (s.x[i] == null) continue;
      const cx = px(s.x[i]), cy = py(s.y[i]);
      const ring = p.id === data.me ? "#ffffff" : us(p.team) ? US : THEM;
      svg += `<g class="pl"><clipPath id="mc${p.id}"><circle cx="${cx}" cy="${cy}" r="10"/></clipPath>
        <circle cx="${cx}" cy="${cy}" r="11.5" fill="${us(p.team) ? US : THEM}"/>
        <image href="${champImg(p.champion)}" x="${cx - 10}" y="${cy - 10}" width="20" height="20" clip-path="url(#mc${p.id})"/>
        ${p.id === data.me ? `<circle cx="${cx}" cy="${cy}" r="13" fill="none" stroke="${ring}" stroke-width="1.5"/>` : ""}
        <title>${esc(p.champion)} · ${ROLE[p.role] || ""} · ${k(s.gold[i])} gold · ${s.cs[i]} CS · Sv ${s.level[i]}</title></g>`;
    }
    el("an-map").innerHTML = svg + "</svg>";
  }

  // ---------- genel ----------
  function render() {
    if (!data) return;
    charts = [];
    const me = who(data.me), opp = data.opponent && who(data.opponent);
    el("an-vs").textContent = scope === "me"
      ? `${me.champion} (${ROLE[me.role] || "?"})${opp ? ` vs ${opp.champion}` : " · no lane opponent found"}`
      : "Your team vs enemy team";

    const [a, b] = pair("gold");
    const diff = a.values.map((v, i) => v - (b ? b.values[i] : 0));
    lineChart(el("ch-gold"), {
      series: [{ label: "Difference", color: US, values: diff }], diff: true,
      upLabel: scope === "me" ? "You ahead" : "Your team ahead", downLabel: scope === "me" ? "Opponent ahead" : "Enemy team ahead",
      tipRows: (i) => [["Difference", diff[i] >= 0 ? US : THEM, signed(diff[i])],
        [a.label, US, k(a.values[i])], ...(b ? [[b.label, THEM, k(b.values[i])]] : [])],
    });
    lineChart(el("ch-cs"), {
      series: pair("cs"), fmt: (v) => `${v}`,
      tipRows: (i) => pair("cs").map((s) => [s.label, s.color, `${s.values[i]} (${(s.values[i] / Math.max(1, data.t[i] / 60)).toFixed(1)}/min)`]),
    });
    lineChart(el("ch-dmg"), { series: pair("damage"), fmt: k });

    const jg = (mine) => data.participants.find((p) => us(p.team) === mine && p.role === "JUNGLE");
    const [jUs, jThem] = [jg(true), jg(false)];
    el("an-jungle-card").classList.toggle("hidden", !jUs || !jThem);
    if (jUs && jThem) {
      lineChart(el("ch-jungle"), {
        series: [{ label: `${jUs.champion} (our jungler)`, color: US, values: data.series[jUs.id].jungle_cs },
                 { label: `${jThem.champion} (enemy jungler)`, color: THEM, values: data.series[jThem.id].jungle_cs }],
        fmt: (v) => `${v}`,
      });
    }
    damageShare();
    table();
    el("an-minute").max = data.t.length - 1;
    renderMap();
    setTime(gameTime);
  }

  function renderToggles() {
    el("an-toggles").innerHTML = Object.entries(GROUPS).map(([g, [label]]) =>
      `<button class="chip ${shown[g] ? "on" : ""}" data-g="${g}">${label}</button>`).join("");
    el("an-toggles").querySelectorAll("[data-g]").forEach((b) => (b.onclick = () => {
      shown[b.dataset.g] = !shown[b.dataset.g];
      try { localStorage.setItem("an-shown", JSON.stringify(shown)); } catch {}
      renderToggles();
      render();
      document.dispatchEvent(new CustomEvent("analysis-changed"));
    }));
  }

  function setTime(t) {
    gameTime = t;
    charts.forEach((c) => c.setCursor(t));
    if (data && mapFollow && t != null && t >= 0) {
      let i = 0;
      data.t.forEach((ft, j) => { if (ft <= t) i = j; });
      if (+el("an-minute").value !== i) { el("an-minute").value = i; renderMap(); }
    }
  }

  function message(text) {
    el("an-msg").textContent = text;
    el("an-msg").classList.toggle("hidden", !text);
  }

  async function load(match) {
    data = null;
    charts = [];
    el("analysis").classList.toggle("hidden", !!match.external);
    el("an-body").classList.add("hidden");
    if (match.external) return;
    message("Loading analysis...");
    try {
      const d = cache.get(match.folder) || await window.pywebview.api.match_analysis(match.folder);
      cache.set(match.folder, d);
      if (current !== match) return;  // bu sırada başka maça geçildi
      data = d;
      message("");
      el("an-body").classList.remove("hidden");
      render();
      document.dispatchEvent(new CustomEvent("analysis-changed"));
    } catch (e) {
      message(`Could not load analysis: ${e}`);
    }
  }

  document.querySelectorAll("#an-scope button").forEach((b) => (b.onclick = () => {
    scope = b.dataset.scope;
    document.querySelectorAll("#an-scope button").forEach((x) => x.classList.toggle("on", x === b));
    render();
  }));
  el("an-minute").addEventListener("input", () => { mapFollow = false; el("an-follow").checked = false; renderMap(); });
  el("an-follow").addEventListener("change", (e) => { mapFollow = e.target.checked; setTime(gameTime); });
  let resizeTimer;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });
  renderToggles();

  return {
    load, setTime,
    data: () => data,
    flash: (text) => { message(text); setTimeout(() => message(""), 6000); },
    // tam maç çizelgesi için: seçili objektif ve kill'ler (oyun saati)
    markers: () => data ? [
      ...objectives().filter((e) => e.type !== "rift").map((e) => ({ t: e.t, color: eventColor(e), title: `${mmss(e.t)} · ${eventName(e)}` })),
    ] : [],
  };
})();
