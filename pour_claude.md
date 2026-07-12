# Pour Claude Code - revue dure et objective de Spectre

Ce fichier est un brief de revue pour Claude Code. Le but n'est pas de valider
gentiment le travail fait, mais de chercher les erreurs, les regressions, les
fausses hypotheses et les choix trop fragiles.

Claude, sois strict :

- Ne dis pas "oui" pour faire plaisir.
- Priorise les bugs, regressions comportementales, erreurs de methode,
  risques de donnees, risques juridiques et trous de tests.
- Si une classification politique/source-level est discutable, signale-la.
- Si un changement frontend est joli mais nuit a la lisibilite, signale-le.
- Si un garde-fou donne une illusion de securite, signale-le.
- Si un test ne couvre pas le vrai risque, dis-le.
- Donne des references fichier/ligne quand possible.
- Termine par une liste courte de corrections recommandees, ordonnee par impact.

## Contexte projet

Projet : `spectre`

Objectif produit : agregateur de presse francaise inspire de Ground News.
Il ingere uniquement les flux RSS, regroupe les articles par evenement, puis
affiche :

- qui couvre quoi selon l'orientation source-level des medias ;
- les "blindspots" entre blocs gauche et droite ;
- les contrastes de vocabulaire entre blocs ;
- une dimension `editorial_style` : `factuel`, `mixte`, `opinion`.

Contraintes fortes du projet :

- Pas de scraping full-text.
- Pas de redistribution des chapeaux RSS dans le HTML public.
- Monolithe Python + SQLite + Jinja, pas de microservices.
- Site statique genere dans `site/`.
- Base SQLite `spectre.db` commitee.
- Les classifications doivent rester indicatives, source-level, jamais article-level.
- L'utilisateur exige surtout qu'il n'y ait pas d'erreur grossiere dans :
  orientations gauche/centre/droite ;
  style factuel/mixte/opinion ;
  blindspots ;
  categories structurelles.

## Etat Git au moment du brief

Derniers commits connus :

- `990d62a style: polish static site interface`
- `358c32c feat: add editorial style v2`
- `522d97e feat: embeddings e5-small, blindspots par rubrique, sante des flux, commits CI gates`

Changements non commites au moment de ce fichier :

- `.github/workflows/pipeline.yml`
- `README.md`
- `config/sources.yaml`
- `run.py`
- `spectre.db`
- `spectre/analyze.py`
- `spectre/categorize.py`
- `spectre/cluster.py`
- `templates/_macros.html`
- `templates/apropos.html`
- `templates/archive_week.html`
- `templates/cluster.html`
- `templates/index.html`
- `tests/test_analyze.py`
- `tests/test_categorize.py`
- `tests/test_cluster.py`
- nouveau `data/archive/2026-W28.json`
- nouveau `spectre/audit.py`
- nouveau `tests/test_audit.py`

Stat diff actuel :

```text
.github/workflows/pipeline.yml |   4 +-
README.md                      |   3 +-
config/sources.yaml            | 131 +++++++++++++++++++++++++++++++++++++++++
run.py                         |  32 +++++++++-
spectre.db                     | Bin 5300224 -> 9150464 bytes
spectre/analyze.py             |  10 ++--
spectre/categorize.py          |  11 ++++
spectre/cluster.py             | 111 +++++++++++++++++++++++++---------
templates/_macros.html         |   2 +-
templates/apropos.html         |  11 +++-
templates/archive_week.html    |   2 +-
templates/cluster.html         |   2 +-
templates/index.html           |   2 +-
tests/test_analyze.py          |  14 +++++
tests/test_categorize.py       |   6 ++
tests/test_cluster.py          |  38 ++++++++++--
16 files changed, 330 insertions(+), 49 deletions(-)
```

Note : `data/archive/2026-W28.json` et `spectre/audit.py`/`tests/test_audit.py`
sont nouveaux et apparaissent hors de ce stat si non stages selon la commande.

## Changements deja faits avant ma reprise, dans les commits precedents

Ces changements sont presents dans l'historique recent et doivent aussi etre
revus car ils conditionnent la suite.

### 1. Consolidation periodique des clusters

Fichiers concernes :

- `spectre/cluster.py`
- `spectre/db.py`
- tests associes dans `tests/test_cluster.py`

Idee initiale :

- Apres le clustering glouton, fusionner des clusters actifs dont les centroides
  convergent au-dessus du seuil.
