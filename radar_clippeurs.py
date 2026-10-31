"""Radar des clippeurs : TikTok des clippeurs de clippeurs.json qui « percent » -> salon Discord radar
(DISCORD_WEBHOOK_RADAR). Tourne sur GitHub, au plus 1 passage toutes les 30 min. Seuls les posts qui percent
sont envoyés. Journal public des posts repérés : radar_clippeurs_alertes.json (lu par le Mac pour télécharger).

Règles : moins de 10 h après publication > 100 k vues ou 20 k likes, OU 3 fois plus de vues que d'habitude
pour ce clippeur au même âge (habitude = médiane des posts de 2 à 30 jours, recalculée 1 fois par jour)."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from radar import _age, _heures_depuis, _maintenant, _n, _texte, habitude, juger, posts_youtube
from veille import _env, date_fr, envoyer_discord

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "clippeurs.json"
ETAT = ROOT / "radar_clippeurs_etat.json"
JOURNAL = ROOT / "radar_clippeurs_alertes.json"
SEUILS = {"absolu": {"heures_max": 10, "vues": 100000, "likes": 20000},
          "relatif": {"facteur": 3, "vues_min": 20000, "heures_max": 48}}
INTERVALLE_S = 25 * 60           # GitHub lance le workflow toutes les 5 min : le radar clippeurs saute 5 fois sur 6
DUREE_MAX_S = 150
POSTS_MAX, BASE_MAX_H, MAJ_VUES_S = 60, 30 * 24, 3600
_PSEUDO = re.compile(r"[\w. ]{2,40}", re.ASCII)
_UC = re.compile(r"UC[\w-]{22}", re.ASCII)
_SEC = re.compile(r"MS4wLjABAAAA[\w-]{20,90}", re.ASCII)
_ID = re.compile(r"\d{8,25}", re.ASCII)
COMMANDE = 'cd ~/Documents/"ark obs stream"/"smart clipper" && bash radar_clippeurs.sh --telecharger'
try:
    from zoneinfo import ZoneInfo
    PARIS = ZoneInfo("Europe/Paris")
except Exception:
    PARIS = None


def _paris() -> datetime:
    return datetime.now(PARIS) if PARIS else datetime.now()


def posts(pseudo: str, sec: str) -> list:
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp absent")
    cibles = ([f"tiktokuser:{sec}"] if sec else []) + [f"https://www.tiktok.com/@{pseudo}"]
    err = ""
    for cible in cibles:
        r = subprocess.run([exe, "--flat-playlist", "-J", "--playlist-items", "1:15", "--no-warnings",
                            "--socket-timeout", "10", cible], capture_output=True, text=True, timeout=45)
        if r.returncode == 0:
            break
        err = (r.stderr.strip().splitlines() or [""])[-1][:160]
    else:
        raise RuntimeError(err)
    out = []
    for v in (json.loads(r.stdout).get("entries") or []):
        vid, ts = str(v.get("id") or ""), v.get("timestamp")
        if not _ID.fullmatch(vid) or not ts:
            continue
        out.append({"id": vid, "plat": "tiktok", "type": "short", "url": f"https://www.tiktok.com/@{pseudo}/video/{vid}",
                    "titre": "", "texte": _texte(v.get("description") or v.get("title") or "", 200),
                    "publie": float(ts), "vues": int(v.get("view_count") or 0), "likes": int(v.get("like_count") or 0)})
    return out


def embed(pseudo: str, p: dict, raisons: list) -> dict:
    plat = "YouTube Short" if p.get("plat") == "youtube" else "TikTok"
    desc = "\n".join(f"{'📈' if k == 'relatif' else '🚀'} {t}" for k, t in raisons)
    if p["texte"]:
        desc += f"\n\n> {p['texte']}"
    if p.get("titre"):
        desc = f"**{p['titre']}**\n" + desc
    desc += f"\n\nTélécharger tous les clips qui ont percé aujourd'hui :\n```{COMMANDE}```"
    return {"title": f"🔥 {'@' if plat == 'TikTok' else ''}{pseudo} · {plat}", "url": p["url"], "color": 0xFF0000 if p.get("plat") == "youtube" else 0x25F4EE, "description": desc[:3500],
            "footer": {"text": f"{_n(p['vues'] / max(_heures_depuis(p['publie']), 0.25))} vues / heure"}}


def main():
    test = "--test" in sys.argv
    webhook = _env().get("DISCORD_WEBHOOK_RADAR", "")
    if not webhook:
        sys.exit("ÉCHEC : ajoute le secret DISCORD_WEBHOOK_RADAR (salon radar)")
    try:
        etat = json.loads(ETAT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        etat = {}
    if not test and time.time() - etat.get("dernier", 0) < INTERVALLE_S:
        print("radar clippeurs : passage récent, sauté")
        return
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    comptes = etat.setdefault("comptes", {})
    jour = _paris().strftime("%Y-%m-%d")
    alertes, lignes, erreurs, debut = [], [], 0, time.time()
    liste = [c for c in cfg.get("clippeurs", []) if _PSEUDO.fullmatch(str(c.get("pseudo") or "").lstrip("@"))]
    # on reprend où on s'était arrêté si le passage précédent a manqué de temps
    k0 = etat.get("suivant", 0) % max(1, len(liste))
    etat["suivant"] = 0
    coupe = False
    for c in liste[k0:] + liste[:k0]:
        ps = str(c["pseudo"]).lstrip("@")
        if time.time() - debut > DUREE_MAX_S:
            if not coupe:
                etat["suivant"], coupe = liste.index(c), True
            lignes.append(f"⏳ @{ps} : au passage suivant")
            continue
        sec = str(c.get("secUid") or "")
        yt = str(c.get("youtube") or "")
        try:
            if _UC.fullmatch(yt):                      # clippeur YouTube : ses Shorts via le flux RSS public
                lus = posts_youtube(yt, shorts_seulement=True)
            elif yt:
                raise RuntimeError("identifiant YouTube invalide dans clippeurs.json")
            else:
                lus = posts(ps, sec if _SEC.fullmatch(sec) else "")
        except Exception as e:
            erreurs += 1
            print(f"@{ps} : illisible ({str(e)[:160]})")
            lignes.append(f"⚠️ @{ps} : illisible pour l'instant")
            continue
        mem = comptes.setdefault(ps, {"posts": {}, "habitude": {}, "habitude_jour": ""})
        premiere, now = not mem["posts"], _maintenant().timestamp()
        for p in lus:
            ancien = mem["posts"].get(p["id"])
            if ancien is None:          # 1er passage sur ce compte : posts existants marqués (pas d'avalanche)
                mem["posts"][p["id"]] = {"publie": p["publie"], "type": "short", "vues": p["vues"], "likes": p["likes"],
                                         "vu": now, "alertes": ["absolu", "relatif"] if premiere else []}
            elif now - ancien.get("vu", 0) >= MAJ_VUES_S:
                ancien.update(vues=p["vues"], likes=p["likes"], vu=now)
        if mem["habitude_jour"] != jour:
            mem["habitude"], mem["habitude_jour"] = habitude(mem["posts"], "short"), jour
        for p in lus:
            m = mem["posts"][p["id"]]
            raisons = [r for r in juger(p, mem["habitude"] or {}, SEUILS) if r[0] not in m["alertes"]]
            if raisons:
                alertes.append((ps, p, raisons, m))
        garder = sorted(mem["posts"].items(), key=lambda kv: -kv[1]["publie"])[:POSTS_MAX]
        mem["posts"] = {k: v for k, v in garder if _heures_depuis(v["publie"]) <= BASE_MAX_H + 24}
        h = mem["habitude"]
        lignes.append(f"@{ps} : {len(lus)} posts · habitude {_n(h['vues']) + ' vues' if h else 'en cours'}")
        time.sleep(1)

    if test:
        envoyer_discord(webhook, [{"title": "Clippeurs suivis", "description": "\n".join(lignes)[:3900],
                                   "color": 0x2B2D31}],
                        f"{date_fr()}\n🧪 Test du radar des clippeurs" + (f" ({erreurs} illisible(s))" if erreurs else ""))
    if alertes:
        envoyer_discord(webhook, [embed(ps, p, r) for ps, p, r, _ in alertes],
                        f"{date_fr()}\n🔥 Clips de clippeurs qui percent :", ping=True)
        try:
            journal = json.loads(JOURNAL.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            journal = []
        for ps, p, r, m in alertes:     # marqués « alertés » seulement après un envoi réussi
            m["alertes"] = m["alertes"] + [k for k, _ in r]
            journal.append({"jour": jour, "heure": _paris().hour, "compte": ps, "url": p["url"], "texte": p["texte"],
                            "vues": p["vues"], "likes": p["likes"], "raisons": [t for _, t in r]})
        JOURNAL.write_text(json.dumps(journal[-500:], ensure_ascii=False, indent=1), encoding="utf-8")
    etat["dernier"] = time.time()
    ETAT.write_text(json.dumps(etat, separators=(",", ":")), encoding="utf-8")
    print(f"radar clippeurs : {len(liste)} comptes, {len(alertes)} alerte(s), {erreurs} illisible(s)")


if __name__ == "__main__":
    main()
