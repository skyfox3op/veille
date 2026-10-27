"""Veille des campagnes Content Rewards -> notification Discord.
Lit la liste publique des campagnes (la même que la page « Discover » du site), garde celles qui passent
tes filtres (veille.json) et envoie une alerte Discord pour chaque NOUVELLE campagne.

Usage :
  python3 veille.py            # un passage (c'est ce que lance GitHub Actions automatiquement)
  python3 veille.py --test     # envoie les 5 meilleures campagnes actuelles sur Discord (vérification)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "veille.json"
ETAT = Path(os.environ.get("VEILLE_ETAT") or ROOT / "work" / "veille_etat.json")
API = "https://contentrewards.com/api/campaign/campaigns/discover?collapseGroups=true&limit=50&view=summary&sortBy=newest"
LIEN = "https://contentrewards.com/c/campaigns/{id}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) smart-clipper-veille/1.0"

DEFAUT = {
    "taux_min_dollars_par_1000_vues": 0.75,
    "budget_restant_min_dollars": 300,
    "progression_max_pourcent": 85,
    "plateformes_acceptees": ["tiktok", "instagram", "youtube"],
    "mots_cles_obligatoires": ["clip"],
    "mots_cles_bonus": ["stream", "twitch", "kick", "kai", "cenat", "n3on", "lacy", "speed", "yomi", "français",
                        "podcast", "streamer", "irl"],
    "mots_cles_exclus": ["ugc", "faceless", "slideshow", "lyric", "sound campaign", "music campaign", "face on camera",
                         "as the sound", "edm track",
                         "roblox", "telegram", "dedicated fan page", "dedicated page"],
    "verifiees_seulement": False,
    "ignorer_sur_candidature": False,
    # salon « meilleures campagnes » (2e webhook DISCORD_WEBHOOK_TOP) : seules celles qui cochent tout
    "top": {
        "taux_min_dollars_par_1000_vues": 1.5,
        "clippeurs_max": 10,
        "publiee_depuis_heures_max": 48,
        "budget_restant_min_dollars": 1000,
        "progression_max_pourcent": 30,
    },
}


def _env() -> dict:
    """Secrets : variables d'environnement (GitHub Actions) sinon fichier .env (Mac)."""
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for l in p.read_text(encoding="utf-8").splitlines():
            if "=" in l and not l.strip().startswith("#"):
                k, v = l.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("DISCORD_WEBHOOK", "DISCORD_WEBHOOK_TOP", "DISCORD_WEBHOOK_LIVES", "NTFY_TOPIC",
              "KICK_CLIENT_ID", "KICK_CLIENT_SECRET"):
        if os.environ.get(k):
            env[k] = os.environ[k].strip()
    return env