- Objectif : recuperer du recall sans baisser le seuil d'attache `0.92`.

Point critique decouvert ensuite :

- La version initiale utilisait une logique de composantes connexes/union-find.
- Avec beaucoup de nouvelles sources, cela a cree un megacluster de 677 articles,
  titre "Canicule...", contenant manifestement trop de sujets.
- C'est une regression grave.

Correction non commitee faite maintenant :

- Suppression de la fusion transitive.
- Fusion paire par paire seulement.
- Un cluster deja fusionne dans une passe ne refusionne pas dans la meme passe.
- Ajout d'un cap `MAX_CONSOLIDATED_MEMBERS = 60`.
- Ajout d'un cap `MAX_CONSOLIDATED_ARTICLES_PER_SOURCE = 8`.
- Ajout d'un recouvrement minimal de tokens forts dans les titres.

Question a revoir :

- Est-ce que ces garde-fous corrigent vraiment le risque de megacluster, ou
  est-ce qu'ils vont seulement le repousser ?
- Le seuil `0.92` reste-t-il coherent avec la consolidation ?
- Le cap 60 est-il arbitraire au point de masquer de vrais evenements nationaux ?
- Le recouvrement lexical par titre est-il trop fragile pour les articles avec
  synonymes ou titres tres differents ?
- Faut-il faire plusieurs passes controlees ou une seule passe est-elle mieux ?

### 2. Tri du feed par sources distinctes

Fichier :

- `spectre/render.py`

Changement :

- Les cartes du feed sont triees par `n_sources` distinctes puis `n_members`.
- Objectif : eviter qu'un media qui publie beaucoup d'articles remonte
  artificiellement un evenement.

Questions :

- Est-ce que le tri doit aussi tenir compte de la diversite d'orientation ?
- Est-ce qu'un sujet couvert par 3 sources du meme groupe devrait compter comme
  3 sources ou etre dedupe par proprietaire ?

### 3. Archives hebdomadaires

Fichiers :

- `spectre/archive.py`
- `templates/archives.html`
- `templates/archive_week.html`
- `run.py`
- `.github/workflows/pipeline.yml`
- `data/archive/2026-W28.json`

Changement :

- Creation de snapshots hebdo JSON dans `data/archive/`.
- Rendu de pages archives statiques.
- Le pipeline ecrit un snapshot de la semaine courante.
- Le workflow git ajoute `data/archive` avec `spectre.db`.

Questions :

- Le snapshot contient-il uniquement des donnees autorisees ?
- Les URLs et titres originaux sont-ils suffisants/legalement ok ?
- La logique "semaine courante reecrite, semaines passees figees" est-elle
  vraiment respectee ?
- Est-ce que le snapshot devrait etre cree avant ou apres analyse/render ?
- Est-ce que l'archive devrait conserver `blindspot_score`, `category`,
  `n_sources`, counts par bloc et lien representatif de facon stable ?

### 4. Categorisation v2 via prototypes embeddings

Fichiers :

- `spectre/categorize.py`
- `spectre/cluster.py`
- tests `tests/test_categorize.py`

Changement :

- Vote URL-slug d'abord.
- Fallback embedding en comparant le centroide du cluster a des prototypes de
  categories embeddes.
- Seuil `PROTOTYPE_SIM_FLOOR = 0.82`.

Probleme trouve :

- `cluster.run()` categorisait avec le modele embeddings.
- Puis `analyze.run()` recategorisait sans modele, donc retombait en URL-only et
  ecrasait une partie des categories.

Correction non commitee :

- `analyze.run(conn, categorize=True)` prend maintenant un flag.
- Le pipeline appelle `analyze_mod.run(conn, categorize=False)`.
- La commande CLI `python run.py analyze` ne recategorise plus par defaut.
- Option explicite `--categorize` ajoutee si on veut forcer URL-only.

Questions :

- Est-ce que `analyze --categorize` devrait exister si c'est moins bon ?
- Est-ce que le nom de l'option est clair ou dangereux ?
- Est-ce que la categorisation devrait vivre dans une commande dediee ?
- Est-ce qu'une analyse lancee seule apres ingestion mais avant cluster doit
  echouer plutot que tourner avec donnees obsoletes ?

### 5. Flux RSS sortant des blindspots

Fichiers :

- `templates/blindspots.xml`
- `spectre/render.py`
- `templates/blindspots.html`

Changement :

