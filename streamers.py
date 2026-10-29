"""Veille des streamers -> notification Discord (salon « lives »).
Pour chaque streamer de streamers.json :
  - nouvelle vidéo YouTube (flux RSS officiel, sans clé)
  - début / fin de live sur YouTube, Twitch, Kick
À la fin d'un live YouTube ou à chaque nouvelle vidéo, le message contient la commande clip.sh prête à copier.

Usage :
  python3 streamers.py           # un passage (lancé automatiquement par GitHub Actions)
  python3 streamers.py --test    # envoie l'état actuel de tous les streamers (vérification)

Secrets (variables d'environnement GitHub, ou fichier .env en local, jamais dans le code) :
  DISCORD_WEBHOOK_LIVES  (sinon DISCORD_WEBHOOK)    DISCORD_WEBHOOK_VIDEOS (nouvelles vidéos ; sinon salon lives)    KICK_CLIENT_ID / KICK_CLIENT_SECRET (facultatif)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from veille import _env, date_fr, envoyer_discord

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "streamers.json"
ETAT = Path(os.environ.get("STREAMERS_ETAT") or ROOT / "work" / "streamers_etat.json")
NAV = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
DELAI_RELANCE_MIN = 75        # un live coupé puis relancé dans les 75 min ne renotifie pas (> écart entre 2 passages)
ABSENCES_FIN = 2              # « fin de live » seulement après 2 passages de suite sans live (évite les fausses fins)
VIDEO_MAX_HEURES = 48         # on ignore les vidéos plus vieilles (évite les vieux envois au 1er passage)
# 2 lignes à coller ensemble dans le Terminal : monter les clips, puis programmer les Shorts du jour
COMMANDE = ('cd ~/Documents/"ark obs stream"/"smart clipper" && bash clip.sh "{url}"\n'
            'bash publier.sh --aujourdhui')

_ID_YT = re.compile(r"^UC[\w-]{22}$")
_PSEUDO = re.compile(r"^[A-Za-z0-9_]{2,40}$")
_ID_VIDEO = re.compile(r"[\w-]{11}")   # un identifiant de vidéo YouTube : rien d'autre ne va dans la commande


def _get(url: str, entetes: dict = None, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": NAV, "Accept-Language": "en-US,en;q=0.8",
                                               **(entetes or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(3_000_000).decode("utf-8", "replace")      # 3 Mo max par page


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _lire_iso(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- YouTube
def videos_youtube(cid: str, longues_seulement: bool) -> list:
    """Dernières vidéos d'une chaîne via le flux RSS public : [{id, titre, url, publie}]."""
    # flux « UULF » = seulement les vidéos (ni lives ni Shorts) ; sinon flux complet de la chaîne
    xml = _get(f"https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{cid[2:]}" if longues_seulement
               else f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    out = []
    for e in ET.fromstring(xml).findall("a:entry", ns):
        vid = e.findtext("yt:videoId", "", ns)
        lien = (e.find("a:link", ns).get("href") if e.find("a:link", ns) is not None else "") or ""
        if not _ID_VIDEO.fullmatch(vid) or (longues_seulement and "/shorts/" in lien):
            continue
        out.append({"id": vid, "titre": e.findtext("a:title", "", ns), "url": f"https://youtu.be/{vid}",
                    "publie": e.findtext("a:published", "", ns)})
    return out


def live_youtube(cid: str):
    """Renvoie {id, titre, url} si la chaîne est en direct, sinon None."""
    html = _get(f"https://www.youtube.com/channel/{cid}/live",
                {"Cookie": "CONSENT=YES+cb; SOCS=CAI"})              # évite la page de consentement
    c = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/([^"]+)"', html)
    if not c:                                                        # page de consentement / anti-robot : erreur,
        raise ValueError("page YouTube inattendue")                  # l'état précédent est conservé
    m = re.fullmatch(r"watch\?v=([\w-]{11})", c.group(1))
    if not m:
        if c.group(1).startswith(("channel/", "@")):
            return None                                              # pas de live : la page renvoie vers la chaîne
        raise ValueError("page YouTube inattendue")
    vid = m.group(1)
    i = html.find(f'"videoDetails":{{"videoId":"{vid}"')
    bloc = html[i:i + 40000] if i >= 0 else ""
    if '"isUpcoming":true' in bloc:                                  # live programmé mais pas commencé
        return {"a_venir": True, "id": vid}
    if '"isLive":true' not in bloc:
        return None
    t = re.search(r'<meta name="title" content="([^"]*)"', html)
    return {"id": vid, "titre": _html(t.group(1)) if t else "", "url": f"https://youtu.be/{vid}"}


def rediffusion_live(vid: str) -> bool:
    """True si la vidéo est un live terminé (la page de la vidéo contient « isLiveContent »:true)."""
    try:
        html = _get(f"https://www.youtube.com/watch?v={vid}", {"Cookie": "CONSENT=YES+cb; SOCS=CAI"})
        return '"isLiveContent":true' in html
    except Exception:
        return False                                     # en cas de doute, on annonce


# ---------------------------------------------------------------- Twitch (page publique, sans clé)
def live_twitch(login: str):
    html = _get(f"https://www.twitch.tv/{login}")
    if '"isLiveBroadcast":true' not in html:
        return None
    t = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    return {"id": None, "titre": _html(t.group(1)) if t else "", "url": f"https://www.twitch.tv/{login}"}


# ---------------------------------------------------------------- Kick
_jeton_kick = {"valeur": None, "essaye": False}


def _jeton(env: dict):
    """Jeton d'application Kick (API officielle), seulement si les secrets KICK_* existent."""
    if _jeton_kick["essaye"]:
        return _jeton_kick["valeur"]
    _jeton_kick["essaye"] = True
    cid, sec = env.get("KICK_CLIENT_ID"), env.get("KICK_CLIENT_SECRET")
    if not (cid and sec):
        return None
    corps = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": cid,
                                    "client_secret": sec}).encode()
    req = urllib.request.Request("https://id.kick.com/oauth/token", data=corps, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": NAV})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            _jeton_kick["valeur"] = json.loads(r.read(100_000)).get("access_token")
    except Exception:
        print("Kick : impossible d'obtenir le jeton (vérifie KICK_CLIENT_ID / KICK_CLIENT_SECRET)")
    return _jeton_kick["valeur"]


def live_kick(slug: str, env: dict):
    url = f"https://kick.com/{slug}"
    jeton = _jeton(env)
    if jeton:                                                        # API officielle
        req = urllib.request.Request(f"https://api.kick.com/public/v1/channels?slug={slug}",
                                     headers={"Authorization": f"Bearer {jeton}", "User-Agent": NAV,
                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = (json.loads(r.read(2_000_000)).get("data") or [{}])[0]
        s = data.get("stream") or {}
        if not s.get("is_live"):
            return None
        return {"id": None, "titre": data.get("stream_title") or "", "url": url}
    j = json.loads(_get(f"https://kick.com/api/v2/channels/{slug}", {"Accept": "application/json"}))
    s = j.get("livestream")                                          # API du site (peut être bloquée)
    if not s or not s.get("is_live"):
        return None
    return {"id": str(s.get("id") or "") or None, "titre": s.get("session_title") or "", "url": url}


def _html(s: str) -> str:
    import html
    return html.unescape(s)[:200]


# ---------------------------------------------------------------- messages
COULEURS = {"youtube": 0xFF0000, "twitch": 0x9146FF, "kick": 0x53FC18}
NOMS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}


def _duree(debut: str) -> str:
    d = _lire_iso(debut or "")
    if not d:
        return "?"
    m = int((_maintenant() - d).total_seconds() // 60)
    return f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min"


def embed_live(nom: str, plat: str, info: dict) -> dict:
    return {"title": f"🔴 {nom} est en live sur {NOMS[plat]}"[:250], "url": info["url"],
            "description": (info.get("titre") or "")[:300], "color": COULEURS[plat]}


def embed_fin(nom: str, plat: str, ancien: dict) -> dict:
    texte = f"Durée : {_duree(ancien.get('depuis'))}."
    if plat == "youtube" and _ID_VIDEO.fullmatch(str(ancien.get("id") or "")):
        url = f"https://youtu.be/{ancien['id']}"
        texte += f"\nRediffusion à clipper (dispo dans 1 à 3 h), puis Shorts programmés :\n```{COMMANDE.format(url=url)}```"
    else:
        texte += "\nLa rediffusion arrive en général sur sa chaîne YouTube dans les heures qui suivent."
    return {"title": f"⚫ Fin du live {NOMS[plat]} de {nom}"[:250], "description": texte[:1500], "color": 0x555555}


def embed_video(nom: str, v: dict) -> dict:
    return {"title": f"🎬 {nom} : {v['titre']}"[:250], "url": v["url"], "color": 0xFF0000,
            "description": f"Nouvelle vidéo. Pour la clipper puis programmer les Shorts (colle les 2 lignes) :\n```{COMMANDE.format(url=v['url'])}```"[:1500]}


# ---------------------------------------------------------------- passage
def verifier(cfg: dict, env: dict, etat: dict):
    """Renvoie (embeds à envoyer, embeds d'état pour --test, nb d'erreurs)."""
    alertes, resume, erreurs = [], [], 0
    lives, vus = etat.setdefault("lives", {}), etat.setdefault("videos", {})
    maintenant = _maintenant()
    a_venir = set()
    for s in cfg.get("streamers", []):
        nom = str(s.get("nom") or "?")[:60]
        sources = [("youtube", c) for c in (s.get("youtube") or []) if _ID_YT.match(str(c))]
        sources += [(p, s[p]) for p in ("twitch", "kick") if s.get(p) and _PSEUDO.match(str(s[p]))]
        lignes = []
        for plat, ident in sources:
            cle = f"{plat}:{ident}"
            try:
                info = live_youtube(ident) if plat == "youtube" else (
                    live_twitch(ident) if plat == "twitch" else live_kick(ident, env))
                lu = True
            except Exception as e:
                erreurs += 1
                print(f"{nom} / {NOMS[plat]} : lecture impossible ({type(e).__name__})")
                info, lu = None, False                       # on garde l'état précédent tel quel
            if info and info.get("a_venir"):                 # live programmé : pas encore une « nouvelle vidéo »
                a_venir.add(info["id"])
                info = None
            ancien = lives.get(cle) or {}
            if not lu:
                pass
            elif info:
                lignes.append(f"🔴 en live sur {NOMS[plat]}")
                if not ancien.get("live"):
                    fin = _lire_iso(ancien.get("fin") or "")
                    relance = (fin and (maintenant - fin).total_seconds() < DELAI_RELANCE_MIN * 60
                               and (info.get("id") is None or info.get("id") == ancien.get("id")))
                    if not relance:
                        alertes.append(embed_live(nom, plat, info))
                    lives[cle] = {"live": True, "id": info.get("id") or ancien.get("id") if relance else info.get("id"),
                                  "depuis": ancien.get("depuis") if relance else _iso(maintenant)}
                elif info.get("id") and info["id"] != ancien.get("id"):
                    if ancien.get("id"):                     # nouveau live enchaîné : on signale la fin du précédent
                        alertes.append(embed_fin(nom, plat, ancien))
                    lives[cle] = {**ancien, "id": info["id"], "absences": 0}
                elif ancien.get("absences"):
                    lives[cle] = {**ancien, "absences": 0}
            elif ancien.get("live"):
                n = int(ancien.get("absences") or 0) + 1
                if n >= ABSENCES_FIN:
                    alertes.append(embed_fin(nom, plat, ancien))
                    lives[cle] = {**ancien, "live": False, "fin": _iso(maintenant), "absences": 0}
                else:
                    lives[cle] = {**ancien, "absences": n}
                    lignes.append(f"🔴 en live sur {NOMS[plat]} (à confirmer)")
            if plat == "youtube":
                try:
                    videos = videos_youtube(ident, cfg.get("videos_longues_seulement", True))
                except Exception as e:
                    erreurs += 1
                    print(f"{nom} / flux YouTube : lecture impossible ({type(e).__name__})")
                    continue
                deja = vus.get(ident)
                if deja is None:                             # 1re fois : on mémorise sans notifier
                    vus[ident] = [v["id"] for v in videos][:30]
                    continue
                live_id = (lives.get(cle) or {}).get("id")
                for v in reversed(videos):                   # de la plus ancienne à la plus récente
                    if v["id"] in deja or v["id"] in a_venir:
                        continue
                    deja.insert(0, v["id"])
                    d = _lire_iso(v["publie"])
                    recente = d and (maintenant - d).total_seconds() < VIDEO_MAX_HEURES * 3600
                    if recente and v["id"] != live_id:
                        alertes.append(embed_video(nom, v))   # seulement les vraies vidéos, pas les lives finis
                vus[ident] = deja[:30]
                if videos:
                    titre = re.sub(r"[\[\]()]", "", videos[0]["titre"])[:60]    # pas de faux lien Markdown
                    lignes.append(f"dernière vidéo : [{titre}]({videos[0]['url']})")
        resume.append({"title": nom, "description": "\n".join(lignes) or "pas en live", "color": 0x2B2D31})
    return alertes, resume, erreurs


def main():
    test = "--test" in sys.argv
    env = _env()
    webhook = env.get("DISCORD_WEBHOOK_LIVES") or env.get("DISCORD_WEBHOOK", "")
    if not webhook:
        sys.exit("ÉCHEC : ajoute le secret DISCORD_WEBHOOK_LIVES (ou DISCORD_WEBHOOK)")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    premiere = not ETAT.exists()
    etat = json.loads(ETAT.read_text(encoding="utf-8")) if not premiere else {}
    avant = json.dumps(etat, sort_keys=True)

    alertes, resume, erreurs = verifier(cfg, env, etat)

    if test or premiere:
        intro = "🧪 Test de la veille streamers" if test else "👀 Veille streamers activée"
        envoyer_discord(webhook, resume, f"{date_fr()}\n{intro} : état actuel"
                        + (f" ({erreurs} source(s) illisible(s), voir le journal GitHub)" if erreurs else ""))
    if alertes and not premiere:                              # en test aussi : sinon l'alerte serait perdue
        videos = [e for e in alertes if e.get("title", "").startswith("🎬")]   # nouvelles vidéos YouTube
        lives = [e for e in alertes if e not in videos]                        # débuts / fins de live
        webhook_videos = env.get("DISCORD_WEBHOOK_VIDEOS") or webhook
        if lives:
            envoyer_discord(webhook, lives, date_fr(), ping=bool(cfg.get("ping_lives", True)))
        if videos:
            envoyer_discord(webhook_videos, videos, f"{date_fr()}\n🎬 Nouvelle(s) vidéo(s) YouTube")
    print(f"{datetime.now():%H:%M} {len(cfg.get('streamers', []))} streamers, {len(alertes)} alerte(s), "
          f"{erreurs} erreur(s)")

    if premiere or json.dumps(etat, sort_keys=True) != avant:     # on n'écrit que si quelque chose a changé
        ETAT.parent.mkdir(parents=True, exist_ok=True)
        ETAT.write_text(json.dumps(etat, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()