def charger_config() -> dict:
    if not CONFIG.exists():
        CONFIG.write_text(json.dumps(DEFAUT, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = dict(DEFAUT)
    cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    cfg["top"] = {**DEFAUT["top"], **(cfg.get("top") or {})}
    return cfg


def recuperer() -> list:
    req = urllib.request.Request(API, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read(5_000_000).decode("utf-8")).get("data", [])


def resumer(c: dict) -> dict:
    m = c.get("metrics") or {}
    budget = (c.get("budgetCents") or 0) / 100
    depense = (m.get("budgetSpentCents") or 0) / 100
    taux = (c.get("primaryPayoutCents") or c.get("cpmMaxRateCents") or 0) / 100
    max_clip = max([p.get("maxPayoutCents") or 0 for p in c.get("payouts") or []] + [0]) / 100
    min_clip = min([p.get("minPayoutCents") or 0 for p in c.get("payouts") or []] or [0]) / 100
    return {
        "id": c["id"], "nom": c.get("name", "?"), "orga": c.get("organizationName", "?"),
        "verifiee": bool(c.get("organizationVerified")), "type": c.get("payoutType", "?"),
        "taux": taux, "budget": budget, "reste": max(0.0, budget - depense),
        "progression": (m.get("budgetProgressBps") or 0) / 100, "plateformes": c.get("platforms") or [],
        "max_clip": max_clip, "min_clip": min_clip, "createurs": m.get("creatorCount") or 0,
        "candidature": bool(c.get("requiresApplication")), "statut": c.get("status"),
        "texte": f"{c.get('name', '')} {c.get('organizationName', '')} {c.get('description', '')}".lower(),
        "cree": c.get("listedAt") or c.get("createdAt"),
    }


def passe(r: dict, cfg: dict) -> bool:
    if r["statut"] != "active":
        return False
    if r["type"] == "cpm" and r["taux"] < cfg["taux_min_dollars_par_1000_vues"]:
        return False
    if r["reste"] < cfg["budget_restant_min_dollars"] or r["progression"] > cfg["progression_max_pourcent"]:
        return False
    if not set(r["plateformes"]) & set(cfg["plateformes_acceptees"]):
        return False
    if cfg["verifiees_seulement"] and not r["verifiee"]:
        return False
    if cfg["ignorer_sur_candidature"] and r["candidature"]:
        return False
    if any(k.lower() in r["texte"] for k in cfg["mots_cles_exclus"]):
        return False
    oblig = cfg["mots_cles_obligatoires"]
    return not oblig or any(k.lower() in r["texte"] for k in oblig)


def _heures(iso: str) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 3600
    except Exception:
        return 999.0


def score(r: dict, cfg: dict) -> float:
    """Classement : prix / 1000 vues (le plus important), fraîcheur, peu de clippeurs, budget, mots-clés."""
    taux = r["taux"] if r["type"] == "cpm" else 1.0            # par post / cagnotte : valeur moyenne
    fraicheur = max(0.0, 1 - _heures(r["cree"]) / 72)          # 1 = publiée à l'instant, 0 = plus de 3 jours
    peu_de_monde = max(0.0, 1 - r["createurs"] / 40)           # 1 = personne, 0 = 40 clippeurs ou plus
    budget = min(r["reste"], 20000) / 20000
    bonus = sum(1 for k in cfg["mots_cles_bonus"] if k.lower() in r["texte"])
    return (taux * 3 + fraicheur * 3 + peu_de_monde * 3 + budget * 1.5 + bonus * 0.7
            + (0.5 if r["verifiee"] else 0) - r["progression"] / 40)


def est_top(r: dict, cfg: dict) -> bool:
    t = cfg["top"]
    return (r["type"] == "cpm" and r["taux"] >= t["taux_min_dollars_par_1000_vues"]
            and r["createurs"] <= t["clippeurs_max"] and _heures(r["cree"]) <= t["publiee_depuis_heures_max"]
            and r["reste"] >= t["budget_restant_min_dollars"] and r["progression"] <= t["progression_max_pourcent"])


_JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
         "novembre", "décembre"]


def date_fr() -> str:
    try:
        from zoneinfo import ZoneInfo
        d = datetime.now(ZoneInfo("Europe/Paris"))
    except Exception:
        d = datetime.now(timezone.utc)
    return f"📅 {_JOURS[d.weekday()]} {d.day} {_MOIS[d.month - 1]} {d.year} · {d:%H:%M}"


def _age(iso: str) -> str:
    try:
        h = _heures(iso)
        return f"il y a {int(h * 60)} min" if h < 1 else (f"il y a {h:.0f} h" if h < 48 else f"il y a {h / 24:.0f} j")
    except Exception:
        return "?"


def embed(r: dict) -> dict:
    taux = {"cpm": f"{r['taux']:.2f} $ / 1000 vues", "pool": "cagnotte partagée",
            "perPost": f"{r['taux']:.0f} $ par post", "retainer": "forfait"}.get(r["type"], r["type"])
    couleur = 0x39FF14 if r["progression"] < 40 else (0xFFEB00 if r["progression"] < 70 else 0xFF6B00)
    return {
        "title": f"{'✅ ' if r['verifiee'] else ''}{r['nom']}"[:250],
        "url": LIEN.format(id=r["id"]),
        "color": couleur,
        "fields": [
            {"name": "Paiement", "value": taux, "inline": True},
            {"name": "Budget restant", "value": f"{r['reste']:,.0f} $ / {r['budget']:,.0f} $ ({r['progression']:.0f} % dépensé)",
             "inline": True},
            {"name": "Par clip", "value": f"min {r['min_clip']:.0f} $ · max {r['max_clip']:.0f} $", "inline": True},
            {"name": "Plateformes", "value": ", ".join(r["plateformes"]) or "?", "inline": True},
            {"name": "Clippeurs", "value": str(r["createurs"]), "inline": True},
            {"name": "Publiée", "value": _age(r["cree"]) + (" · sur candidature" if r["candidature"] else ""),
             "inline": True},
        ],
        "footer": {"text": f"{r['orga']} · Content Rewards"},
    }


def envoyer_discord(url: str, embeds: list, texte: str = "", ping: bool = False):
    """`ping` = mention @everyone : force la notification push sur le téléphone."""
    for i in range(0, len(embeds), 10):                     # Discord : 10 embeds max par message
        contenu = (("@everyone " if ping else "") + texte) if i == 0 else ""
        corps = json.dumps({"content": contenu, "embeds": embeds[i:i + 10],
                            "allowed_mentions": {"parse": ["everyone"] if ping else []}}).encode()
        req = urllib.request.Request(url, data=corps, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        for essai in range(3):
            try:
                urllib.request.urlopen(req, timeout=20).read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and essai < 2:
                    time.sleep(min(float(e.headers.get("retry-after") or 2), 30))
                    continue
                # message volontairement sans l'URL du webhook (c'est un secret)
                raise RuntimeError(f"Discord a refusé le message (HTTP {e.code})") from None
            except Exception as e:                           # jamais l'URL dans le message d'erreur
                raise RuntimeError(f"Discord : échec ({type(e).__name__})") from None
        time.sleep(1)


def envoyer_ntfy(topic: str, camps: list):
    """Notification push sur téléphone via l'appli gratuite ntfy (5 max par passage)."""
    for r in camps[:5]:
        taux = f"{r['taux']:.2f} $/1000 vues" if r["type"] == "cpm" else r["type"]
        corps = json.dumps({
            "topic": topic, "title": r["nom"][:120], "priority": 4, "tags": ["moneybag"],
            "message": f"{taux} · reste {r['reste']:,.0f} $ ({r['progression']:.0f} % dépensé) · "
                       f"{', '.join(r['plateformes'])}",
            "click": LIEN.format(id=r["id"])}).encode("utf-8")
        req = urllib.request.Request("https://ntfy.sh/", data=corps, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            print(f"ntfy : échec ({e})")


def main():
    test = "--test" in sys.argv
    env = _env()
    webhook = env.get("DISCORD_WEBHOOK", "")
    webhook_top = env.get("DISCORD_WEBHOOK_TOP", "")
    ntfy = env.get("NTFY_TOPIC", "")
    if not (webhook or webhook_top or ntfy):
        sys.exit("ÉCHEC : ajoute DISCORD_WEBHOOK=... (et DISCORD_WEBHOOK_TOP=... / NTFY_TOPIC=...) en secret")
    cfg = charger_config()
    try:
        brutes = recuperer()
    except Exception as e:                                   # site indisponible : on réessaiera au prochain passage
        print(f"{datetime.now():%H:%M} site injoignable ({e})")
        return
    camps = [resumer(c) for c in brutes]
    bonnes = sorted([r for r in camps if passe(r, cfg)], key=lambda r: -score(r, cfg))   # la meilleure en 1er

    ETAT.parent.mkdir(parents=True, exist_ok=True)
    etat = json.loads(ETAT.read_text(encoding="utf-8")) if ETAT.exists() else {"vus": []}
    vus = set(etat["vus"])
    premiere = not ETAT.exists()

    def publier(liste: list, intro: str):
        if not liste:
            return
        entete = f"{date_fr()}\n{intro}"
        if webhook:
            envoyer_discord(webhook, [embed(r) for r in liste], entete)
        tops = [r for r in liste if est_top(r, cfg)]
        if webhook_top and tops:
            envoyer_discord(webhook_top, [embed(r) for r in tops],
                            f"{date_fr()}\n🔥 {len(tops)} campagne(s) au top : bien payée, récente, peu de clippeurs",
                            ping=True)
        if ntfy:
            envoyer_ntfy(ntfy, tops or liste)

    if test:
        publier(bonnes[:5], f"🧪 Test : {len(bonnes)} campagnes passent tes filtres, voici les 5 meilleures.")
        print(f"Test envoyé ({len(bonnes)} campagnes filtrées, {sum(est_top(r, cfg) for r in bonnes)} au top).")
        if not premiere:                          # les vraies nouveautés du moment ne sont pas perdues
            nouvelles = [r for r in bonnes if r["id"] not in vus]
            publier(nouvelles, f"🚨 {len(nouvelles)} nouvelle(s) campagne(s), la meilleure en premier")
    elif premiere:   # 1er lancement : on n'inonde pas Discord, on mémorise tout et on envoie un résumé
        publier(bonnes[:5], f"👀 Veille activée. {len(bonnes)} campagnes ouvertes passent tes filtres. Les 5 meilleures :")
    else:
        nouvelles = [r for r in bonnes if r["id"] not in vus]
        publier(nouvelles, f"🚨 {len(nouvelles)} nouvelle(s) campagne(s), la meilleure en premier")
        print(f"{datetime.now():%H:%M} {len(camps)} campagnes lues, {len(nouvelles)} nouvelle(s) envoyée(s)")

    nouveaux_vus = vus | {r["id"] for r in camps}
    if nouveaux_vus != vus or premiere:           # on n'écrit que si quelque chose a changé
        ordre = etat["vus"] + sorted(nouveaux_vus - vus)   # les plus anciens d'abord : on garde les 3000 derniers
        ETAT.write_text(json.dumps({"vus": ordre[-3000:]}, indent=0), encoding="utf-8")


if __name__ == "__main__":
    main()
