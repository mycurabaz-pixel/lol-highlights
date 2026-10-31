"""Rün / item önerileri için istatistik veritabanı ve toplayıcı.

Riot API öneri vermez; Blitz/Porofessor gibi biz de maç verisinden çıkarırız. Toplayıcı
uygulama açıkken arka planda Master+ oyuncuların son dereceli maçlarını Match-V5'ten
çeker (istek sınırının bir payını ön plana bırakarak) ve her oyuncudan rün sayfasını,
bitmiş itemlerini, rolünü ve sonucunu stats.db'ye yazar. Oyuncu bilgisi ekranında
çekilen maçlar da buraya eklenir. Öneriler yalnızca şampiyon seçiminde, oyun başlamadan
verilir; oyun sırasında öneri yoktur.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import ddragon
from .riot_api import REGIONAL, KeyExpired, RiotApi

DB = Path(__file__).resolve().parents[1] / "stats.db"
# Toplanan bölgeler. Match-V5 istekleri bölgesel kümeye gider (TR ve EUW ikisi de "europe"),
# Riot'un sınırı küme başına ayrı olduğu için her küme kendi iş parçacığında toplanır.
CLUSTERS = {"europe": ("TR1", "EUW1"), "asia": ("KR",)}
REGION_NAMES = {"TR1": "TR", "EUW1": "EUW", "KR": "KR"}
START_WINDOW = 60_000  # ilk 60 sn'de alınanlar başlangıç itemi sayılır (ms)
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
SCOUTING = threading.Event()  # oyuncu bilgisi yüklenirken toplayıcı bekler
MIN_GAMES = 8        # bu kadar maçtan azsa öneri "az veri" uyarısıyla gösterilir
MIN_MATCHUP = 5      # eşleşme önerisi için eşik (eşleşme verisi çok daha seyrek)
_lock = threading.Lock()


def _cluster_of(platform: str) -> str:
    return REGIONAL[platform.upper()]


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS matches (id TEXT PRIMARY KEY, patch TEXT, queue INT, created REAL);
        CREATE TABLE IF NOT EXISTS parts (match_id TEXT, puuid TEXT, champion INT, role TEXT, win INT,
            runes TEXT, items TEXT, opponent INT);
        CREATE INDEX IF NOT EXISTS parts_champ ON parts (champion, role);
        CREATE INDEX IF NOT EXISTS parts_puuid ON parts (puuid);
    """)
    # source: 'apex' = toplayıcının Master+ maçları (öneriler bunlardan), 'scout' = oyuncu bilgisi için çekilenler
    if "source" not in [r[1] for r in c.execute("PRAGMA table_info(matches)")]:
        c.execute("ALTER TABLE matches ADD COLUMN source TEXT")
    # timeline'dan: başlangıç itemleri ve satın alma sırası (timeline'ı olmayan eski maçlarda boş)
    cols = [r[1] for r in c.execute("PRAGMA table_info(parts)")]
    # mdmg/pdmg: şampiyonlara verilen büyü / fiziksel hasar (AP-AD profili için; eski maçlarda boş)
    for col, kind in (("start", "TEXT"), ("build", "TEXT"), ("bound", "INT"), ("mdmg", "INT"), ("pdmg", "INT")):
        if col not in cols:
            c.execute(f"ALTER TABLE parts ADD COLUMN {col} {kind}")
    return c


def patch_of(game_version: str) -> str:
    return ".".join(game_version.split(".")[:2])


def _runes(p: dict) -> dict | None:
    perks = p.get("perks") or {}
    styles = perks.get("styles") or []
    if len(styles) < 2:
        return None
    sp = perks.get("statPerks") or {}
    return {"primary": styles[0]["style"], "sub": styles[1]["style"],
            "perks": [s["perk"] for s in styles[0]["selections"]] + [s["perk"] for s in styles[1]["selections"]],
            "shards": [sp.get("offense"), sp.get("flex"), sp.get("defense")]}