- Generation de `site/blindspots.xml`.
- Items RSS pointent vers l'article original representatif.

Questions :

- Est-ce qu'un flux RSS sortant qui pointe vers l'article original est le bon
  choix, ou devrait-il pointer vers la page cluster Spectre ?
- Est-ce que le XML echappe correctement les titres/descriptions ?
- Est-ce que `SITE_BASE_URL` est correct pour GitHub Pages ?

### 6. Ollama qwen3:4b

Fichiers :

- probablement `spectre/analyzers.py`, `README.md` ou config env selon diff
  historique.

Changement :

- Modele par defaut passe a `qwen3:4b` ou recommande.

Questions :

- Est-ce que qwen3:4b est disponible sur toutes les machines cibles ?
- Est-ce que le prompt/schema restent robustes avec ce modele ?
- Est-ce que les tests mockes couvrent assez les hallucinations/citations ?

### 7. Refonte HTML/CSS

Fichiers :

- `templates/*.html`
- `templates/style.css`
- `spectre/render.py`

Changement :

- Refonte visuelle deja commitee dans `990d62a`.
- UI plus dense, plus editorial dashboard.
- Ajout de KPI, pills, badges style, layout de cartes, meilleur header/nav.

Questions :

- Est-ce que l'interface est belle sans faire "IA" ?
- Est-ce que les textes tiennent sur mobile ?
- Est-ce que les couleurs gauche/droite ne biaisent pas trop la lecture ?
- Est-ce que les contrastes WCAG sont corrects ?
- Est-ce que la page `blindspots.html` reste utile quand il y a beaucoup de
  sujets structurels ?

## Changements non commites faits depuis ma reprise

### A. Audit des classifications source-level

Fichiers :

- `spectre/audit.py`
- `tests/test_audit.py`
- `run.py`
- `.github/workflows/pipeline.yml`
- `README.md`

Ajouts :

- Nouvelle commande :

```bash
python run.py check-classifications
```

- Le workflow CI lance ce check avant le pipeline.
- L'audit verifie :
  - top-level `classification`;
  - `reviewed_at`;
  - `scope: source-level`;
  - `basis`;
  - champs obligatoires par source :
    - `id`
    - `name`
    - `orientation`
    - `editorial_style`
    - `owner`
    - `rss`
    - `active`
  - ids uniques ;
  - orientation dans le vocabulaire autorise ;
  - editorial_style dans le vocabulaire autorise ;
  - active boolean ;
  - source active avec au moins un RSS ;
  - URL RSS `http/https` valide ;
  - pas de flux RSS declare deux fois.

Limite volontaire :

- Cet audit ne prouve pas qu'une orientation politique subjective est "vraie".
- Il empeche les erreurs mecaniques et les implicites.

Questions de revue :

- Est-ce suffisant pour repondre a l'exigence "je ne tolere aucune erreur" ?
- Faut-il ajouter un champ `classification_note` par source pour documenter les
  cas limites ?
- Faut-il separer `owner` et `editorial_position_basis` ?
- Faut-il refuser les sources sans `reviewed_at` par source ?
- Faut-il tester les URLs RSS en live dans CI, ou est-ce trop fragile ?
- Faut-il ajouter un `source_type` : presse nationale, regionale, TV, radio,
  service public, pure player, magazine opinion ?

### B. Meta classification dans `config/sources.yaml`

Ajout top-level :

```yaml
classification:
  reviewed_at: "2026-07-11"
  scope: source-level
  basis: "Positionnement editorial public, proprietaires et dominante du flux RSS suivi ; ne qualifie jamais un article isole."
```

Questions :

- La phrase est-elle assez claire ?
- Faut-il indiquer explicitement que `orientation` ne pretend pas evaluer chaque
  article ?
- Faut-il mettre des sources documentaires par media ?

### C. Ajout de 14 sources RSS actives

Sources ajoutees au YAML :

- `alternativeseco` - Alternatives Economiques
  - orientation : `gauche`
  - style : `mixte`
  - owner : `Scop Alternatives Economiques`
  - RSS : `https://www.alternatives-economiques.fr/rss.xml`

- `franceculture` - France Culture
  - orientation : `centre-gauche`
  - style : `mixte`
  - owner : `Radio France (service public)`
  - RSS : `https://www.radiofrance.fr/franceculture/rss`

