const $ = (s) => document.querySelector(s);
const TAGS = { kill: "Kill", death: "Death", assist: "Assist" };
const STATES = {
  stopped: "Stopped", starting: "Starting...", waiting: "Waiting for a match",
  recording: "Recording match", processing: "Preparing clips", error: "Error",
};
const SPLASH = (c, n = 0) => `https://ddragon.leagueoflegends.com/cdn/img/champion/splash/${c}_${n}.jpg`;
// skin görseli yoksa (ör. chroma) alttaki varsayılan splash görünür
const splashBg = (c, n) => `url('${SPLASH(c, n)}')` + (n ? `, url('${SPLASH(c)}')` : "");

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
  if (d < 60) return `${Math.max(1, Math.round(d))} min ago`;
  if (d < 1440) return `${Math.round(d / 60)} h ago`;
  return new Date(t * 1000).toLocaleDateString("en-GB", { day: "numeric", month: "long" });
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
  if (view !== "match") { $("#video").pause(); Draw.off(); Timers.off(); }
}

// ---------- maç listesi ----------
async function loadMatches() {
  matches = await api.matches();
  const grid = $("#grid");
  $("#empty").classList.toggle("hidden", matches.length > 0);
  $("#match-count").textContent = matches.length ? `${matches.length} matches` : "";
  grid.innerHTML = matches.map((m, i) => {
    const n = tagCounts(m.clips);
    const kda = m.kda ? `<span class="kda"><b>${m.kda[0]}</b> / <b>${m.kda[1]}</b> / <b>${m.kda[2]}</b></span>` : "";
    const res = m.win == null ? "" : `<span class="res ${m.win ? "win" : "loss"}">${m.win ? "Win" : "Loss"}</span>`;
    return `<div class="card" data-i="${i}" style="background-image:${splashBg(esc(m.icon), +m.skin || 0)}">
      ${m.recording ? `<span class="badge-full">${i === 0 ? "Last game · " : ""}Full recording</span>` : ""}
      <span class="pill">${m.clips.length} clips</span>
      <div class="card-body">
        <div class="card-champ">${esc(m.champion)}</div>
        <div class="card-meta">${res}${kda}
          <span style="color:var(--kill)">${n.kill} kills</span>
          <span style="color:var(--death)">${n.death} deaths</span>
          <span class="spacer"></span><span>${ago(m.created)}</span>
        </div>
      </div></div>`;
  }).join("");
  grid.querySelectorAll(".card").forEach((el) => (el.onclick = () => openMatch(matches[+el.dataset.i])));
}

// ---------- maç detayı ----------
// Olaysız klipler (tekrardan alınan kayıtlar) filtreden bağımsız hep görünür
function visibleClips() {
  return current.clips.filter((c) => !c.moments.length || c.moments.some((m) => filter.has(m.tag)));
}

// external: maç klasörü olmayan tek video (tekrardan alınan kayıt); silinmez, Tekrarlar'a döner
function openMatch(m, startMode = "clips") {
  current = m;
  $("#m-title").textContent = m.champion;
  $("#m-res").className = m.win == null ? "hidden" : `res ${m.win ? "win" : "loss"}`;
  $("#m-res").textContent = m.win == null ? "" : m.win ? "Win" : "Loss";
  $("#m-sub").textContent = `${m.kda ? m.kda.join(" / ") + " · " : ""}${m.match_id} · ${ago(m.created)}`;
  $("#back").textContent = m.external ? "← Replays" : "← Matches";
  $("#m-delete").classList.toggle("hidden", !!m.external);
  $("#c-delete").classList.toggle("ext-hidden", !!m.external);
  const full = document.querySelector('.mode[data-mode="full"]');
  full.disabled = !m.recording;
  full.title = m.recording ? "" : "No full recording kept for this match (only the latest matches are kept)";
  renderFilters();
  show("match");
  setMode(m.recording ? startMode : "clips");
  Analysis.load(m);
  loadMatchPlayers(m);
}

