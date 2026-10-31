"""Radar : repère les posts récents qui « percent » sur les comptes de comptes.json (vrais youtubeurs / streamers)
-> alerte dans le salon Discord des vidéos (DISCORD_WEBHOOK_VIDEOS).

Sources (sans clé, sans connexion) :
  - YouTube : flux RSS officiel de la chaîne (vues + likes de chaque vidéo / Short)
  - TikTok  : yt-dlp (liste des derniers posts avec vues / likes)
  - Instagram : non pris en charge (Instagram exige un compte connecté et bannit les comptes qui scannent)

Deux règles d'alerte (réglables dans comptes.json) :
  - absolu  : moins de X h après la publication, le post dépasse N vues ou N likes
  - relatif : le post fait F fois plus que d'habitude POUR CE COMPTE au même âge
La « moyenne habituelle » (médiane des posts de 2 à 30 jours) n'est recalculée qu'une fois par jour.

Usage :
  python3 radar.py           # un passage (lancé automatiquement par GitHub Actions)
  python3 radar.py --test    # envoie un résumé de ce qui est suivi (vérification)
"""
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from veille import _env, date_fr, envoyer_discord

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "comptes.json"
ETAT = Path(os.environ.get("RADAR_ETAT") or ROOT / "work" / "radar_etat.json")
JOURNAL = Path(os.environ.get("RADAR_JOURNAL") or ROOT / "work" / "radar_alertes.json")
NAV = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
SEUILS_DEFAUT = {"absolu": {"heures_max": 10, "vues": 100000, "likes": 20000},
                 "relatif": {"facteur": 3, "vues_min": 20000, "heures_max": 48}}
DEMI_VIE_H = 12          # courbe « normale » : ~56 % des vues finales à 10 h, ~86 % à 24 h
BASE_MIN_H, BASE_MAX_H = 48, 30 * 24   # posts servant à calculer l'habitude du compte
POSTS_MAX = 60           # posts gardés en mémoire par compte
COMMANDE_DL = ('cd ~/Downloads && yt-dlp -f "bv*[vcodec^=avc1]+ba[ext=m4a]/b" --merge-output-format mp4 '
               '-o "%(uploader)s - %(title).80s.%(ext)s" "{url}"')

_ID_YT = re.compile(r"UC[\w-]{22}")
_HANDLE = re.compile(r"@[\w.-]{2,60}")
_PSEUDO_TT = re.compile(r"[\w.]{2,40}", re.ASCII)
_ID_VIDEO = re.compile(r"[\w-]{6,40}", re.ASCII)
DUREE_MAX_S = 110        # au-delà, les sources restantes attendent le passage suivant (rien n'est perdu)
MAJ_VUES_S = 3600        # les vues d'un post déjà connu ne sont réenregistrées qu'1 fois par heure


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def _heures_depuis(ts: float) -> float:
    return (_maintenant().timestamp() - ts) / 3600


def _get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": NAV, "Accept-Language": "en-US,en;q=0.8",
                                               "Cookie": "CONSENT=YES+cb; SOCS=CAI"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(3_000_000).decode("utf-8", "replace")


def _texte(s: str, n: int = 300) -> str:
    """Texte du post nettoyé (pas de mentions Discord, pas de faux liens), tronqué."""
    s = re.sub(r"\s+", " ", s or "").strip()
    s = s.replace("@", "@​")                      # @everyone / @here neutralisés
    return re.sub(r"[\[\]`]", "", s)[:n]              # pas de faux bloc de commande


# ---------------------------------------------------------------- YouTube
def chaine_youtube(ident: str, cache: dict) -> str:
    """@pseudo -> identifiant UC... (résolu une seule fois puis gardé)."""
    if _ID_YT.fullmatch(ident):
        return ident
    if ident in cache:
        return cache[ident]
    html = _get(f"https://www.youtube.com/{ident}")
    m = re.search(r'"externalId":"(UC[\w-]{22})"', html) or re.search(r'channel_id=(UC[\w-]{22})', html)
    if not m:
        raise ValueError("chaîne introuvable")
    cache[ident] = m.group(1)
    return cache[ident]