- `rfi` - RFI
  - orientation : `centre`
  - style : `factuel`
  - owner : `France Medias Monde (service public)`
  - RSS : `https://www.rfi.fr/fr/rss`

- `france24` - France 24
  - orientation : `centre`
  - style : `factuel`
  - owner : `France Medias Monde (service public)`
  - RSS : `https://www.france24.com/fr/rss`

- `publicsenat` - Public Senat
  - orientation : `centre`
  - style : `factuel`
  - owner : `Public Senat / Senat`
  - RSS : `https://www.publicsenat.fr/rss`

- `lcp` - LCP - Assemblee nationale
  - orientation : `centre`
  - style : `factuel`
  - owner : `LCP-Assemblee nationale`
  - RSS : `https://lcp.fr/rss.xml`

- `tv5monde` - TV5MONDE
  - orientation : `centre`
  - style : `factuel`
  - owner : `TV5MONDE SA (audiovisuel public francophone)`
  - RSS : `https://information.tv5monde.com/rss.xml`

- `francebleu` - ICI / France Bleu
  - orientation : `centre`
  - style : `factuel`
  - owner : `Radio France (service public)`
  - RSS : `https://www.radiofrance.fr/francebleu/rss`

- `sudouest` - Sud Ouest
  - orientation : `centre`
  - style : `factuel`
  - owner : `Groupe Sud Ouest`
  - RSS : `https://www.sudouest.fr/rss.xml`

- `ladepeche` - La Depeche du Midi
  - orientation : `centre`
  - style : `factuel`
  - owner : `Groupe La Depeche`
  - RSS : `https://www.ladepeche.fr/rss.xml`

- `nicematin` - Nice-Matin
  - orientation : `centre`
  - style : `factuel`
  - owner : `Groupe Nice-Matin`
  - RSS : `https://www.nicematin.com/rss`

- `leprogres` - Le Progres
  - orientation : `centre`
  - style : `factuel`
  - owner : `Groupe EBRA (Credit Mutuel Alliance Federale)`
  - RSS : `https://www.leprogres.fr/rss`

- `tf1info` - TF1 Info
  - orientation : `centre-droit`
  - style : `factuel`
  - owner : `Groupe TF1 (Bouygues)`
  - RSS : `https://www.tf1info.fr/feeds/rss-une.xml`

- `contrepoints` - Contrepoints
  - orientation : `droite`
  - style : `opinion`
  - owner : `Association Liberaux.org`
  - RSS : `https://www.contrepoints.org/feed`

Flux candidats testes mais non ajoutes car 403/404 ou trop incertains :

- `https://www.publicsenat.fr/rss.xml` : 404
- `https://www.rtl.fr/actu/rss` : 404
- `https://www.letelegramme.fr/rss.xml` : 403
- `https://actu.fr/feed/` : 403
- `https://www.latribune.fr/rss.xml` : 404
- `https://www.marianne.net/rss.xml` : 403
- `https://www.lavoixdunord.fr/rss.xml` : 403
- `https://www.courrierinternational.com/feed/category/actualites/rss.xml` : 404

Autres flux testes OK mais non ajoutes pour l'instant, a reconsiderer :

- `https://fr.euronews.com/rss`
- `https://fr.euronews.com/rss?level=theme&name=news`
- `https://www.huffingtonpost.fr/feeds/index.xml`
- `https://rmc.bfmtv.com/rss/actualites/`

Raison de prudence :

- Eviter d'ajouter trop de doublons de source ou de lignes editoriales
  difficiles a classer sans documenter davantage.

Questions de revue :

- Les orientations choisies sont-elles acceptables source-level ?
- `France Culture` en `centre-gauche/mixte` est-il defendable ou faut-il
  `centre/mixte` ?
- `TF1 Info` en `centre-droit/factuel` est-il defendable ou faut-il `centre` ?
- `Alternatives Economiques` en `gauche/mixte` est-il trop fort ou faut-il
  `centre-gauche/mixte` ?
- `Contrepoints` en `droite/opinion` est-il correct ou faut-il une dimension
  "liberal economique" separee ?
- Les medias regionaux en `centre/factuel` sont-ils un raccourci trop grossier ?
- Ajouter beaucoup de sources centre/factuel fausse-t-il les blindspots ?

### D. DB synchronisee et pipeline relance

Actions executees :

- Pipeline complet avec reseau :

```bash
.venv/bin/python run.py pipeline --stats-file /tmp/spectre-sources-stats.json
```

