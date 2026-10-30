// Tekrarlar: maç geçmişinden tekrar indirme/açma ve açık tekrarı uzaktan yönetme.
// Yalnızca Riot'un yerel API'leri (LCU, Replay API) kullanılır; oyuna dokunulmaz.
const REPLAY_STATES = {
  download: ["Available", ""], downloading: ["Downloading", ""], checking: ["Checking", ""],
  watch: ["Downloaded", "ok"], found: ["Downloaded", "ok"],
  incompatible: ["Old patch, can't play", "bad"], missingOrExpired: ["Expired", "bad"],
  unsupported: ["Unsupported", "bad"], retryDownload: ["Retry", "bad"],
  lost: ["Lost", "bad"], error: ["Unknown", "bad"],
};
const CAMERA = [["top", "Top lane"], ["mid", "Mid lane"], ["bot", "Bot lane"],
  ["dragon", "Dragon"], ["baron", "Baron"], ["blue_base", "Blue base"], ["red_base", "Red base"]];
const REPLAY_SPEEDS = [0.5, 1, 2, 4, 8];

let client = { connected: false };
let replayMatches = [];
let pb = null;          // açık tekrarın oynatma durumu
let rcPlayers = null;
let rcSeeking = false;
let rcRecording = false;

const visible = () => !$("#view-replays").classList.contains("hidden");
const fmtSize = (b) => `${(b / 1e6).toFixed(1)} MB`;
function stateLabel(r) {
  const [label, cls] = REPLAY_STATES[r?.state] || [r?.state || "—", ""];
  return r?.state === "downloading" && r.downloadProgress != null
    ? [`${label} %${Math.round(r.downloadProgress)}`, cls] : [label, cls];
}
function flash(msg) {
  $("#r-client").textContent = msg;
  setTimeout(() => renderClient(), 5000);
}

// ---------- genel durum ----------
function renderClient() {
  $("#r-client").textContent = client.connected ? `· ${client.name}` : "· LoL client closed";
  const q = client.quota;
  if (q) $("#r-quota").textContent = `Downloads this hour: ${q.used}/${q.limit}`;
}

async function loadReplays() {
  const was = client.connected;
  try { client = await api.replay_overview(); } catch { client = { connected: false }; }
  renderClient();
  renderSetup();
  if (client.connected) loadReplayMatches();
  else if (was || !replayMatches.length) {
    replayMatches = [];
    $("#r-matches").innerHTML = `<div class="r-empty">Open the LoL client to see match history. Downloaded replays still show below.</div>`;
  }
  loadLocal();
  loadGameClips();
  pollReplay();
}

// ---------- maç geçmişi ----------
async function loadReplayMatches() {
  if (!replayMatches.length) $("#r-matches").innerHTML = `<div class="r-empty">Loading...</div>`;
  try {
    replayMatches = await api.replay_matches();
    renderReplayMatches();
  } catch (e) {
    $("#r-matches").innerHTML = `<div class="r-empty">Could not load match history: ${esc(e)}</div>`;
  }
}

function renderReplayMatches() {
  if (!replayMatches.length) { $("#r-matches").innerHTML = `<div class="r-empty">No matches.</div>`; return; }
  $("#r-matches").innerHTML = replayMatches.map((m, i) => {
    const [label, cls] = stateLabel(m.replay);
    const s = m.replay?.state;
    return `<div class="rrow">
      <span class="res ${m.win ? "win" : "loss"}">${m.win ? "Victory" : "Defeat"}</span>
      <div class="rmain"><b>${esc(m.champion)}</b><span class="muted">${esc(m.queue)}</span></div>
      <span class="kda"><b>${m.kda[0]}</b> / <b>${m.kda[1]}</b> / <b>${m.kda[2]}</b></span>
      <span class="muted">${ago(m.created)}</span>
      <span class="muted">${mmss(m.duration)}</span>
      <span class="rstate ${cls}">${label}</span>
      <div class="ract">
        ${s === "download" || s === "retryDownload" ? `<button class="btn ghost" data-dl="${i}">Download</button>` : ""}
        ${s === "watch" || s === "found" ? `<button class="btn" data-watch="${m.gameId}">Watch</button>` : ""}
      </div></div>`;
  }).join("");
  $("#r-matches").querySelectorAll("[data-dl]").forEach((b) => (b.onclick = () => download(replayMatches[+b.dataset.dl])));
  $("#r-matches").querySelectorAll("[data-watch]").forEach((b) => (b.onclick = () => watch(+b.dataset.watch)));
}

