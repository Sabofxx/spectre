# Données ouvertes Spectre

Instantanés hebdomadaires de la couverture de la presse française par
orientation des sources. **Licence : CC-BY 4.0** (attribution : « Spectre »,
lien vers le site). Cible : chercheurs et data-journalistes.

Un fichier par semaine ISO (`AAAA-Wss.json`) :

- `week`, `generated_at`, `license`, `attribution`
- `clusters[]` : `title` (titre de l'événement), `url` (article original
  représentatif), `n_members`, `n_sources`, `counts` (sources distinctes par
  bloc gauche/centre/droit), `style_counts`, `blindspot_score` (−1 à +1),
  `blindspot_for`, `divergence` (0-1), `category`, `terms_left`/`terms_right`
  (termes sur-représentés, méthode log-odds Monroe et al. 2008).

**Jamais de contenu de presse** : titres et liens uniquement, plus nos
métriques calculées. Méthodologie complète sur la page à-propos du site.
