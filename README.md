# JobWatch — veille automatique des offres

Robot qui lit toutes les 2 heures les pages carrières de plusieurs centaines d'employeurs,
garde uniquement les **nouvelles** offres pertinentes, leur donne un score et envoie une alerte.

## Confidentialité

Le dépôt est public, mais la partie privée est **chiffrée** dans `vault.enc` (AES-256) :

- les réglages (`config.json`) ;
- la liste des employeurs surveillés (`sources/`) ;
- la mémoire du robot et les offres trouvées (`data/`).

La clé est rangée dans les secrets GitHub (`JOBWATCH_KEY`) : personne ne peut la lire, pas même depuis la page du dépôt.
Seul le robot la reçoit pendant son passage. Le canal des notifications est lui aussi un secret (`NTFY_TOPIC`).

## Ce que contient le dépôt

| Fichier | Rôle |
|---|---|
| `watcher.py` | Le robot (Python) : lit les sources, filtre, note, alerte. |
| `make_index.py` | Construit l'index des sources pour le tableau de bord. |
| `vault.sh` | Chiffre (`lock`) et déchiffre (`unlock`) la partie privée. |
| `vault.enc` | La partie privée, chiffrée. |
| `.github/workflows/jobwatch.yml` | Le planning : toutes les 2 h de 7 h à 23 h (heure UK). |

## Modifier les réglages (sur le PC)

```bash
export JOBWATCH_KEY="<la clé>"
./vault.sh unlock      # fait apparaître config.json, sources/, data/
# ... modifier ...
./vault.sh lock        # rechiffre dans vault.enc
git add vault.enc && git commit -m "réglages" && git push
```

Les fichiers déchiffrés ne sont jamais envoyés : ils sont listés dans `.gitignore`.

## Coût

Gratuit : les dépôts publics ont des minutes GitHub Actions illimitées.