def posts_youtube(cid: str) -> list:
    xml = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    out = []
    for e in ET.fromstring(xml).findall("a:entry", ns):
        vid = e.findtext("yt:videoId", "", ns)
        if not re.fullmatch(r"[\w-]{11}", vid):
            continue
        lien = e.find("a:link", ns)
        court = "/shorts/" in (lien.get("href") if lien is not None else "")
        stats = e.find("m:group/m:community/m:statistics", ns)
        note = e.find("m:group/m:community/m:starRating", ns)
        try:
            publie = datetime.fromisoformat(e.findtext("a:published", "", ns)).timestamp()
        except ValueError:
            continue
        out.append({
            "id": vid, "plat": "youtube", "type": "short" if court else "video",
            "url": f"https://www.youtube.com/shorts/{vid}" if court else f"https://youtu.be/{vid}",
            "titre": _texte(e.findtext("a:title", "", ns), 150),
            "texte": _texte(e.findtext("m:group/m:description", "", ns)),
            "publie": publie,
            "vues": int(stats.get("views", 0)) if stats is not None else 0,
            "likes": int(note.get("count", 0)) if note is not None else 0,
        })
    return out


# ---------------------------------------------------------------- TikTok (yt-dlp)
def posts_tiktok(pseudo: str) -> list:
    exe = shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("yt-dlp absent")
    r = subprocess.run([exe, "--flat-playlist", "-J", "--playlist-items", "1:15", "--no-warnings",
                        "--socket-timeout", "10", f"https://www.tiktok.com/@{pseudo}"],
                       capture_output=True, text=True, timeout=40)
    if r.returncode:
        derniere = (r.stderr.strip().splitlines() or [""])[-1][:200]
        raise RuntimeError(f"TikTok illisible : {derniere}")
    out = []
    for v in (json.loads(r.stdout).get("entries") or []):
        vid = str(v.get("id") or "")
        ts = v.get("timestamp")
        if not _ID_VIDEO.fullmatch(vid) or not ts:
            continue
        desc = v.get("description") or v.get("title") or ""
        out.append({
            "id": vid, "plat": "tiktok", "type": "short",
            "url": f"https://www.tiktok.com/@{pseudo}/video/{vid}",
            "titre": _texte(desc, 150), "texte": _texte(desc), "publie": float(ts),
            "vues": int(v.get("view_count") or 0), "likes": int(v.get("like_count") or 0),
        })
    return out


# ---------------------------------------------------------------- calculs
def courbe(age_h: float) -> float:
    """Part des vues finales qu'un post « normal » a déjà atteinte à cet âge."""
    return 1 - math.exp(-max(age_h, 0.25) / DEMI_VIE_H)


def habitude(posts: dict, typ: str) -> dict:
    """Médiane des vues / likes des posts « mûrs » (2 à 30 jours) de ce type."""
    # seulement les posts dont les vues ont été relevées à 48 h ou plus (sinon chiffre gelé trop tôt)
    murs = [p for p in posts.values() if p["type"] == typ
            and (p.get("vu", 0) - p["publie"]) / 3600 >= BASE_MIN_H
            and _heures_depuis(p["publie"]) <= BASE_MAX_H]
    if len(murs) < 3:
        return {}
    return {"vues": statistics.median(p["vues"] for p in murs),
            "likes": statistics.median(p["likes"] for p in murs), "n": len(murs)}


def juger(p: dict, base: dict, s: dict) -> list:
    """Renvoie les raisons d'alerte (vide si le post ne perce pas)."""
    age = _heures_depuis(p["publie"])
    raisons = []
    a = s["absolu"]
    if age <= a["heures_max"] and (p["vues"] >= a["vues"] or (p["likes"] and p["likes"] >= a["likes"])):
        raisons.append(("absolu", f"{_n(p['vues'])} vues · {_n(p['likes'])} likes en {_age(age)}"))
    r = s["relatif"]
    if base and age <= r["heures_max"] and p["vues"] >= r["vues_min"] and base.get("vues"):
        attendu = base["vues"] * courbe(age)
        x = p["vues"] / attendu if attendu else 0
        if x >= r["facteur"]:
            raisons.append(("relatif", f"×{x:.1f} par rapport à d'habitude ({_n(attendu)} vues attendues "
                                       f"à {_age(age)}, médiane du compte {_n(base['vues'])})"))
    return raisons