async function download(m) {
  try {
    client.quota = await api.replay_download(m);
    renderClient();
    m.replay = { state: "downloading", downloadProgress: 0 };
    renderReplayMatches();
  } catch (e) { flash(String(e)); return; }
  // indirme bitene kadar durumu izle
  for (let i = 0; i < 400; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    try { m.replay = await api.replay_state(m); } catch { break; }
    if (visible()) renderReplayMatches();
    if (!["downloading", "checking"].includes(m.replay.state)) break;
  }
  loadLocal();
}

async function watch(gameId) {
  try {
    await api.replay_watch(gameId);
    flash("Opening the replay in LoL; controls appear above once it's open.");
  } catch (e) { flash(String(e)); }
}

// ---------- bilgisayardaki tekrarlar ----------
async function loadLocal() {
  let list = [];
  try { list = await api.local_replays(); } catch {}
  if (!list.length) { $("#r-local").innerHTML = `<div class="r-empty">No downloaded replays on this PC.</div>`; return; }
  $("#r-local").innerHTML = list.map((r) => {
    const meta = r.meta;
    const team = (t) => meta ? meta.players.filter((p) => p.team === t).map((p) => esc(p.champion)).join(", ") : "";
    const winner = meta?.players.find((p) => p.win);
    return `<div class="rrow local">
      <div class="rmain"><b>${esc(r.name)}</b>
        ${meta ? `<span class="team blue">${team("blue")}</span><span class="team red">${team("red")}</span>` : ""}</div>
      <span class="muted">${meta ? mmss(meta.length) : ""}</span>
      <span class="muted">${ago(r.mtime)}</span>
      <span class="muted">${fmtSize(r.size)}</span>
      <span class="muted">${winner ? (winner.team === "blue" ? "Blue win" : "Red win") : ""}</span>
      <div class="ract"><button class="btn" data-watch="${r.gameId}" ${r.gameId && client.connected ? "" : "disabled"}
        title="${client.connected ? "" : "The LoL client must be open"}">Watch</button></div></div>`;
  }).join("");
  $("#r-local").querySelectorAll("[data-watch]").forEach((b) => (b.onclick = () => watch(+b.dataset.watch)));
}

// ---------- tekrardan alınan kayıtlar ----------
async function loadGameClips() {
  let list = [];
  try { list = await api.game_clips(); } catch {}
  if (!list.length) {
    $("#r-clips").innerHTML = `<div class="r-empty">No recordings yet. Open a replay and use "Start recording" above.</div>`;
    return;
  }
  $("#r-clips").innerHTML = list.map((c, i) => `<div class="rrow clipfile" data-i="${i}">
      <div class="rmain"><b>${esc(c.file)}</b>${c.notes ? `<span class="tag note">Drawn</span>` : ""}</div>
      <span class="muted">${ago(c.mtime)}</span><span class="muted">${fmtSize(c.size)}</span>
      <div class="ract"><button class="btn">Open</button></div></div>`).join("");
  $("#r-clips").querySelectorAll(".rrow").forEach((row) => (row.onclick = () => {
    const c = list[+row.dataset.i];
    openMatch({
      external: true, champion: "Replay recording", match_id: c.file, created: c.mtime, kda: null, recording: null,
      clips: [{ file: c.file, url: c.url, duration: 0, time: 0, moments: [] }],
    });
  }));
}

$("#r-open-replays").onclick = () => api.open_replay_folder("replays");
$("#r-open-clips").onclick = () => api.open_replay_folder("highlights");
$("#r-refresh").onclick = loadReplays;

// ---------- tekrar kontrol ----------
function renderSetup() {
  $("#rc-setup").innerHTML = client.api_enabled
    ? `<p class="muted">No replay open. Press <b>Watch</b> on a match below; controls appear here once the replay opens.</p>`
    : `<p class="muted">To control the camera and time from here, Riot's Replay API must be enabled. This is Riot's documented
         way: <code>EnableReplayApi=1</code> is added to <code>game.cfg</code> (a backup is kept). Nothing is added to the game.
         Do this before opening a replay.</p><button class="btn" id="rc-enable">Enable Replay API</button>`;
  $("#rc-enable")?.addEventListener("click", async () => {
    try { await api.replay_enable_api(); loadReplays(); } catch (e) { flash("Could not change game.cfg: " + e); }
  });
}

$("#rc-speeds").insertAdjacentHTML("beforeend",
  REPLAY_SPEEDS.map((s) => `<button data-speed="${s}">${s}×</button>`).join(""));
$("#rc-presets").innerHTML = CAMERA.map(([k, l]) => `<button class="chip" data-cam="${k}">${l}</button>`).join("");