Resultat ingestion :

- 745 nouveaux articles.
- 35 sources actives.

Resultat final apres corrections/reconstruction :

- `spectre.db` pese environ 9.1M.
- `site/` pese environ 2.9M.
- `data/archive/2026-W28.json` pese environ 24K.

Etat DB final observe :

```text
sources 38
active_sources 35
articles 1952
clusters 909
max_cluster_size 51
blindspot_scores 64
vocab_analyses 43
active orientations:
  centre 15
  centre-droit 4
  centre-gauche 5
  droite 6
  gauche 5
active styles:
  factuel 16
  mixte 12
  opinion 7
```

Note importante :

- L'audit affiche `orientation_counts` sur les 38 sources declarees, y compris
  les 3 inactives.
- La DB active exclut `politis`, `lesechos`, `lepoint`.

Question de revue :

- Est-ce une bonne idee de committer `spectre.db` apres un gros ajout de sources,
  ou faut-il laisser GitHub Actions regenerer ?
- La croissance 5.3M -> 9.1M est-elle acceptable ?
- Faut-il compacter avant commit ?

### E. Correction du pipeline `analyze`

Fichiers :

- `spectre/analyze.py`
- `run.py`
- `tests/test_analyze.py`
- `README.md`

Changement :

- `analyze.run(conn, categorize: bool = True)` accepte un flag.
- Le pipeline appelle `analyze_mod.run(conn, categorize=False)`.
- CLI `python run.py analyze` a maintenant :

```bash
--categorize
```

- Par defaut, la commande `analyze` ne recalcule plus les categories.

Pourquoi :

- Eviter d'ecraser la categorisation v2 faite avec embeddings par une passe
  URL-only.

Question :

- Est-ce que le defaut `categorize=False` sur CLI est correct ?
- Est-ce que `README.md` explique assez cette nuance ?
- Faut-il renommer `--categorize` en `--url-categorize` pour rendre le danger
  explicite ?

### F. Correction forte de la consolidation

Fichiers :

- `spectre/cluster.py`
- `tests/test_cluster.py`

Probleme observe :

- Apres ajout massif de sources, une consolidation par union-find a cree un
  cluster de 677 articles.
- C'etait un vrai bug de precision.

Correction :

- Ajout de constantes :

```python
MAX_CONSOLIDATED_MEMBERS = 60
MAX_CONSOLIDATED_ARTICLES_PER_SOURCE = 8
```

- Ajout `_title_tokens`.
- Ajout `_can_consolidate_pair`.
- Consolidation maintenant :
  - trie les paires par similarite descendante ;
  - ignore les clusters deja fusionnes dans la passe ;
  - exige recouvrement lexical entre titres ;
  - refuse si taille combinee > 60 ;
  - refuse si une source aurait > 8 articles dans le cluster fusionne ;
  - fusionne uniquement une paire a la fois.

Tests ajoutes :

- merge legitime avec recouvrement titre ;
- rejet d'un match embedding sans recouvrement lexical ;
- pas de chaine de multiples merges en une passe.

Questions de revue :

- Le test reproduit-il vraiment le bug 677 articles ou seulement un symptome
  simplifie ?
- Faut-il ajouter un test d'integration avec plusieurs dizaines de clusters
  proches ?
- Le seuil lexical doit-il utiliser tous les titres membres, pas seulement le
  titre central du cluster ?
- Le cap par source est-il bien calcule avant fusion ?
- Faut-il stocker/logguer les paires rejetees pour calibration ?

### G. Nouvelle categorie `environnement`

Fichiers :

- `spectre/categorize.py`
- `tests/test_categorize.py`

Probleme observe :

- Un gros cluster canicule et vigilance rouge etait classe `faits-divers`.
- Comme `faits-divers` est structurel et filtre des colonnes blindspots, cela
  aurait masque un vrai sujet editorial/environnemental.

Correction :

- Ajout category URL/title `environnement`.
- Ajout prototype :

```python
"environnement": "meteo, climat, canicule, chaleur extreme, secheresse, tempete, inondation, vigilance rouge"
```

- `STRUCTURAL_CATEGORIES` reste :

```python
{"sport", "faits-divers"}
```

- Donc environnement reste visible dans les blindspots.

Verification :

- Le plus gros cluster final :

```text
44 51 environnement DIRECT. Canicule: 37 departements places en vigilance rouge...
```

Questions :

