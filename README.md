# Veille : campagnes de clipping + streamers

Tourne tout seul sur GitHub Actions et envoie des alertes Discord :

| Script | Ce qu'il surveille | Salon Discord (secret) |
|---|---|---|
| `veille.py` | nouvelles campagnes Content Rewards (filtres : `veille.json`) | `DISCORD_WEBHOOK`, `DISCORD_WEBHOOK_TOP` |
| `streamers.py` | lives YouTube / Twitch / Kick et nouvelles vidéos YouTube (liste : `streamers.json`) | `DISCORD_WEBHOOK_LIVES` |

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