def _n(x: float) -> str:
    x = float(x or 0)
    return f"{x / 1e6:.1f} M" if x >= 1e6 else (f"{x / 1e3:.0f} k" if x >= 1e3 else f"{x:.0f}")


def _age(h: float) -> str:
    return f"{int(h * 60)} min" if h < 1 else f"{h:.0f} h"


# ---------------------------------------------------------------- messages
COULEURS = {"youtube": 0xFF0000, "tiktok": 0x25F4EE}
NOMS = {"youtube": "YouTube", "tiktok": "TikTok"}


def embed(nom: str, p: dict, raisons: list) -> dict:
    vitesse = p["vues"] / max(_heures_depuis(p["publie"]), 0.25)
    desc = "\n".join(f"{'📈' if k == 'relatif' else '🚀'} {t}" for k, t in raisons)
    if p["texte"] and p["texte"] != p["titre"]:
        desc += f"\n\n> {p['texte'][:250]}"
    desc += f"\n\nTélécharger :\n```{COMMANDE_DL.format(url=p['url'])}```"
    return {"title": f"🔥 {nom} · {NOMS[p['plat']]} {'Short' if p['type'] == 'short' else 'vidéo'} : "
                     f"{p['titre'] or '(sans titre)'}"[:250],
            "url": p["url"], "color": COULEURS[p["plat"]], "description": desc[:3500],
            "footer": {"text": f"{_n(vitesse)} vues / heure"}}


# ---------------------------------------------------------------- passage
def passage(cfg: dict, etat: dict):
    alertes, resume, erreurs = [], [], 0
    comptes = etat.setdefault("comptes", {})
    cache = etat.setdefault("chaines", {})
    s_defaut = {k: {**SEUILS_DEFAUT[k], **((cfg.get("seuils") or {}).get(k) or {})} for k in SEUILS_DEFAUT}
    jour = _maintenant().strftime("%Y-%m-%d")
    debut = _maintenant().timestamp()
    for c in cfg.get("comptes", []):
        nom = str(c.get("nom") or "?")[:60]
        s = {k: {**s_defaut[k], **((c.get("seuils") or {}).get(k) or {})} for k in s_defaut}
        sources = [("youtube", y) for y in (c.get("youtube") or [])
                   if _ID_YT.fullmatch(str(y)) or _HANDLE.fullmatch(str(y))]
        if c.get("tiktok") and _PSEUDO_TT.fullmatch(str(c["tiktok"]).lstrip("@")):
            sources.append(("tiktok", str(c["tiktok"]).lstrip("@")))
        lignes = []
        for plat, ident in sources:
            if _maintenant().timestamp() - debut > DUREE_MAX_S:
                lignes.append(f"⏳ {NOMS[plat]} {ident} : reporté au passage suivant")
                continue
            try:
                lus = posts_youtube(chaine_youtube(ident, cache)) if plat == "youtube" else posts_tiktok(ident)
            except Exception as e:
                erreurs += 1
                print(f"{nom} / {NOMS[plat]} {ident} : lecture impossible ({type(e).__name__}: {str(e)[:200]})")
                lignes.append(f"⚠️ {NOMS[plat]} {ident} : illisible pour l'instant")
                continue
            cle = f"{plat}:{ident}"
            mem = comptes.setdefault(cle, {"posts": {}, "habitude": {}, "habitude_jour": ""})
            premiere = not mem["posts"]
            maintenant = _maintenant().timestamp()
            for p in lus:
                ancien = mem["posts"].get(p["id"])
                if ancien is None:                                     # mémoire minimale : pas de texte stocké ici
                    vieux = premiere and _heures_depuis(p["publie"]) > max(s["absolu"]["heures_max"],
                                                                           s["relatif"]["heures_max"])
                    mem["posts"][p["id"]] = {"publie": p["publie"], "type": p["type"], "vues": p["vues"],
                                             "likes": p["likes"], "vu": maintenant,
                                             "alertes": ["absolu", "relatif"] if vieux else []}
                elif maintenant - ancien.get("vu", 0) >= MAJ_VUES_S:  # vues réenregistrées 1 fois par heure max
                    ancien.update(vues=p["vues"], likes=p["likes"], vu=maintenant)
            if mem["habitude_jour"] != jour:                       # recalcul 1 fois par jour seulement
                mem["habitude"] = {t: habitude(mem["posts"], t) for t in ("video", "short")}
                mem["habitude_jour"] = jour
            for p in lus:
                m = mem["posts"][p["id"]]
                raisons = [r for r in juger(p, mem["habitude"].get(p["type"]) or {}, s) if r[0] not in m["alertes"]]
                if raisons:
                    alertes.append((nom, p, raisons))
                    m["alertes"] = m["alertes"] + [k for k, _ in raisons]
            # mémoire bornée : les POSTS_MAX plus récents, 30 jours max
            garder = sorted(mem["posts"].items(), key=lambda kv: -kv[1]["publie"])[:POSTS_MAX]
            mem["posts"] = {k: v for k, v in garder if _heures_depuis(v["publie"]) <= BASE_MAX_H + 24}
            h = mem["habitude"]
            if any(h.values()):
                hab = " / ".join(f"{t} {_n(h[t]['vues'])} vues" for t in ("video", "short") if h.get(t))
            else:
                hab = "en cours de calcul (il faut des posts de plus de 2 jours)"
            lignes.append(f"{NOMS[plat]} {ident} : {len(lus)} posts lus · habitude {hab}")
        resume.append({"title": nom, "description": "\n".join(lignes)[:1500] or "aucune source valide",
                       "color": 0x2B2D31})
    return alertes, resume, erreurs