- `environnement` est-il le bon nom ou faut-il `meteo-climat` ?
- Faut-il separer meteo courte duree et climat/environnement ?
- Le fallback titre environnement risque-t-il de surclasser des faits divers
  lies a incendies/inondations ?
- L'ordre de patterns URL est-il correct ?

### H. Textes HTML renforces sur source-level

Fichiers :

- `templates/apropos.html`
- `templates/index.html`
- `templates/cluster.html`
- `templates/_macros.html`
- `templates/archive_week.html`

Changements :

- Les libelles precisent davantage que les orientations et styles qualifient
  les sources/flux suivis, pas chaque article.
- `apropos.html` dit maintenant "plus de trente medias francais et francophones".
- Limites des classifications expliquees plus explicitement.

Questions :

- Est-ce assez visible pour eviter une mauvaise interpretation publique ?
- Faut-il afficher cette note directement sur chaque carte ?
- Trop de disclaimers nuisent-ils a l'UX ?

### I. Workflow CI renforce

Fichier :

- `.github/workflows/pipeline.yml`

Changement :

- Ajout de `python run.py check-classifications` avant pipeline/check-leaks.
- Le workflow ajoute maintenant `data/archive` avec `spectre.db` si changements.

Questions :

- L'ordre est-il correct ?
- Le workflow doit-il lancer les tests complets ou seulement les gates rapides ?
- Le commit DB doit-il inclure `data/archive` seulement quand `NEW > 0` ?
- Et si aucun nouvel article mais classifications/sources changent ?

## Validations deja executees

Commandes passees avec succes apres les corrections :

```bash
.venv/bin/python -m pytest
```

Resultat :

```text
76 passed
```

```bash
.venv/bin/python run.py check-classifications
```

Resultat :

```text
Classifications OK - 38 sources, 35 actives ;
orientations={'gauche': 6, 'centre-gauche': 5, 'centre': 15, 'centre-droit': 6, 'droite': 6} ;
styles={'mixte': 14, 'opinion': 8, 'factuel': 16}
```

```bash
.venv/bin/python run.py check-leaks
```

Resultat :

```text
Aucune fuite de chapo dans le HTML.
```

```bash
.venv/bin/python -c "import xml.etree.ElementTree as ET; ET.parse('site/blindspots.xml'); print('blindspots.xml OK')"
```

Resultat :

```text
blindspots.xml OK
```

```bash
git diff --check
```

Resultat : aucune sortie, donc pas d'erreur whitespace detectee.

Dernier rendu :

```text
site built in site: {'feed': 124, 'blindspots': 36, 'details': 124}
```

## Points rouges que Claude doit challenger

### 1. "Aucune erreur" est impossible au sens politique

Le projet peut reduire les erreurs mecaniques, mais ne peut pas garantir
objectivement une classification gauche/droite parfaite.

Question :

- Comment rendre cette limite honnete sans affaiblir le produit ?
- Faut-il remplacer l'axe unique par plusieurs axes comme suggere par
  l'utilisateur :
  - economie gauche/liberal ;
  - societal progressiste/conservateur ;
  - souverainisme/europeisme ;
  - factuel/editorial ?

### 2. Trop de sources centre/factuel

Apres ajout :

- centre actif : 15 sources ;
- gauche active : 5 ;
- centre-gauche active : 5 ;
- centre-droit active : 4 ;
- droite active : 6.

Les blindspots normalisent gauche/droite par nombre de sources actives, mais le
centre reste affiche a part.

Questions :

- Le centre est-il devenu trop dominant dans le feed ?
- Faut-il filtrer/ponderer differemment le centre ?
- Les regionaux generent-ils trop de sujets faits-divers/meteo/sport ?
- Faut-il un type `regional` pour eviter d'interpreter leur agenda local comme
  un signal politique national ?

### 3. Moteur de blindspots

Actuel :

- gauche bloc = `gauche` + `centre-gauche`
- droite bloc = `centre-droit` + `droite`
- centre exclu du score gauche/droite, affiche separement.
- score = couverture normalisee droite vs gauche.
- seuil blindspot : 80% d'un seul cote.

Questions :

- Est-ce que `centre-droit` doit vraiment etre dans le bloc droite ?
- Est-ce que `centre-gauche` doit vraiment etre dans le bloc gauche ?
- Est-ce que certains medias `mixte` devraient moins peser que `factuel` ?
- Est-ce qu'une source opinion devrait peser autant qu'une source factuelle ?
- Est-ce que le score doit compter des proprietaires distincts plutot que
  sources distinctes ?