async function rc(fn, ...args) {
  try { return await api[fn](...args); } catch (e) { flash(String(e)); }
  finally { pollReplay(true); }
}

$("#rc-speeds").querySelectorAll("[data-speed]").forEach((b) => (b.onclick = () => rc("replay_playback", { speed: +b.dataset.speed })));
$("#rc-presets").querySelectorAll("[data-cam]").forEach((b) => (b.onclick = () => rc("replay_camera", b.dataset.cam)));
$("#rc-play").onclick = () => pb && rc("replay_playback", { paused: !pb.paused });
$("#rc-back").onclick = () => pb && rc("replay_playback", { time: Math.max(0, pb.currentTime - 10) });
$("#rc-fwd").onclick = () => pb && rc("replay_playback", { time: pb.currentTime + 10 });
$("#rc-seek").oninput = () => (rcSeeking = true);
$("#rc-seek").onchange = async () => { await rc("replay_playback", { time: +$("#rc-seek").value }); rcSeeking = false; };
$("#rc-fog").onchange = (e) => rc("replay_render", { fogOfWar: e.target.checked });
$("#rc-ui").onchange = (e) => rc("replay_render", { interfaceAll: e.target.checked });
$("#rc-rec").onclick = async () => {
  if (!pb) return;
  const patch = rcRecording ? { recording: false }
    : { recording: true, codec: "webm", framesPerSecond: 60, enforceFrameRate: true, lossless: false,
        replaySpeed: 1, startTime: pb.currentTime, endTime: pb.length };
  await rc("replay_recording", patch);
  if (rcRecording) setTimeout(loadGameClips, 3000);  // durdurduysak dosya birazdan düşer
};

async function loadPlayers() {
  try { rcPlayers = await api.replay_players(); } catch { rcPlayers = null; return; }
  const btn = (p, i) => `<button class="pl" data-p="${i}">${esc(p.champion)}<span class="muted">${esc(p.name)}</span></button>`;
  $("#rc-blue").innerHTML = rcPlayers.map((p, i) => p.team === "blue" ? btn(p, i) : "").join("");
  $("#rc-red").innerHTML = rcPlayers.map((p, i) => p.team === "red" ? btn(p, i) : "").join("");
  document.querySelectorAll("#rc-blue [data-p], #rc-red [data-p]").forEach((b) => (b.onclick = async () => {
    const p = rcPlayers[+b.dataset.p];
    const ok = await rc("replay_follow", [p.name, p.full, p.champion]);
    if (ok === false) flash("Couldn't lock the camera on this player; click their portrait in the game instead.");
  }));
}

let replayTimer = null;
async function pollReplay(once = false) {
  if (!once) clearTimeout(replayTimer);
  if (!visible()) return;
  let s = { running: false };
  try { s = await api.replay_status(); } catch {}
  const running = s.running;
  $("#rc-state").textContent = running ? "Replay open" : "No replay open";
  $("#rc-state").classList.toggle("on", running);
  $("#rc-setup").classList.toggle("hidden", running);
  $("#rc-controls").classList.toggle("hidden", !running);
  if (running) {
    pb = s.playback;
    $("#rc-play").textContent = pb.paused ? "Play" : "Pause";
    $("#rc-time").textContent = `${mmss(pb.currentTime)} / ${mmss(pb.length)}`;
    $("#rc-seek").max = pb.length || 1;
    if (!rcSeeking) $("#rc-seek").value = pb.currentTime;
    $("#rc-speeds").querySelectorAll("[data-speed]").forEach((b) => b.classList.toggle("on", +b.dataset.speed === pb.speed));
    $("#rc-fog").checked = !!s.render?.fogOfWar;
    $("#rc-ui").checked = !!s.render?.interfaceAll;
    if (!rcPlayers) loadPlayers();
    try {
      const r = await api.replay_recording();
      rcRecording = !!r.recording;
      $("#rc-rec").textContent = rcRecording ? "Stop recording" : "Start recording";
      $("#rc-rec").classList.toggle("rec", rcRecording);
      if (rcRecording) $("#rc-rec-state").textContent = `Recording · ${mmss((r.currentTime || 0) - (r.startTime || 0))}`;
    } catch {}
  } else {
    pb = null;
    rcPlayers = null;
  }
  if (!once) replayTimer = setTimeout(pollReplay, 1000);
}

// görünür olduğu sürece client durumunu izle (tekrarı pollReplay izler)
setInterval(() => visible() && api && api.replay_overview().then((o) => {
  const reconnected = o.connected && !client.connected;
  client = o;
  renderClient();
  if (reconnected) loadReplays();
}).catch(() => {}), 5000);
