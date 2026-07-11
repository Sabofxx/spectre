"""Spectre CLI: ingest / cluster / analyze / render / pipeline / serve."""

from __future__ import annotations

import logging
from pathlib import Path

import typer

from spectre import db as dbmod
from spectre import ingest as ingest_mod

app = typer.Typer(add_completion=False, help="Spectre — agrégateur de presse française")

DEFAULT_DB = Path("spectre.db")
DEFAULT_CONFIG = Path("config/sources.yaml")
SITE_DIR = Path("site")


@app.callback()
def main(log_level: str = typer.Option("INFO", help="DEBUG / INFO / WARNING / ERROR")) -> None:
    """Configure logging before any command runs."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _report_ingest(conn) -> None:
    """Print per-source article counts, grouped by orientation."""
    typer.echo(f"\n{'source':<22}{'orientation':<16}{'articles':>9}  latest")
    typer.echo("-" * 70)
    for row in dbmod.source_stats(conn):
        typer.echo(
            f"{row['name']:<22}{row['orientation']:<16}{row['n_articles']:>9}"
            f"  {row['latest'] or '-'}"
        )


@app.command()
def ingest(
    db: Path = typer.Option(DEFAULT_DB, help="Chemin de la base SQLite"),
    config: Path = typer.Option(DEFAULT_CONFIG, help="Chemin de sources.yaml"),
) -> None:
    """Fetch every active RSS feed and store new articles."""
    conn = dbmod.connect(db)
    sources = ingest_mod.load_sources(config)
    dbmod.sync_sources(conn, sources)
    started = dbmod.utcnow_iso()
    n_new = ingest_mod.ingest_all(conn, sources)

    failures = [r for r in dbmod.last_run_fetch_log(conn, started) if r["status"] != "ok"]
    for row in failures:
        typer.echo(
            f"ECHEC  {row['source_id']:<18} {row['status']}"
            f" (http={row['http_code']}) {row['feed_url']}"
        )
    _report_ingest(conn)
    typer.echo(f"\n{n_new} nouveaux articles.")
    conn.close()


@app.command()
def purge(db: Path = typer.Option(DEFAULT_DB)) -> None:
    """Delete articles older than 30 days and NULL stale embeddings."""
    conn = dbmod.connect(db)
    counts = dbmod.purge(conn)
    typer.echo(f"Purge : {counts}")
    conn.close()


@app.command()
def cluster(
    db: Path = typer.Option(DEFAULT_DB),
    threshold: float = typer.Option(None, help="Seuil de similarité (défaut : 0.55)"),
) -> None:
    """Group articles into event clusters (embed + greedy attach)."""
    from spectre import cluster as cluster_mod

    conn = dbmod.connect(db)
    stats = cluster_mod.run(conn, threshold or cluster_mod.DEFAULT_THRESHOLD)
    typer.echo(f"Clustering : {stats}")
    conn.close()


@app.command()
def inspect(
    db: Path = typer.Option(DEFAULT_DB),
    n_clusters: int = typer.Option(10, help="Clusters aléatoires à afficher"),
    n_pairs: int = typer.Option(15, help="Paires en zone grise à afficher"),
    lo: float = typer.Option(0.88),
    hi: float = typer.Option(0.92),
    seed: int = typer.Option(None, help="Graine aléatoire (reproductibilité)"),
) -> None:
    """Manual calibration report: random clusters + gray-zone pairs."""
    from spectre import cluster as cluster_mod

    conn = dbmod.connect(db)

    typer.echo(f"=== {n_clusters} clusters aléatoires (>= 2 membres) ===")
    for item in cluster_mod.sample_clusters(conn, n_clusters):
        c = item["cluster"]
        typer.echo(f"\n#{c['id']} [{c['n_members']} art.] {c['title']}")
        for m in item["members"]:
            typer.echo(
                f"   {m['similarity']:.2f}  {m['source_name']:<16} ({m['orientation']})"
                f"  {m['title']}"
            )

    typer.echo(f"\n=== {n_pairs} paires inter-sources, similarité {lo}-{hi} ===")
    for sim, a, b in cluster_mod.gray_zone_pairs(conn, lo, hi, n_pairs, seed):
        typer.echo(f"\n{sim:.3f}")
        typer.echo(f"   [{a['source_id']}] {a['title']}")
        typer.echo(f"   [{b['source_id']}] {b['title']}")
    conn.close()


@app.command()
def analyze(
    db: Path = typer.Option(DEFAULT_DB),
    ollama: bool = typer.Option(
        False, "--ollama",
        help="Ajoute l'analyse qualitative via un LLM local (Ollama requis)",
    ),
) -> None:
    """Compute blindspots and vocabulary contrast for eligible clusters."""
    import json

    from spectre import analyze as analyze_mod

    conn = dbmod.connect(db)
    stats = analyze_mod.run(conn)
    typer.echo(f"Analyses : {stats}")
    if ollama:
        ostats = analyze_mod.compute_ollama(conn)
        if ostats.get("unavailable"):
            typer.echo("Ollama indisponible — analyse qualitative sautée.")
        else:
            typer.echo(
                f"Ollama : {ostats['analyzed']} analysés, "
                f"{ostats['skipped_cache']} skippés (cache), "
                f"{ostats['invalid']} invalides."
            )

    typer.echo("\n=== Blindspots (|score| >= 0.6) ===")
    for row in dbmod.top_blindspots(conn, 0.6, 8):
        side = "ignoré à GAUCHE" if row["blindspot_score"] > 0 else "ignoré à DROITE"
        typer.echo(
            f"  {row['blindspot_score']:+.2f} [{row['n_members']} art.] {side} — {row['title']}"
        )

    typer.echo("\n=== Contrastes de vocabulaire les plus divergents ===")
    for row in dbmod.top_divergent(conn, 5):
        payload = json.loads(row["payload"])
        typer.echo(f"\n  div={row['divergence_score']:.2f} [{row['n_members']} art.] {row['title']}")
        left = ", ".join(f"{t} ({z})" for t, z in payload["left_terms"][:6])
        right = ", ".join(f"{t} ({z})" for t, z in payload["right_terms"][:6])
        typer.echo(f"    bord gauche : {left}")
        typer.echo(f"    bord droit  : {right}")
    conn.close()


@app.command()
def render(
    db: Path = typer.Option(DEFAULT_DB),
    out: Path = typer.Option(SITE_DIR, help="Dossier de sortie du site statique"),
) -> None:
    """Generate the static site (titles + links only, never RSS summaries)."""
    from spectre import render as render_mod

    conn = dbmod.connect(db)
    stats = render_mod.build_site(conn, out)
    typer.echo(f"Site généré dans {out}/ : {stats}")
    conn.close()


@app.command()
def pipeline(
    db: Path = typer.Option(DEFAULT_DB),
    config: Path = typer.Option(DEFAULT_CONFIG),
    stats_file: Path = typer.Option(
        None, help="Écrit un résumé JSON du run (utilisé par la CI pour "
                   "ne committer la base que s'il y a du contenu neuf)"
    ),
) -> None:
    """ingest -> purge -> cluster -> analyze -> render, in order."""
    import json

    from spectre import analyze as analyze_mod
    from spectre import cluster as cluster_mod
    from spectre import render as render_mod

    conn = dbmod.connect(db)
    sources = ingest_mod.load_sources(config)
    dbmod.sync_sources(conn, sources)
    n_new = ingest_mod.ingest_all(conn, sources)
    dbmod.purge(conn)
    cluster_stats = cluster_mod.run(conn)
    analyze_mod.run(conn)
    stats = render_mod.build_site(conn, SITE_DIR)
    typer.echo(f"Pipeline OK — site généré : {stats}")
    if stats_file:
        stats_file.write_text(json.dumps({"new_articles": n_new, **cluster_stats}))
    conn.close()


@app.command()
def compact(db: Path = typer.Option(DEFAULT_DB)) -> None:
    """Checkpoint WAL, switch to journal_mode=DELETE, VACUUM (pre-commit CI)."""
    dbmod.compact(db)
    typer.echo(f"{db} compactée.")


@app.command(name="check-leaks")
def check_leaks(
    db: Path = typer.Option(DEFAULT_DB),
    site: Path = typer.Option(SITE_DIR),
) -> None:
    """Fail (exit 1) if any RSS summary text leaked into the generated HTML."""
    from spectre import render as render_mod

    conn = dbmod.connect(db)
    leaks = render_mod.find_leaks(conn, site)
    conn.close()
    if leaks:
        typer.echo(f"FUITE de {len(leaks)} chapô(s) dans le HTML public :")
        for s in leaks[:5]:
            typer.echo(f"  - {s[:100]}")
        raise typer.Exit(1)
    typer.echo("Aucune fuite de chapô dans le HTML.")


@app.command()
def serve(port: int = typer.Option(8000, help="Port local")) -> None:
    """Serve site/ locally for development."""
    import functools
    import http.server

    if not SITE_DIR.is_dir():
        typer.echo("site/ n'existe pas encore — lance d'abord `python run.py render`.")
        raise typer.Exit(1)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR)
    )
    typer.echo(f"http://127.0.0.1:{port}")
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    app()