// o maç oynanırken kaydedilen 10 oyuncu (lig, LP, kazanma oranı); Solo/Flex seçimi Live ile ortak
let mpData = null;
let mpQueue = "solo";
async function loadMatchPlayers(m) {
  const box = $("#m-players");
  mpData = null;
  box.classList.toggle("hidden", !!m.external);
  if (m.external) return;
  $("#mp-body").innerHTML = `<div class="r-empty">Loading...</div>`;
  try { mpData = await api.match_players(m.match_id); } catch (e) { mpData = { players: null }; }
  if (current !== m) return;
  renderMatchPlayers();
}
function renderMatchPlayers() {
  try { mpQueue = localStorage.getItem("lv-queue") || "solo"; } catch {}
  document.querySelectorAll("#mp-queue .chip").forEach((b) => b.classList.toggle("on", b.dataset.q === mpQueue));
  $("#mp-queue").classList.toggle("hidden", !mpData?.players);
  $("#mp-body").innerHTML = mpData?.players ? Live.table(mpData.players, mpQueue, mpData.ddragon)
    : `<div class="r-empty">Player ranks weren't saved for this match. They are saved for every game from now on, while the app is open.</div>`;
  $("#mp-note").textContent = mpData?.saved ? "Ranks and LP as they were during this game" : "";
}
$("#mp-queue").onclick = (e) => {
  const b = e.target.closest("[data-q]");
  if (!b) return;
  try { localStorage.setItem("lv-queue", b.dataset.q); } catch {}
  renderMatchPlayers();
};

// oyun saati <-> video: tam maçta kaydın offset'i, klipte klibin kayıttaki başlangıcı
function currentGameTime() {
  const v = $("#video");
  if (!current || current.external) return null;
  if (mode === "full") return v.currentTime - current.recording.offset;
  const c = visibleClips()[playing];
  return c && c.start != null ? c.start + v.currentTime - current.offset : null;
}

// Analizden bir ana gitmek: tam kayıt varsa orada 5 sn öncesine, yoksa o anı içeren klibe
function seekGame(t) {
  const v = $("#video");
  const top = () => $("#view-match").scrollTo({ top: 0, behavior: "smooth" });
  if (current.recording) {
    const go = () => { v.currentTime = Math.max(0, t + current.recording.offset - 5); v.play().catch(() => {}); };
    if (mode !== "full") { setMode("full"); v.addEventListener("loadedmetadata", go, { once: true }); } else go();
    return top();
  }
  const list = visibleClips();
  const i = list.findIndex((c) => c.start != null && t >= c.start - current.offset && t <= c.start - current.offset + c.duration);
  if (i < 0) return Analysis.flash(`No video for ${mmss(t)}: there is no full recording of this match and no clip at that moment.`);
  play(i);
  v.addEventListener("loadedmetadata", () => (v.currentTime = Math.max(0, t - (list[i].start - current.offset))), { once: true });
  top();
}

// ---------- klipler / tam maç ----------
let mode = "clips";

function setMode(m) {
  mode = m;
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("on", b.dataset.mode === m));
  document.querySelectorAll(".clip-only").forEach((el) => el.classList.toggle("hidden", m === "full"));
  $("#markers").classList.toggle("hidden", m !== "full");
  playing = -1;
  renderClips();
  if (m === "full") {
    const v = $("#video");
    v.src = encodeURI(current.recording.url);
    v.addEventListener("loadedmetadata", renderMarkers, { once: true });
    $("#now-title").textContent = "Full match";
    $("#now-sub").textContent = "Click a moment in the list or on the timeline";
  } else if (visibleClips().length) play(0);
}

document.querySelectorAll(".mode").forEach((b) => (b.onclick = () => !b.disabled && setMode(b.dataset.mode)));

// tam maçta oyun saati -> video saati
const videoTime = (gameTime) => gameTime + current.recording.offset;

