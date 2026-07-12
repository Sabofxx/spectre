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
python run.py cluster     # regroupement par événement (seuil : --threshold, défaut 0.92)
python run.py analyze     # blindspots + contraste de vocabulaire (sans écraser les catégories v2)
python run.py render      # génération du site statique dans site/
python run.py pipeline    # les quatre dans l'ordre (+ purge > 30 jours)
python run.py serve       # sert site/ sur http://127.0.0.1:8000
python run.py inspect     # calibration : clusters aléatoires + paires en zone grise
python run.py check-leaks # vérifie qu'aucun chapô RSS ne fuit dans le HTML public
python run.py check-classifications # vérifie le référentiel sources/orientations/styles
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

## Analyse qualitative locale (Ollama, optionnelle)

En plus des statistiques, `python run.py analyze --ollama` génère une analyse
qualitative du cadrage (résumé neutre, angle par bord, omissions) via un LLM
**local** — [Ollama](https://ollama.com), aucun service payant. Modèle par
défaut : `llama3.2:3b` (validé sur machine 8 Go ; `qwen3:4b` dispo via la variable d'env mais trop lent sous ~6 Go de VRAM), configurable :

```bash
ollama pull llama3.2:3b                    # le défaut
export SPECTRE_OLLAMA_MODEL=qwen3:4b       # si ≥ 16 Go de RAM (think désactivé automatiquement)
```

Garde-fous : clusters de 4 à 15 articles avec ≥ 2 orientations seulement,
sortie JSON validée strictement (schéma, longueurs max), rejet de toute
réponse qui recopie ≥ 8 mots consécutifs d'un titre, cache (re-analyse
uniquement si la composition du cluster a changé de ≥ 2 articles). Les
sections publiées sont étiquetées comme générées par IA.

### Boucle locale → site

Les runners GitHub n'ont pas Ollama : les analyses qualitatives se font **sur
ta machine** et voyagent via la base committée.

```bash
git pull --rebase                          # la base bouge toutes les 30 min (cron)
python run.py analyze --ollama             # analyse les clusters actifs
# RELIS les sections générées (pages cluster en local) avant de committer :
# sur un petit modèle, ~30 % des sorties échouent à la relecture humaine
# (inversions factuelles, hallucinations) malgré la validation automatique.
git add spectre.db && git commit -m "chore: analyses qualitatives" && git push
# si un cron est passé entre-temps : git pull --rebase puis git push à nouveau
```

Le prochain déploiement (cron ou `workflow_dispatch`) rendra les sections.
Normal et attendu : le payload d'un cluster encore actif peut être écrasé
plus tard si le cluster évolue (≥ 2 nouveaux articles) ; et un cluster sorti
de la fenêtre de 72 h garde son analyse stockée telle quelle, elle n'est
jamais recalculée (ses chapôs ont été purgés).

## Méthode (résumé)

- **Ingestion** : flux RSS uniquement, titres + chapôs. Pas de scraping du texte
  intégral (droits voisins). Dédup par guid puis URL canonique sans trackers.
  **Les extraits RSS (chapôs) ne sont conservés que le temps du traitement
  (72 h), jamais publiés** : passé la fenêtre de clustering, ils sont effacés de
  la base — le repo public ne redistribue donc pas les contenus de presse, il ne
  contient que titres, liens et résultats d'analyse.
- **Clustering** : embeddings `intfloat/multilingual-e5-small` (local,
  CPU, préfixe `query:`), clustering incrémental glouton sur fenêtre 72 h,
  seuil 0.92 calibré sur données réelles (les similarités E5 sont compressées
  vers le haut : médiane du corpus 0.81), règle anti-chaînage (similarité exigée au centroïde ET au
  membre le plus proche).
- **Blindspots** : couverture en sources distinctes par bord, normalisée par le
  nombre de sources actives du bord. Score −1 (gauche seule) à +1 (droite seule),
  angle mort à ≥ 80 % d'un seul côté.
- **Style éditorial** : chaque source porte une dominante `factuel`, `mixte` ou
  `opinion` pour distinguer les fils d'actualité bruts des médias plus
  éditorialisés. Cette dimension complète l'axe politique sans le remplacer.
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

## Maintenance

- **Taille du repo** : la base SQLite est committée à chaque run apportant du
  contenu neuf (les runs sans nouvel article ne committent pas). SQLite étant
  binaire, les deltas git sont médiocres : surveiller `du -sh .git` de temps en
  temps. Si l'historique devient encombrant : squash périodique de la branche,
  ou migrer le stockage de la base vers un artifact/release GitHub.
- **Santé des flux** : le footer du site affiche « flux RSS : X/Y OK au dernier
  passage » (rouge si un flux est tombé). Le détail par flux est dans la table
  `fetch_log`.
- **Blindspots** : les clusters `sport` et `faits-divers` (catégorisation par
  slug d'URL) sont écartés des colonnes principales — leur couverture
  déséquilibrée est structurelle, pas éditoriale.
