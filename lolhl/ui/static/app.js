const $ = (s) => document.querySelector(s);
const TAGS = { kill: "Kill", death: "Ölüm", assist: "Asist" };
const STATES = {
  stopped: "Durduruldu", starting: "Başlatılıyor...", waiting: "Maç bekleniyor",
  recording: "Maç kaydediliyor", processing: "Klipler hazırlanıyor", error: "Hata",
};
const SPLASH = (c) => `https://ddragon.leagueoflegends.com/cdn/img/champion/splash/${c}_0.jpg`;

let api;
let matches = [];
let current = null;      // açık maç
let playing = -1;        // görünen listedeki index
let filter = new Set(["kill", "death", "assist"]);
let logCount = 0;
let lastState = "";

// ---------- yardımcılar ----------
const mmss = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const ago = (t) => {
  const d = (Date.now() / 1000 - t) / 60;
  if (d < 60) return `${Math.max(1, Math.round(d))} dk önce`;
  if (d < 1440) return `${Math.round(d / 60)} saat önce`;
  return new Date(t * 1000).toLocaleDateString("tr-TR", { day: "numeric", month: "long" });
};
const tagCounts = (clips) => {
  const n = { kill: 0, death: 0, assist: 0 };
  clips.forEach((c) => c.moments.forEach((m) => n[m.tag] !== undefined && n[m.tag]++));
  return n;
};

function show(view) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  $(`#view-${view}`).classList.remove("hidden");
  document.querySelectorAll(".nav").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view || (view === "match" && b.dataset.view === "matches")));
  if (view !== "match") $("#video").pause();
}

// ---------- maç listesi ----------
async function loadMatches() {
  matches = await api.matches();
  const grid = $("#grid");
  $("#empty").classList.toggle("hidden", matches.length > 0);
  $("#match-count").textContent = matches.length ? `${matches.length} maç` : "";
  grid.innerHTML = matches.map((m, i) => {
    const n = tagCounts(m.clips);
    const kda = m.kda ? `<span class="kda"><b>${m.kda[0]}</b> / <b>${m.kda[1]}</b> / <b>${m.kda[2]}</b></span>` : "";
    return `<div class="card" data-i="${i}" style="background-image:url('${SPLASH(esc(m.icon))}')">
      <span class="pill">${m.clips.length} klip</span>
      <div class="card-body">
        <div class="card-champ">${esc(m.champion)}</div>
        <div class="card-meta">${kda}
          <span style="color:var(--kill)">${n.kill} kill</span>
          <span style="color:var(--death)">${n.death} ölüm</span>
          <span class="spacer"></span><span>${ago(m.created)}</span>
        </div>
      </div></div>`;
  }).join("");
  grid.querySelectorAll(".card").forEach((el) => (el.onclick = () => openMatch(matches[+el.dataset.i])));
}

// ---------- maç detayı ----------
function visibleClips() {
  return current.clips.filter((c) => c.moments.some((m) => filter.has(m.tag)));
}

function openMatch(m) {
  current = m;
  $("#m-title").textContent = m.champion;
  $("#m-sub").textContent = `${m.kda ? m.kda.join(" / ") + " · " : ""}${m.match_id} · ${ago(m.created)}`;
  renderFilters();
  renderClips();
  show("match");
  if (visibleClips().length) play(0);
}

function renderFilters() {
  const n = tagCounts(current.clips);
  $("#filters").innerHTML = Object.keys(TAGS).filter((t) => n[t]).map((t) =>
    `<button class="chip ${filter.has(t) ? "on" : ""}" data-t="${t}">${TAGS[t]} · ${n[t]}</button>`).join("");
  $("#filters").querySelectorAll(".chip").forEach((b) => (b.onclick = () => {
    const t = b.dataset.t;
    filter.has(t) ? filter.delete(t) : filter.add(t);
    if (!filter.size) filter.add(t); // hepsi kapanmasın
    playing = -1;
    renderFilters();
    renderClips();
  }));
}

function renderClips() {
  const list = visibleClips();
  $("#clips").innerHTML = list.map((c, i) => `
    <li class="clip ${i === playing ? "playing" : ""}" data-i="${i}">
      <span class="clip-time">${mmss(c.time)}</span>
      <div class="clip-main">
        <div class="tags">${c.moments.map((m) => `<span class="tag ${m.tag}">${TAGS[m.tag] || m.tag}</span>`).join("")}</div>
        <div class="clip-label">${c.moments.map((m) => esc(m.label)).join(" · ")}</div>
      </div>
      <span class="clip-dur">${Math.round(c.duration)} sn</span>
    </li>`).join("");
  $("#clips").querySelectorAll(".clip").forEach((el) => (el.onclick = () => play(+el.dataset.i)));
}

