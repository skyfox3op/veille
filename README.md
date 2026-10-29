# Veille : campagnes de clipping + streamers

Tourne tout seul sur GitHub Actions et envoie des alertes Discord :

| Script | Ce qu'il surveille | Salon Discord (secret) |
|---|---|---|
| `veille.py` | nouvelles campagnes Content Rewards (filtres : `veille.json`) | `DISCORD_WEBHOOK`, `DISCORD_WEBHOOK_TOP` |
| `streamers.py` | lives YouTube / Twitch / Kick et nouvelles vidéos YouTube (liste : `streamers.json`) | `DISCORD_WEBHOOK_LIVES` (lives), `DISCORD_WEBHOOK_VIDEOS` (vidéos) |

## Fréquence (automatique)
- Dépôt **privé** : 2 passages par heure (≈ 1 440 min / mois, sous les 2 000 min gratuites).
- Dépôt **public** : toutes les 5 minutes (minutes illimitées). Si ça ne s'accélère pas après le passage en public,
  crée la variable `VEILLE_RAPIDE` = `oui` (Settings > Secrets and variables > Actions > Variables).
  Ne la crée PAS tant que le dépôt est privé (le quota gratuit serait épuisé en 1 semaine).

## Sécurité
- Aucun secret dans le code : uniquement dans Settings > Secrets and variables > Actions > Secrets.
  Les secrets ne sont jamais visibles, même si le dépôt devient public.
- Les fichiers `*_etat.json` ne contiennent que des identifiants publics (campagnes vues, vidéos vues, état des lives).
- Le workflow ne se lance que sur horaire ou à la main : aucune pull request extérieure ne peut l'exécuter.

## Ajouter / retirer un streamer
Modifie `streamers.json` sur GitHub (icône crayon) : `youtube` = identifiant de chaîne `UC...`,
`twitch` / `kick` = pseudo dans l'URL, `null` si absent.

## Tester
Onglet Actions > « Veille (campagnes + streamers) » > Run workflow : envoie un état de test dans chaque salon.

## Radar : posts qui percent (`radar.py`)
- Liste à modifier : `comptes.json` (YouTube `@pseudo`, TikTok pseudo). Chaque modification lance un passage immédiat
  et envoie un résumé dans le salon radar.
- Alerte si un post récent dépasse les seuils `absolu` (ex. 100 k vues ou 20 k likes en moins de 10 h)
  ou fait `facteur` fois plus que l'habitude du compte au même âge (habitude recalculée 1 fois par jour).
- Historique des posts repérés (titre, texte, vues, likes, raison) : `radar_alertes.json`.
- Secret facultatif : `DISCORD_WEBHOOK_RADAR` (sinon le salon « lives » est utilisé).
- Instagram n'est pas pris en charge : il exige un compte connecté et bannit les comptes qui scannent.