function renderMarkers() {
  const v = $("#video");
  if (mode !== "full" || !v.duration) return;
  const marks = current.clips.flatMap((c) => c.moments).filter((m) => filter.has(m.tag));
  $("#markers").innerHTML = marks.map((m) =>
    `<span class="mk ${m.tag}" style="left:${(videoTime(m.game_time) / v.duration) * 100}%"
      title="${mmss(m.game_time)} · ${TAGS[m.tag] || m.tag}: ${esc(m.label)}"></span>`).join("") +
    Draw.list().map((a) =>
      `<span class="mk note" style="left:${(a.t / v.duration) * 100}%" title="Drawing · ${mmss(a.t)}"></span>`).join("") +
    Analysis.markers().map((o) =>
      `<span class="mk obj" style="left:${(videoTime(o.t) / v.duration) * 100}%;background:${o.color}" title="${esc(o.title)}"></span>`).join("") +
    `<span class="head" id="head"></span>`;
}
document.addEventListener("drawings-changed", renderMarkers);
document.addEventListener("analysis-changed", renderMarkers);

$("#markers").onclick = (e) => {
  const v = $("#video");
  const r = e.currentTarget.getBoundingClientRect();
  v.currentTime = ((e.clientX - r.left) / r.width) * v.duration;
};

$("#video").addEventListener("timeupdate", () => {
  const v = $("#video");
  const head = $("#head");
  if (mode === "full" && head && v.duration) head.style.left = `${(v.currentTime / v.duration) * 100}%`;
  Analysis.setTime(currentGameTime());
});

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
    renderMarkers();
  }));
}

function renderClips() {
  const list = visibleClips();
  $("#clips").innerHTML = list.map((c, i) => `
    <li class="clip ${i === playing ? "playing" : ""}" data-i="${i}">
      <span class="clip-time">${mmss(c.time)}</span>
      <div class="clip-main">
        <div class="tags">${c.moments.map((m) => `<span class="tag ${m.tag}">${TAGS[m.tag] || m.tag}</span>`).join("")}</div>
        <div class="clip-label">${c.moments.length ? c.moments.map((m) => esc(m.label)).join(" · ") : esc(c.file)}</div>
      </div>
      <span class="clip-dur">${c.duration ? Math.round(c.duration) + " s" : ""}</span>
    </li>`).join("");
  $("#clips").querySelectorAll(".clip").forEach((el) => (el.onclick = () => play(+el.dataset.i)));
}

function play(i) {
  const list = visibleClips();
  if (!list[i]) return;
  playing = i;
  const c = list[i];
  const v = $("#video");
  if (mode === "full") {
    // tam maçta klibin olduğu ana sar; öncesini de görmek için 10 sn geriden başla
    v.currentTime = Math.max(0, videoTime(c.time) - 10);
  } else {
    v.src = encodeURI(c.url).replace(/\+/g, "%2B");
  }
  v.play().catch(() => {});
  $("#now-title").textContent = c.moments.length
    ? c.moments.map((m) => TAGS[m.tag] || m.tag).join(" + ") + ` · ${mmss(c.time)}` : c.file;
  $("#now-sub").textContent = c.moments.map((m) => m.label).join(" · ");
  renderClips();
  document.querySelector(".clip.playing")?.scrollIntoView({ block: "nearest" });
}

$("#video").addEventListener("ended", () => {
  if (mode === "clips" && $("#autonext").checked && playing + 1 < visibleClips().length) play(playing + 1);
});

// ---------- hız ve kare kare ----------
const RATES = [0.25, 0.5, 1, 1.5, 2];
const FRAME = 1 / 60;
let rate = 1;
try { rate = +localStorage.getItem("rate") || 1; } catch {}

function setRate(r) {
  rate = r;
  $("#video").playbackRate = r;
  $("#video").defaultPlaybackRate = r; // yeni klipte de korunsun
  document.querySelectorAll("#speeds button").forEach((b) => b.classList.toggle("on", +b.dataset.r === r));
  try { localStorage.setItem("rate", r); } catch {}
}
setRate(rate);
$("#video").addEventListener("loadeddata", () => ($("#video").playbackRate = rate));
document.querySelectorAll("#speeds button").forEach((b) => (b.onclick = () => setRate(+b.dataset.r)));