### 4. Regeneration des clusters

Pour corriger le megacluster, j'ai supprime :

- `analyses`
- `cluster_members`
- `clusters`

Puis j'ai relance `run.py cluster`, `run.py analyze`, `run.py snapshot`,
`run.py render`.

Articles et embeddings ont ete conserves.

Questions :

- Est-ce propre avec SQLite et les FK ?
- Des analyses historiques utiles ont-elles ete perdues ?
- Les anciennes pages clusters dans `site/cluster/` sont-elles correctement
  remplacees ou reste-t-il des fichiers HTML orphelins dans `site/cluster/` ?
  Le site est ignore par git mais l'utilisateur le consulte localement.
- `render.build_site` devrait-il nettoyer `site/cluster/*.html` avant rendu ?

### 5. Site local vs GitHub Pages

Le site local est genere dans `site/`.
GitHub Pages redeploie via artifact.

Questions :

- `SITE_BASE_URL = "https://sabofxx.github.io/spectre/"` est-il toujours exact ?
- Les liens RSS absolus et OpenGraph sont-ils coherents local/prod ?
- Faut-il rendre `SITE_BASE_URL` configurable ?

## Commandes que Claude devrait lancer

Commencer par :

```bash
git status --short
git diff --stat
git diff -- config/sources.yaml
git diff -- spectre/cluster.py
git diff -- spectre/analyze.py
git diff -- spectre/categorize.py
git diff -- spectre/audit.py tests/test_audit.py
git diff -- run.py .github/workflows/pipeline.yml
```

Puis :

```bash
.venv/bin/python -m pytest
.venv/bin/python run.py check-classifications
.venv/bin/python run.py check-leaks
.venv/bin/python -c "import xml.etree.ElementTree as ET; ET.parse('site/blindspots.xml')"
```

DB sanity :

```bash
.venv/bin/python - <<'PY'
from spectre import db
conn = db.connect_readonly('spectre.db')
for sql in [
    'SELECT COUNT(*) FROM sources',
    'SELECT COUNT(*) FROM sources WHERE active=1',
    'SELECT COUNT(*) FROM articles',
    'SELECT COUNT(*) FROM clusters',
    'SELECT MAX(n_members) FROM clusters',
    'SELECT COUNT(*) FROM clusters WHERE blindspot_score IS NOT NULL',
    "SELECT COUNT(*) FROM analyses WHERE kind='vocab_contrast'",
]:
    print(sql, conn.execute(sql).fetchone()[0])
print('top clusters:')
for r in conn.execute('SELECT id,n_members,category,title FROM clusters ORDER BY n_members DESC LIMIT 20'):
    print(r['id'], r['n_members'], r['category'], r['title'][:140])
conn.close()
PY
```

Review UX :

```bash
.venv/bin/python run.py serve
```

Puis ouvrir :

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/blindspots.html`
- `http://127.0.0.1:8000/a-propos.html`
- `http://127.0.0.1:8000/archives.html`

Verifier mobile et desktop.

## Questions finales a repondre sans complaisance

1. Y a-t-il une regression bloquante dans le backend ?
2. Y a-t-il une regression bloquante dans le frontend ?
3. Y a-t-il une erreur de classification source-level manifeste ?
4. Y a-t-il une source ajoutee qui devrait etre retiree ou marquee inactive ?
5. Le correctif de consolidation est-il suffisant contre les megaclusters ?
6. Le pipeline peut-il encore ecraser des donnees correctes ?
7. Le rendu peut-il laisser des pages obsolete dans `site/cluster/` ?
8. Le flux RSS sortant est-il valide et utile ?
9. Le projet respecte-t-il toujours la contrainte droits voisins ?
10. Quels changements doivent etre faits avant commit/push ?

## Format de reponse souhaite de Claude

Reponds avec :

1. Findings critiques, par severite, avec fichiers/lignes.
2. Bugs probables mais a confirmer.
3. Risques methodologiques/produit.
4. Risques classification.
5. Tests manquants.
6. Verdict commit :
   - "OK commit"
   - "Commit apres corrections mineures"
   - "Ne pas commit"
7. Liste des corrections recommandees, dans l'ordre.

Encore une fois : sois dur, objectif, et n'essaie pas de rassurer.
