# Contribuer à Spectre

Spectre est un agrégateur de presse française open source, 100 % gratuit et
statique. Les contributions les plus utiles :

## Contester un classement

Le référentiel des sources est un fichier public versionné :
[`config/sources.yaml`](config/sources.yaml). Chaque source porte une
orientation (source-level, jamais article-level), une dominante éditoriale,
un propriétaire, et parfois une `classification_note` documentant les cas
limites. Ouvrez une issue avec le template « Contester le classement d'une
source » — arguments sourcés exigés — ou directement une PR sur le YAML.

## Signaler un cluster défectueux

Template « Signaler un regroupement défectueux ». Les fusions thématiques
sont une limite connue ; les signalements servent à calibrer les seuils.

## Ajouter une source

PR sur `config/sources.yaml` : flux RSS vérifié (fetch réel), orientation et
dominante argumentées, propriétaire sourcé (carte Le Monde diplomatique /
Acrimed), champ `paywall`. La CI valide mécaniquement le référentiel
(`python run.py check-classifications`).

## Développement

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -e ".[dev]"
pytest
```

Contraintes non négociables : 0 € (aucune API payante), droits voisins
(aucun chapô RSS dans le HTML public — gardé par `check-leaks`), pas de
framework front, tests verts.