const seek = (d) => { const v = $("#video"); v.currentTime = Math.max(0, Math.min(v.duration || 0, v.currentTime + d)); };
const stepFrame = (d) => { $("#video").pause(); seek(d * FRAME); };
$("#back5").onclick = () => seek(-5);
$("#fwd5").onclick = () => seek(5);
$("#prevf").onclick = () => stepFrame(-1);
$("#nextf").onclick = () => stepFrame(1);

document.addEventListener("keydown", (e) => {
  if ($("#view-match").classList.contains("hidden") || e.target.matches("input, textarea")) return;
  const v = $("#video");
  const i = RATES.indexOf(rate);
  const keys = {
    " ": () => (v.paused ? v.play() : v.pause()),
    ArrowLeft: () => seek(-5), ArrowRight: () => seek(5),
    ",": () => stepFrame(-1), ".": () => stepFrame(1),
    "[": () => setRate(RATES[Math.max(0, i - 1)]), "]": () => setRate(RATES[Math.min(RATES.length - 1, i + 1)]),
  };
  if (keys[e.key]) { e.preventDefault(); keys[e.key](); }
});

$("#back").onclick = () => {
  if (current?.external) { show("replays"); loadReplays(); } else { show("matches"); loadMatches(); }
};
$("#c-show").onclick = () => {
  const c = visibleClips()[playing];
  if (c) current.external ? api.reveal_video(c.url) : api.open_folder(current.folder, c.file);
};
$("#c-delete").onclick = async () => {
  const c = visibleClips()[playing];
  if (!c || !confirm("Delete this clip?")) return;
  $("#video").removeAttribute("src"); $("#video").load(); // dosya kilidini bırak
  await api.delete_clip(current.folder, c.file);
  current.clips = current.clips.filter((x) => x.file !== c.file);
  renderFilters(); renderClips();
  play(Math.min(playing, visibleClips().length - 1));
};
$("#m-delete").onclick = async () => {
  if (!confirm(`Delete all clips of this ${current.champion} match?`)) return;
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
    $("#toggle").textContent = running ? "Stop" : "Start";

    const banner = $("#banner");
    if (s.state === "error") {
      const keyErr = /401|403|api_key/i.test(s.error);
      banner.innerHTML = keyErr
        ? `Riot API key is invalid or expired. <a id="go-settings">Enter a new one in Settings</a>.`
        : `Error: ${esc(s.error)}`;
      banner.classList.remove("hidden");
      $("#go-settings")?.addEventListener("click", () => { show("settings"); loadSettings(); });
    } else banner.classList.add("hidden");

    // klipler hazırlanınca listeyi tazele
    if (lastState === "processing" && s.state !== "processing") loadMatches();
    lastState = s.state;
  } catch (e) { /* pencere kapanıyor */ }
}

$("#toggle").onclick = async () => {
  const running = $("#toggle").textContent === "Stop";
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
  if (!rules.length) { $("#saved").textContent = "Pick at least one type."; return; }
  const data = Object.fromEntries(["api_key", "game_name", "tag_line", "pre", "post", "death_pre", "keep_recordings"].map((k) => [k, f[k].value]));
  try {
    const r = await api.save_settings({ ...data, rules });
    $("#saved").textContent = r.restarted ? "Saved." : "Saved; takes effect after the current match.";
    poll();
  } catch (err) { $("#saved").textContent = "Could not save: " + err; }
};

document.querySelectorAll(".nav").forEach((b) => (b.onclick = () => {
  show(b.dataset.view);
  if (b.dataset.view === "matches") loadMatches();
  if (b.dataset.view === "replays") loadReplays();
  if (b.dataset.view === "live") Live.open();
  if (b.dataset.view === "settings") loadSettings();
  if (b.dataset.view === "log") $("#log").scrollTop = 1e9;
}));

window.addEventListener("pywebviewready", () => {
  api = window.pywebview.api;
  loadMatches();
  poll();
  setInterval(poll, 1500);
});