function play(i) {
  const list = visibleClips();
  if (!list[i]) return;
  playing = i;
  const c = list[i];
  $("#video").src = encodeURI(c.url).replace(/\+/g, "%2B");
  $("#video").play().catch(() => {});
  $("#now-title").textContent = c.moments.map((m) => TAGS[m.tag] || m.tag).join(" + ") + ` · ${mmss(c.time)}`;
  $("#now-sub").textContent = c.moments.map((m) => m.label).join(" · ");
  renderClips();
  document.querySelector(".clip.playing")?.scrollIntoView({ block: "nearest" });
}

$("#video").addEventListener("ended", () => {
  if ($("#autonext").checked && playing + 1 < visibleClips().length) play(playing + 1);
});

$("#back").onclick = () => { show("matches"); loadMatches(); };
$("#c-show").onclick = () => { const c = visibleClips()[playing]; c && api.open_folder(current.folder, c.file); };
$("#c-delete").onclick = async () => {
  const c = visibleClips()[playing];
  if (!c || !confirm("Bu klip silinsin mi?")) return;
  $("#video").removeAttribute("src"); $("#video").load(); // dosya kilidini bırak
  await api.delete_clip(current.folder, c.file);
  current.clips = current.clips.filter((x) => x.file !== c.file);
  renderFilters(); renderClips();
  play(Math.min(playing, visibleClips().length - 1));
};
$("#m-delete").onclick = async () => {
  if (!confirm(`${current.champion} maçının tüm klipleri silinsin mi?`)) return;
  $("#video").removeAttribute("src"); $("#video").load();
  await api.delete_match(current.folder);
  show("matches"); loadMatches();
};

// ---------- durum ----------
async function poll() {
  try {
    const s = await api.state(logCount);
    logCount = s.log_count;
    if (s.logs.length) {
      const log = $("#log");
      log.textContent += s.logs.join("\n") + "\n";
    }
    $("#status").dataset.state = s.state;
    $("#status-text").textContent = STATES[s.state] || s.state;
    const running = !["stopped", "error"].includes(s.state);
    $("#toggle").textContent = running ? "Durdur" : "Başlat";

    const banner = $("#banner");
    if (s.state === "error") {
      const keyErr = /401|403|api_key/i.test(s.error);
      banner.innerHTML = keyErr
        ? `Riot API anahtarı geçersiz ya da süresi dolmuş. <a id="go-settings">Ayarlar'dan yenisini gir</a>.`
        : `Hata: ${esc(s.error)}`;
      banner.classList.remove("hidden");
      $("#go-settings")?.addEventListener("click", () => { show("settings"); loadSettings(); });
    } else banner.classList.add("hidden");

    // klipler hazırlanınca listeyi tazele
    if (lastState === "processing" && s.state !== "processing") loadMatches();
    lastState = s.state;
  } catch (e) { /* pencere kapanıyor */ }
}

$("#toggle").onclick = async () => {
  const running = $("#toggle").textContent === "Durdur";
  if (running) await api.stop(); else await api.start();
  poll();
};
$("#open-clips").onclick = () => api.open_folder();

// ---------- ayarlar ----------
async function loadSettings() {
  const s = await api.settings();
  const f = $("#settings");
  for (const k of ["api_key", "game_name", "tag_line", "pre", "post", "death_pre", "keep_recordings"]) f[k].value = s[k];
  f.querySelectorAll("[name=rule]").forEach((c) => (c.checked = s.rules.includes(c.value)));
  $("#saved").textContent = "";
}

$("#settings").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const rules = [...f.querySelectorAll("[name=rule]:checked")].map((c) => c.value);
  if (!rules.length) { $("#saved").textContent = "En az bir tür seç."; return; }
  const data = Object.fromEntries(["api_key", "game_name", "tag_line", "pre", "post", "death_pre", "keep_recordings"].map((k) => [k, f[k].value]));
  try {
    const r = await api.save_settings({ ...data, rules });
    $("#saved").textContent = r.restarted ? "Kaydedildi." : "Kaydedildi; maç bitince geçerli olacak.";
    poll();
  } catch (err) { $("#saved").textContent = "Kaydedilemedi: " + err; }
};

document.querySelectorAll(".nav").forEach((b) => (b.onclick = () => {
  show(b.dataset.view);
  if (b.dataset.view === "matches") loadMatches();
  if (b.dataset.view === "settings") loadSettings();
  if (b.dataset.view === "log") $("#log").scrollTop = 1e9;
}));

window.addEventListener("pywebviewready", () => {
  api = window.pywebview.api;
  loadMatches();
  poll();
  setInterval(poll, 1500);
});