def main():
    test = "--test" in sys.argv
    env = _env()
    # vidéos des vrais youtubeurs/streamers -> salon « vidéos » (le salon « radar » est réservé aux TikTok
    # des clippeurs, surveillés depuis le Mac : TikTok bloque GitHub)
    webhook = env.get("DISCORD_WEBHOOK_VIDEOS") or env.get("DISCORD_WEBHOOK_LIVES") or env.get("DISCORD_WEBHOOK", "")
    if not webhook:
        sys.exit("ÉCHEC : ajoute le secret DISCORD_WEBHOOK_VIDEOS (ou DISCORD_WEBHOOK_LIVES)")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    etat = json.loads(ETAT.read_text(encoding="utf-8")) if ETAT.exists() else {}
    avant = json.dumps(etat, sort_keys=True)

    alertes, resume, erreurs = passage(cfg, etat)

    if test:
        envoyer_discord(webhook, resume, f"{date_fr()}\n🧪 Test du radar : comptes suivis"
                        + (f" ({erreurs} source(s) illisible(s))" if erreurs else ""))
    if alertes:
        envoyer_discord(webhook, [embed(n, p, r) for n, p, r in alertes], f"{date_fr()}\n🔥 Ça perce :", ping=True)
        journal = json.loads(JOURNAL.read_text(encoding="utf-8")) if JOURNAL.exists() else []
        for n, p, r in alertes:                                      # historique lisible des posts repérés
            journal.append({"quand": _maintenant().strftime("%Y-%m-%dT%H:%MZ"), "compte": n, "plateforme": p["plat"],
                            "url": p["url"], "titre": p["titre"], "texte": p["texte"], "vues": p["vues"],
                            "likes": p["likes"], "raisons": [t for _, t in r]})
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        JOURNAL.write_text(json.dumps(journal[-300:], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{datetime.now():%H:%M} radar : {len(cfg.get('comptes', []))} comptes, {len(alertes)} alerte(s), "
          f"{erreurs} erreur(s)")
    if json.dumps(etat, sort_keys=True) != avant:
        ETAT.parent.mkdir(parents=True, exist_ok=True)
        ETAT.write_text(json.dumps(etat, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
