# Spectre

Agrégateur de presse française inspiré de Ground News : pour chaque événement
d'actualité, qui le couvre selon l'orientation politique des rédactions, comment
le cadrage diverge entre bords, et quels sujets sont des angles morts
("blindspots") d'un côté du spectre.

**100 % gratuit et local** : aucune API payante, aucun compte tiers, aucun cloud
facturé. Le seul téléchargement est le modèle d'embeddings (~500 Mo, une fois).

## Installation

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu  # CPU-only, ~10x plus léger
pip install -e .
```

Python ≥ 3.11 requis.

## Utilisation

```bash
python run.py ingest      # fetch des flux RSS (titres + chapôs, rien d'autre)
python run.py cluster     # regroupement par événement (seuil : --threshold, défaut 0.70)
python run.py analyze     # blindspots + contraste de vocabulaire
python run.py render      # génération du site statique dans site/
python run.py pipeline    # les quatre dans l'ordre (+ purge > 30 jours)
python run.py serve       # sert site/ sur http://127.0.0.1:8000
python run.py inspect     # calibration : clusters aléatoires + paires en zone grise
python run.py check-leaks # vérifie qu'aucun chapô RSS ne fuit dans le HTML public
```

En local, un cron toutes les 30 minutes :

```cron
*/30 * * * * cd /chemin/vers/spectre && .venv/bin/python run.py pipeline >> pipeline.log 2>&1
```

## Déploiement GitHub Actions + Pages (0 €)

Le workflow `.github/workflows/pipeline.yml` exécute le pipeline toutes les
30 minutes et déploie `site/` sur GitHub Pages via artifact
(`upload-pages-artifact` + `deploy-pages`) — le HTML ne transite jamais par git.
La base SQLite est compactée (checkpoint WAL + VACUUM) puis committée dans le
repo à chaque run où elle change ; la purge à 30 jours et la mise à NULL des
embeddings hors fenêtre de 72 h la gardent petite.

Mise en route :

1. Créer le repo GitHub et pousser le projet (`spectre.db` peut être absent au
   premier run, il sera créé).
2. **Settings → Pages → Source : "GitHub Actions"** (pas "Deploy from a branch").
3. Ajuster `REPO_URL` dans `spectre/render.py` (lien du footer).
4. Lancer le premier run à la main : onglet **Actions → pipeline → Run workflow**
   (`workflow_dispatch`).

⚠️ **Les crons GitHub Actions sont en UTC** (`*/30 * * * *` = toutes les 30 min
UTC, pas heure de Paris) **et le déclenchement est souvent décalé de plusieurs
minutes** selon la charge de la plateforme — normal, pas un bug. Le
`concurrency group` avec `cancel-in-progress: false` garantit qu'un run qui
committe la base n'est jamais annulé par le suivant.

## Méthode (résumé)

- **Ingestion** : flux RSS uniquement, titres + chapôs. Pas de scraping du texte
  intégral (droits voisins). Dédup par guid puis URL canonique sans trackers.
  **Les extraits RSS (chapôs) ne sont conservés que le temps du traitement
  (72 h), jamais publiés** : passé la fenêtre de clustering, ils sont effacés de
  la base — le repo public ne redistribue donc pas les contenus de presse, il ne
  contient que titres, liens et résultats d'analyse.
- **Clustering** : embeddings `paraphrase-multilingual-MiniLM-L12-v2` (local,
  CPU), clustering incrémental glouton sur fenêtre 72 h, seuil 0.70 calibré sur
  données réelles, règle anti-chaînage (similarité exigée au centroïde ET au
  membre le plus proche).
- **Blindspots** : couverture en sources distinctes par bord, normalisée par le
  nombre de sources actives du bord. Score −1 (gauche seule) à +1 (droite seule),
  angle mort à ≥ 80 % d'un seul côté.
- **Contraste de vocabulaire** : log-odds ratio avec prior de Dirichlet
  informatif (Monroe et al. 2008, "Fightin' Words"), prior estimé sur tout le
  corpus, plancher de 50 tokens par bord sous lequel on affiche
  "corpus insuffisant" plutôt qu'un faux contraste.

## Limites connues

- **La classification des orientations est indicative et débattable** — voir la
  page "À propos" du site. Propriétaires sourcés d'après la carte "Médias
  français, qui possède quoi" (Le Monde diplomatique / Acrimed).
- Fusion thématique résiduelle : deux événements proches du même domaine peuvent
  partager un cluster ; une divergence élevée peut signaler ce défaut.
- Le Parisien ne publie que des titres (pas de chapô) ; Les Échos, Le Point et
  Politis n'ont pas de flux RSS accessible (`active: false`).
- Le HTML public ne contient QUE titres, liens vers les originaux et nos
  analyses — jamais les chapôs RSS (vérifié par `check-leaks` en CI avant chaque
  déploiement).