def purchases(timeline: dict) -> dict[int, tuple[list[int], list[int]]]:
    """participantId -> (başlangıç itemleri, sırayla alınan bütün itemler). Geri alınan
    (undo) alışverişler çıkarılır."""
    events = sorted((e for f in timeline["info"]["frames"] for e in f["events"]
                     if e["type"] in ("ITEM_PURCHASED", "ITEM_UNDO")), key=lambda e: e["timestamp"])
    bought: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for e in events:
        pid = e.get("participantId")
        if e["type"] == "ITEM_PURCHASED":
            bought[pid].append((e["timestamp"], e["itemId"]))
        elif e.get("beforeId"):  # alışverişi geri al: o itemin son alımını sil
            seq = bought[pid]
            for i in range(len(seq) - 1, -1, -1):
                if seq[i][1] == e["beforeId"]:
                    del seq[i]
                    break
    return {pid: ([it for t, it in seq if t <= START_WINDOW], [it for _, it in seq]) for pid, seq in bought.items()}


def store(info: dict, source: str = "apex", timeline: dict | None = None) -> None:
    """Match-V5 maç detayını (ve varsa timeline'ını) veritabanına ekler (zaten varsa dokunmaz)."""
    i = info["info"]
    if i.get("gameDuration", 0) < 600:  # remake / erken teslim
        return
    mid = info["metadata"]["matchId"]
    parts = i["participants"]
    by_role = {(p["teamId"], p.get("teamPosition")): p["championId"] for p in parts}
    buys = purchases(timeline) if timeline else {}
    rows = []
    for p in parts:
        role = p.get("teamPosition") or ""
        start, build = buys.get(p["participantId"], (None, None))
        rows.append((mid, p["puuid"], p["championId"], role, int(p["win"]), json.dumps(_runes(p)),
                     json.dumps([p.get(f"item{k}", 0) for k in range(6) if p.get(f"item{k}")]),
                     by_role.get((300 - p["teamId"], role)),
                     json.dumps(start) if start is not None else None, json.dumps(build) if build is not None else None,
                     # rol görevi yuvası: ADC'nin botu 6 item yuvasında değil burada durur
                     p.get("roleBoundItem") or 0,
                     p.get("magicDamageDealtToChampions"), p.get("physicalDamageDealtToChampions")))
    with _lock, _db() as c:
        if c.execute("SELECT 1 FROM matches WHERE id=?", (mid,)).fetchone():
            return
        c.execute("INSERT INTO matches (id, patch, queue, created, source) VALUES (?,?,?,?,?)",
                  (mid, patch_of(i.get("gameVersion", "")), i.get("queueId"), i.get("gameCreation", 0) / 1000, source))
        c.executemany("INSERT INTO parts (match_id, puuid, champion, role, win, runes, items, opponent, start, build, bound, "
                      "mdmg, pdmg) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def known(match_ids: list[str]) -> set[str]:
    if not match_ids:
        return set()
    with _db() as c:
        q = ",".join("?" * len(match_ids))
        return {r[0] for r in c.execute(f"SELECT id FROM matches WHERE id IN ({q})", match_ids)}


def summary() -> dict:
    with _db() as c:
        rows = c.execute("SELECT patch, COUNT(*) FROM matches WHERE queue=420 AND source='apex' GROUP BY patch").fetchall()
        regions = c.execute("SELECT substr(id, 1, instr(id, '_') - 1) r, COUNT(*) FROM matches "
                            "WHERE queue=420 AND source='apex' GROUP BY r").fetchall()
    if not rows:
        return {"matches": 0, "patch": None, "regions": {}}
    patch = max(rows, key=lambda r: [int(x) for x in r[0].split(".") if x.isdigit()])
    return {"matches": sum(n for _, n in rows), "patch": patch[0], "patch_matches": patch[1],
            "regions": {REGION_NAMES.get(r, r): n for r, n in regions}}


def patch_start() -> int | None:
    """Son yamanın (yaklaşık) başlangıcı: o yamada toplanan en eski maçın zamanı (saniye)."""
    cur = summary().get("patch")
    if not cur:
        return None
    with _db() as c:
        t = c.execute("SELECT MIN(created) FROM matches WHERE patch=? AND source='apex'", (cur,)).fetchone()[0]
    return int(t) if t else None


def player_champion(puuid: str, champion: int, match_ids: list[str]) -> dict:
    """Oyuncunun verilen maçlarından (veritabanına inmiş olanlar) o şampiyonla kazanma durumu,
    kuyruk başına: {"solo": {games, wins, of}, "flex": {...}}. `of` = incelenen maç sayısı."""
    out = {q: {"games": 0, "wins": 0, "of": 0} for q in ("solo", "flex")}
    if not match_ids:
        return out
    q = ",".join("?" * len(match_ids))
    with _db() as c:
        rows = c.execute(f"SELECT p.champion, p.win, m.queue FROM parts p JOIN matches m ON m.id=p.match_id "
                         f"WHERE p.puuid=? AND p.match_id IN ({q})", (puuid, *match_ids)).fetchall()
    for ch, win, queue in rows:
        if queue not in (420, 440):
            continue
        o = out["solo" if queue == 420 else "flex"]
        o["of"] += 1
        if ch == champion:
            o["games"] += 1
            o["wins"] += win
    return out


def main_role(champion: int) -> str:
    with _db() as c:
        r = c.execute("SELECT role, COUNT(*) n FROM parts p JOIN matches m ON m.id=p.match_id "
                      "WHERE m.source='apex' AND champion=? AND role!='' GROUP BY role "
                      "ORDER BY n DESC LIMIT 1", (champion,)).fetchone()
    return r[0] if r else ""


def assign_roles(champions: list[int]) -> dict[int, str]:
    """Rakip takımın şampiyonlarını rollere en olası şekilde dağıtır (her rol bir kişi).
    Tek tek "en çok oynandığı rol"e bakmak yanılır: Yone en çok Top'ta oynanır ama
    takımda başka bir Top varsa Mid'dir. Veride hiç görülmeyen eşleşmelere küçük pay verilir."""
    from itertools import permutations
    champions = champions[:5]
    if not champions:
        return {}
    q = ",".join("?" * len(champions))
    with _db() as c:
        rows = c.execute(f"SELECT p.champion, p.role, COUNT(*) FROM parts p JOIN matches m ON m.id=p.match_id "
                         f"WHERE m.source='apex' AND p.role!='' AND p.champion IN ({q}) GROUP BY 1, 2",
                         champions).fetchall()
    counts = defaultdict(lambda: defaultdict(int))
    for ch, role, n in rows:
        counts[ch][role] = n
    def prob(ch: int, role: str) -> float:
        total = sum(counts[ch].values())
        return (counts[ch][role] + 0.5) / (total + 0.5 * len(ROLES))
    best = max(permutations(ROLES, len(champions)),
               key=lambda roles: math.prod(prob(ch, r) for ch, r in zip(champions, roles)))
    return dict(zip(champions, best))


# ---------- rakip takımın hasar profili (AP / AD) ----------
# Riot'un şampiyon puanları (info.attack / info.magic) birkaç şampiyonda yanıltıyor
FALLBACK_AP = {4: 0.85, 42: 0.75}  # Twisted Fate, Corki: hasarlarının çoğu büyü
DAMAGE_MIN = 30   # şampiyonun gerçek hasar oranının kullanılması için gereken maç
DAMAGE_GROUPS = (("ad", "AD-heavy"), ("mixed", "Mixed"), ("ap", "AP-heavy"))
_profile_cache: dict = {}


def damage_profiles() -> dict[int, tuple[float, float]]:
    """championId -> (büyü hasarı oranı 0..1, ağırlık). Yeterli maçta kaydedilmiş gerçek
    hasardan; yoksa Riot'un şampiyon puanlarından. Ağırlık, takım içinde ne kadar hasar
    verdiği: tanklar ve destekler takım profilini daha az belirler."""
    with _db() as c:
        n = c.execute("SELECT COUNT(*) FROM parts WHERE mdmg IS NOT NULL").fetchone()[0]
        if _profile_cache.get("n") == n:
            return _profile_cache["p"]
        measured = c.execute("SELECT champion, COUNT(*), AVG(mdmg), AVG(pdmg) FROM parts "
                             "WHERE mdmg IS NOT NULL GROUP BY champion").fetchall()
    raw = {int(v["key"]): v for v in ddragon._load("champion")["data"].values()}
    out = {}
    for cid, ch in raw.items():
        i, tags = ch["info"], ch["tags"]
        t = i["attack"] + i["magic"]
        share = FALLBACK_AP.get(cid, i["magic"] / t if t else 0.5)
        out[cid] = (share, 0.5 if tags and tags[0] in ("Tank", "Support") else 1.0)
    total = [m + p for _, k, m, p in measured if k >= DAMAGE_MIN]
    avg_total = sum(total) / len(total) if total else 0
    for cid, k, m, p in measured:
        if k >= DAMAGE_MIN and m + p > 0:
            out[cid] = (m / (m + p), min(1.5, (m + p) / avg_total) if avg_total else 1.0)
    _profile_cache.update(n=n, p=out)
    return out


def team_ap(champions: list[int], profiles: dict | None = None) -> float | None:
    """Takımın (hasar ağırlıklı) büyü hasarı oranı."""
    profiles = profiles or damage_profiles()
    known_ = [profiles[c] for c in champions if c in profiles]
    w = sum(wt for _, wt in known_)
    return sum(s * wt for s, wt in known_) / w if w else None


def _thresholds(profiles: dict) -> tuple[float, float]:
    """AD-heavy / AP-heavy sınırları: toplanan maçlardaki takımların alt ve üst %25'i.
    Sabit sınır yerine dağılımdan alınır ki gruplar dengeli kalsın."""
    # toplayıcı sürekli maç eklediği için her seferinde değil, 10 dakikada bir hesaplanır
    key = ("thr", int(time.time() // 600))
    if key not in _profile_cache:
        teams = defaultdict(list)
        with _db() as c:
            for mid, ch, win in c.execute("SELECT p.match_id, p.champion, p.win FROM parts p JOIN matches m "
                                          "ON m.id=p.match_id WHERE m.source='apex' AND m.queue=420"):
                teams[(mid, win)].append(ch)
        vals = sorted(v for t in teams.values() if len(t) == 5 and (v := team_ap(t, profiles)) is not None)
        _profile_cache[key] = (vals[len(vals) // 4], vals[3 * len(vals) // 4]) if len(vals) > 100 else (0.38, 0.54)
    return _profile_cache[key]


def damage_group(ap: float | None, thr: tuple[float, float]) -> str | None:
    if ap is None:
        return None
    return "ad" if ap <= thr[0] else "ap" if ap >= thr[1] else "mixed"


def _teams(match_ids) -> dict[tuple[str, int], list[int]]:
    """(match_id, kazanma durumu) -> o takımın şampiyonları. Her maçta 5 kişi kazanır,
    5 kişi kaybeder; kazanma durumu farklı olan 5 kişi rakip takımdır."""
    ids = list(match_ids)
    teams = defaultdict(list)
    with _db() as c:
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            q = ",".join("?" * len(chunk))
            for mid, ch, win in c.execute(f"SELECT match_id, champion, win FROM parts WHERE match_id IN ({q})", chunk):
                teams[(mid, win)].append(ch)
    return teams


def _by_enemy_damage(rows: list[tuple], enemies_now: list[int] | None) -> dict:
    """Satırları (match_id, win, ...) rakip takımın hasar profiline göre gruplar ve her grup
    için item / bot tercihlerini, genel orana göre farkıyla döner."""
    profiles = damage_profiles()
    thr = _thresholds(profiles)
    enemy = _teams({r[0] for r in rows})
    groups = defaultdict(list)
    for r in rows:
        g = damage_group(team_ap(enemy[(r[0], 1 - r[1])], profiles), thr)
        if g:
            groups[g].append(r[1:])  # match_id'siz, _aggregate'in beklediği biçim

    def picks(data):
        n = len(data)
        cnt = Counter()
        for win, _, items, _, _, bound, _ in data:
            for it in set(json.loads(items)) | ({bound} if bound else set()):
                if ddragon.item_kind(it):
                    cnt[it] += 1
        return {it: 100 * k / n for it, k in cnt.items()} if n else {}

    overall = picks([r for g in groups.values() for r in g])
    out = []
    for key, label in DAMAGE_GROUPS:
        data = groups.get(key, [])
        agg = _aggregate(data, MIN_GAMES)
        p = picks(data)
        # bu grupta genele göre en çok daha sık alınan itemler: rakibe göre alınan item burada çıkar
        shifted = sorted(((it, p[it] - overall.get(it, 0)) for it in p if p[it] >= 8), key=lambda kv: kv[1], reverse=True)
        for lst in (agg["core"], agg["boots"], agg["situational"]):
            for it in lst:
                it["delta"] = round(p.get(it["id"], 0) - overall.get(it["id"], 0), 1)
        out.append({"key": key, "label": label, "games": agg["games"], "wr": agg["wr"], "enough": agg["enough"],
                    "core": agg["core"], "boots": agg["boots"], "situational": agg["situational"],
                    "more": [{"id": it, "delta": round(d, 1), "pick": round(p[it], 1)} for it, d in shifted[:4] if d >= 3]})
    now = damage_group(team_ap(enemies_now, profiles), thr) if enemies_now else None
    return {"groups": out, "current": now, "partial": bool(enemies_now) and len(enemies_now) < 5}


def _span(values: list[float], extra: float | None) -> list[float]:
    lo, hi = (values[len(values) // 50], values[-len(values) // 50 - 1]) if len(values) > 50 else (0.25, 0.65)
    if extra is not None:
        lo, hi = min(lo, extra), max(hi, extra)
    return [round(lo - 0.02, 2), round(hi + 0.02, 2)]


def item_table(champion: int, role: str = "", opponent: int | None = None,
               ap_min: float = 0.0, ap_max: float = 1.0, enemies_now: list[int] | None = None) -> dict:
    """Seçilen şampiyonun maçlarını süzer: (isteğe bağlı) yalnızca `opponent` koridor rakibine
    karşı olanlar ve rakip takımın büyü hasarı oranı [ap_min, ap_max] aralığında olanlar.
    Her item için alınma ve kazanma oranı, ayrıca şampiyonun bütün maçlarına göre farkı."""
    role = role or main_role(champion)
    cur = summary().get("patch")
    sql = ("SELECT p.match_id, p.win, p.items, p.bound, p.role, p.opponent FROM parts p JOIN matches m "
           "ON m.id=p.match_id WHERE m.queue=420 AND m.source='apex' AND p.champion=?")
    args: list = [champion]
    if role:
        sql += " AND p.role=?"; args.append(role)
    profiles = damage_profiles()

    def load(patch_only: bool):
        with _db() as c:
            rows = c.execute(sql + " AND m.patch=?", (*args, cur)).fetchall() if patch_only else c.execute(sql, args).fetchall()
        teams = _teams({r[0] for r in rows})
        ap_of = {r[0]: team_ap(teams[(r[0], 1 - r[1])], profiles) for r in rows}
        sel = [r for r in rows if (not opponent or r[5] == opponent)
               and ap_of[r[0]] is not None and ap_min <= ap_of[r[0]] <= ap_max]
        return rows, ap_of, sel

    # süzgeçler veriyi çok inceltiyor: son yamada süzgeçten 30 maçtan azı geçerse bütün yamalar
    rows, ap_of, sel = load(True) if cur else ([], {}, [])
    scope = cur
    if len(sel) < 30:
        rows, ap_of, sel = load(False)
        scope = "all patches"

    def stats_of(data):
        n = len(data)
        cnt, wins = Counter(), Counter()
        boot_games = 0
        for mid, win, items, bound, row_role, _ in data:
            known_boots = row_role != "BOTTOM" or bound is not None
            boot_games += known_boots
            for it in set(json.loads(items)) | ({bound} if bound else set()):
                kind = ddragon.item_kind(it)
                if kind and (kind != "boots" or known_boots):
                    cnt[it] += 1
                    wins[it] += win
        return n, boot_games, cnt, wins

    n_all, boots_all, cnt_all, _ = stats_of(rows)
    n, boot_games, cnt, wins = stats_of(sel)
    pct = lambda k, total: round(100 * k / total, 1) if total else None  # noqa: E731

    def entry(it):
        boots = ddragon.item_kind(it) == "boots"
        pick = pct(cnt[it], boot_games if boots else n)
        base = pct(cnt_all[it], boots_all if boots else n_all) or 0
        return {"id": it, "games": cnt[it], "pick": pick, "wr": pct(wins[it], cnt[it]),
                "delta": round(pick - base, 1), "boots": boots}

    items = [entry(it) for it in cnt]
    all_ap = sorted(a for a in ap_of.values() if a is not None)
    ap_now = team_ap(enemies_now, profiles) if enemies_now else None
    return {
        "champion": champion, "role": role, "opponent": opponent, "scope": scope,
        "games": n, "all_games": n_all, "wr": pct(sum(r[1] for r in sel), n),
        "boots": sorted((i for i in items if i["boots"]), key=lambda i: -i["games"])[:3],
        "items": sorted((i for i in items if not i["boots"]), key=lambda i: -i["games"])[:12],
        # genele göre en çok yükselenler (en az %10 alınanlar arasında)
        "more": sorted((i for i in items if (i["pick"] or 0) >= 10 and i["games"] >= 3 and i["delta"] >= 3),
                       key=lambda i: -i["delta"])[:5],
        "enough": n >= 20,
        "range": [ap_min, ap_max],
        "thresholds": list(_thresholds(profiles)),
        # kaydırıcının anlamlı bir aralık göstermesi için: bu şampiyonun maçlarındaki rakip takım AP oranları
        # (uçtaki %2 hariç) ve şu anki rakip takım
        "ap_span": _span(all_ap, ap_now),
        "ap_now": ap_now,
        "partial": bool(enemies_now) and len(enemies_now) < 5,
    }


def suggest(champion: int, role: str = "", opponent: int | None = None,
            enemies: list[int] | None = None) -> dict:
    """Şampiyon (+ rol) için en çok kullanılan rün sayfaları ve item yolu. Son yamada
    yeterli veri yoksa tüm yamalara bakılır. `opponent` verilirse ayrıca yalnızca o rakibe
    karşı (aynı koridorda) oynanan maçlardan bir eşleşme önerisi döner (`matchup`)."""
    role = role or main_role(champion)
    cur = summary().get("patch")
    with _db() as c:
        def rows(patch_only: bool, vs: int | None = None, with_id: bool = False):
            sql = ("SELECT " + ("p.match_id, " if with_id else "") +
                   "p.win, p.runes, p.items, p.start, p.build, p.bound, p.role FROM parts p JOIN matches m ON m.id=p.match_id "
                   "WHERE m.queue=420 AND m.source='apex' AND p.champion=?")
            args: list = [champion]
            if role:
                sql += " AND p.role=?"; args.append(role)
            if patch_only and cur:
                sql += " AND m.patch=?"; args.append(cur)
            if vs:
                sql += " AND p.opponent=?"; args.append(vs)
            return c.execute(sql, args).fetchall()
        data = rows(True)
        scope = cur
        if len(data) < MIN_GAMES:
            data, scope = rows(False), "all patches"
        # eşleşme de önce son yamadan; yetmezse tüm yamalar
        vs, vs_scope = [], cur
        if opponent:
            vs = rows(True, opponent)
            if len(vs) < MIN_MATCHUP:
                vs, vs_scope = rows(False, opponent), "all patches"
        # rakip takımın AP/AD'sine göre: maçlar üç gruba bölündüğü için daha çok veri ister
        dmg_scope = cur if len(data) >= 3 * 4 * MIN_GAMES and scope == cur else "all patches"
        dmg_rows = rows(dmg_scope == cur, with_id=True)

    out = {"champion": champion, "role": role, "scope": scope, **_aggregate(data, MIN_GAMES)}
    out["matchup"] = {"opponent": opponent, "scope": vs_scope, **_aggregate(vs, MIN_MATCHUP)} if opponent else None
    out["vs_damage"] = {"scope": dmg_scope, **_by_enemy_damage(dmg_rows, enemies)}
    return out


COUNTER_MIN = 8      # bir şampiyonun karşı pick listesine girmesi için gereken eşleşme maçı
COUNTER_PRIOR = 20   # kazanma oranı az maçta %50'ye bu kadar "sanal maç" ile çekilir


def counters(opponent: int, role: str, limit: int = 6) -> dict:
    """`opponent`a karşı aynı koridorda (`role`) oynanan şampiyonların kazanma oranları.
    En iyi ve en kötü pick'ler. Az maçlı eşleşmeler yanıltmasın diye oran %50'ye doğru
    çekilerek sıralanır (8 maçta 7 galibiyet, 300 maçta %56'dan üstte görünmesin); ekranda
    gerçek oran ve maç sayısı gösterilir. Son yamada yeterli veri yoksa tüm yamalara bakılır."""
    cur = summary().get("patch")
    sql = ("SELECT p.champion, COUNT(*), SUM(p.win) FROM parts p JOIN matches m ON m.id=p.match_id "
           "WHERE m.queue=420 AND m.source='apex' AND p.opponent=? AND p.role=?")
    with _db() as c:
        rows, scope = [], cur
        if cur:
            rows = c.execute(sql + " AND m.patch=? GROUP BY 1", (opponent, role, cur)).fetchall()
        if sum(n >= COUNTER_MIN for _, n, _ in rows) < 2 * limit:
            rows, scope = c.execute(sql + " GROUP BY 1", (opponent, role)).fetchall(), "all patches"
    ranked = []
    for champ, n, wins in rows:
        if n >= COUNTER_MIN:
            score = (wins + COUNTER_PRIOR * 0.5) / (n + COUNTER_PRIOR)
            ranked.append({"id": champ, "games": n, "wr": round(100 * wins / n, 1), "score": score})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    best = ranked[:limit]
    worst = [r for r in ranked[::-1][:limit] if r not in best]
    return {"opponent": opponent, "role": role, "scope": scope, "games": sum(n for _, n, _ in rows),
            "champions": len(ranked), "best": best, "worst": worst}


def _aggregate(data: list[tuple], enough: int) -> dict:
    """Maç satırlarından rün sayfaları, başlangıç itemleri, item sırası ve itemler."""
    games = len(data)
    wr = lambda w, n: round(100 * w / n, 1) if n else None  # noqa: E731

    pages = defaultdict(lambda: [0, 0])
    for win, r, *_ in data:
        if r and r != "null":
            pages[r][0] += 1
            pages[r][1] += win
    top_pages = sorted(pages.items(), key=lambda kv: kv[1][0], reverse=True)[:3]

    legend, boots = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    boot_games = 0
    for win, _, items, _, _, bound, row_role in data:
        # rol yuvası kaydedilmeden toplanan ADC maçlarında bot bilinmiyor; bot hesabına girmez
        known_boots = row_role != "BOTTOM" or bound is not None
        boot_games += known_boots
        for it in set(json.loads(items)) | ({bound} if bound else set()):
            kind = ddragon.item_kind(it)
            if kind:
                if kind == "boots" and not known_boots:
                    continue
                bucket = legend if kind == "legendary" else boots
                bucket[it][0] += 1
                bucket[it][1] += win
    rank = lambda d: sorted(d.items(), key=lambda kv: kv[1][0], reverse=True)  # noqa: E731
    item = lambda kv: {"id": kv[0], "games": kv[1][0], "pick": wr(kv[1][0], games), "wr": wr(kv[1][1], kv[1][0])}  # noqa: E731

    # timeline'ı olan maçlardan: başlangıç seti ve ilk 3 bitmiş itemin alınma sırası
    timed = [(win, json.loads(st), json.loads(bd)) for win, _, _, st, bd, *_ in data if st is not None and bd]
    starts, orders = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for win, st, bd in timed:
        st = [i for i in st if not ddragon.is_trinket(i)]  # totem herkese bedava, seti bölmesin
        if st:
            k = tuple(sorted(st))
            starts[k][0] += 1
            starts[k][1] += win
        seen: list[int] = []
        for it in bd:
            if ddragon.item_kind(it) == "legendary" and it not in seen:
                seen.append(it)
        if len(seen) >= 3:
            orders[tuple(seen[:3])][0] += 1
            orders[tuple(seen[:3])][1] += win
    n_starts = sum(v[0] for v in starts.values())
    n_orders = sum(v[0] for v in orders.values())
    group = lambda kv, n: {"items": [{"id": i, "count": kv[0].count(i)} for i in dict.fromkeys(kv[0])],  # noqa: E731
                           "games": kv[1][0], "pick": wr(kv[1][0], n), "wr": wr(kv[1][1], kv[1][0])}

    return {
        "games": games, "wr": wr(sum(d[0] for d in data), games), "enough": games >= enough,
        "runes": [{**json.loads(k), "games": v[0], "pick": wr(v[0], games), "wr": wr(v[1], v[0])}
                  for k, v in top_pages],
        "timed": len(timed),
        "start": [group(kv, n_starts) for kv in rank(starts)[:2]],
        "order": [group(kv, n_orders) for kv in rank(orders)[:3]],
        "core": [item(kv) for kv in rank(legend)[:3]],
        "boots": [{**item(kv), "pick": wr(kv[1][0], boot_games)} for kv in rank(boots)[:2]],
        "situational": [item(kv) for kv in rank(legend)[3:9]],
    }


class Collector:
    """Arka planda Master+ maçlarını (timeline'larıyla) toplar: her bölgesel küme (europe:
    TR+EUW, asia: KR) kendi iş parçacığında, kendi istek sınırıyla. Anahtar geçersizse bekler,
    uygulama kapanınca durur. Oyuncu bilgisi yüklenirken aynı kümedeki toplayıcı bekler."""

    def __init__(self, cfg: dict, log):
        self.cfg, self.log = cfg, log
        self.states = {c: "idle" for c in CLUSTERS}
        self.added = 0
        self._stop = threading.Event()
        self._cond = threading.Condition()

    @property
    def state(self) -> str:
        st = list(self.states.values())
        if "collecting" in st:
            return "collecting"
        return next((x for x in st if x != "idle"), "idle")

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def update(self, cfg: dict) -> None:
        """Ayarlar (ör. yeni API anahtarı) kaydedilince beklemeden devam et."""
        self.cfg = cfg
        with self._cond:
            self._cond.notify_all()

    def _wait(self, seconds: float) -> None:
        with self._cond:
            self._cond.wait(seconds)

    def run(self) -> None:
        workers = [threading.Thread(target=self._work, args=(c,), daemon=True) for c in CLUSTERS]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

    def _work(self, cluster: str) -> None:
        platforms = CLUSTERS[cluster]
        players: list[tuple[str, str]] = []  # (platform, puuid)
        refreshed = 0.0
        while not self._stop.is_set():
            try:
                key = self.cfg["riot"]["api_key"]
                home = _cluster_of(self.cfg["riot"]["platform"])
                if time.time() - refreshed > 3600 or not players:
                    players = [(pf, pu) for pf in platforms for pu in RiotApi(key, pf, reserve=35).apex_puuids()]
                    random.shuffle(players)
                    refreshed = time.time()
                    if not players:
                        self.states[cluster] = "no players"
                        self._wait(600)
                        continue
                if SCOUTING.is_set() and cluster == home:
                    self.states[cluster] = "paused for player info"
                    self._wait(5)
                    continue
                self.states[cluster] = "collecting"
                platform, puuid = players.pop()
                api = RiotApi(key, platform, reserve=35)  # 35 isteği ön plana bırak
                # yalnızca son yamanın maçları (eski yama önerileri bozmasın, istek boşa gitmesin)
                ids = api.match_ids(puuid, count=10, since=patch_start())
                for mid in [m for m in ids if m not in known(ids)]:
                    if self._stop.is_set():
                        return
                    if SCOUTING.is_set() and cluster == home:
                        break
                    if (info := api.match(mid)):
                        store(info, "apex", api.timeline(mid))
                        self.added += 1
                        if self.added % 200 == 0:
                            sm = summary()
                            self.log(f"Stats: {sm['matches']} high-elo matches collected "
                                     f"({', '.join(f'{k} {v}' for k, v in sm['regions'].items())})")
            except KeyExpired:
                self.states[cluster] = "key expired"
                self._wait(600)
            except Exception as e:  # ağ hatası vb.; toplayıcı ölmesin
                self.states[cluster] = f"error: {e}"
                self._wait(60)
