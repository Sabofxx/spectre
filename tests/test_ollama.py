"""OllamaAnalyzer with mocked HTTP (httpx.MockTransport — no extra dep)."""

import json

import httpx
import pytest

from spectre import db as dbmod
from spectre.analyze import compute_ollama
from spectre.analyzers import ClusterData, OllamaAnalyzer
from spectre.cluster import cluster_pending

from test_cluster import put_article

VALID = {
    "event_summary": "Un incendie a parcouru 3 500 hectares dans la Drôme.",
    "framing": {"gauche": "insiste sur la gestion de crise", "centre": None,
                "droite": "insiste sur les moyens engagés"},
    "omissions": "la droite ne mentionne pas les évacuations chaotiques",
}

TAGS_OK = {"models": [{"name": "qwen2.5:7b-instruct"}, {"name": "llama3.2:latest"}]}


def analyzer_with(handler, model: str = "qwen2.5:7b-instruct") -> OllamaAnalyzer:
    client = httpx.Client(
        base_url="http://localhost:11434", transport=httpx.MockTransport(handler)
    )
    return OllamaAnalyzer(model=model, client=client)


def chat_response(content) -> httpx.Response:
    body = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"message": {"content": body}})


def seed_cluster(conn, n_left: int = 2, n_right: int = 2) -> tuple[int, list[int]]:
    """A 4-article, 2-bloc cluster in the window; returns (cluster_id, ids)."""
    ids = []
    for i in range(n_left + n_right):
        aid = put_article(conn, [1, 0.001 * i, 0], hours_ago=5 - i,
                          title=f"Titre {i}")
        source = "g1" if i < n_left else "d1"
        conn.execute(
            "UPDATE articles SET source_id = ?, summary = ? WHERE id = ?",
            (source, f"Chapô numéro {i} avec du contexte.", aid),
        )
        ids.append(aid)
    cluster_pending(conn, threshold=0.7)
    cluster_id = conn.execute("SELECT id FROM clusters WHERE n_members >= 4").fetchone()[0]
    return cluster_id, ids


def stored_payload(conn, cluster_id: int) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM analyses WHERE cluster_id = ? AND kind = 'ollama'",
        (cluster_id,),
    ).fetchone()
    return json.loads(row[0]) if row else None


class TestDetection:
    def test_server_down_disables_silently(self, conn):
        def handler(request):
            raise httpx.ConnectError("refused")

        analyzer = analyzer_with(handler)
        assert analyzer.available() is False
        stats = compute_ollama(conn, analyzer=analyzer)
        assert stats.get("unavailable") is True
        assert stats["analyzed"] == 0

    def test_missing_model_disables_with_pull_hint(self, conn, caplog):
        def handler(request):
            return httpx.Response(200, json={"models": [{"name": "autre:1b"}]})

        analyzer = analyzer_with(handler)
        with caplog.at_level("INFO"):
            assert analyzer.available() is False
        assert "ollama pull qwen2.5:7b-instruct" in caplog.text

    def test_untagged_model_name_matches_any_tag(self):
        def handler(request):
            return httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}]})

        assert analyzer_with(handler, model="llama3.2").available() is True

    def test_probe_happens_once(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=TAGS_OK)

        analyzer = analyzer_with(handler)
        assert analyzer.available() and analyzer.available()
        assert calls == ["/api/tags"]


class TestAnalyze:
    def test_valid_response_is_stored_with_ids_and_model(self, conn):
        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            return chat_response(VALID)

        cluster_id, ids = seed_cluster(conn)
        stats = compute_ollama(conn, analyzer=analyzer_with(handler))

        assert stats == {"analyzed": 1, "skipped_cache": 0, "skipped_size": 0, "invalid": 0}
        payload = stored_payload(conn, cluster_id)
        assert payload["event_summary"] == VALID["event_summary"]
        assert payload["framing"]["centre"] is None
        assert payload["article_ids"] == sorted(ids)
        assert payload["model"] == "qwen2.5:7b-instruct"

    def test_invalid_json_twice_stores_nothing(self, conn):
        chat_calls = []

        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            chat_calls.append(1)
            return chat_response("pas du json {{{")

        cluster_id, _ = seed_cluster(conn)
        stats = compute_ollama(conn, analyzer=analyzer_with(handler))

        assert stats == {"analyzed": 0, "skipped_cache": 0, "skipped_size": 0, "invalid": 1}
        assert len(chat_calls) == 2  # exactly one retry
        assert stored_payload(conn, cluster_id) is None

    def test_retry_error_is_fed_back_then_success(self, conn):
        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            messages = json.loads(request.content)["messages"]
            if len(messages) == 2:
                return chat_response("broken")
            assert "invalide" in messages[-1]["content"]
            return chat_response(VALID)

        cluster_id, _ = seed_cluster(conn)
        stats = compute_ollama(conn, analyzer=analyzer_with(handler))
        assert stats["analyzed"] == 1
        assert stored_payload(conn, cluster_id) is not None

    def test_null_strings_are_normalized_to_none(self, conn):
        """Small local models emit the STRING "null" — must become real null."""
        sloppy = {**VALID,
                  "framing": {"gauche": "un angle", "centre": "null", "droite": "None"},
                  "omissions": "null"}

        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            return chat_response(sloppy)

        cluster_id, _ = seed_cluster(conn)
        compute_ollama(conn, analyzer=analyzer_with(handler))
        payload = stored_payload(conn, cluster_id)
        assert payload["framing"]["centre"] is None
        assert payload["framing"]["droite"] is None
        assert payload["omissions"] is None

    @pytest.mark.parametrize("bad", [
        {"framing": VALID["framing"], "omissions": None},          # missing key
        {**VALID, "event_summary": "x" * 301},                      # too long
        {**VALID, "framing": {"gauche": "x" * 501, "centre": None, "droite": None}},
        {**VALID, "framing": "pas un objet"},
        {**VALID, "event_summary": 42},
    ])
    def test_schema_violations_are_rejected(self, conn, bad):
        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            return chat_response(bad)

        cluster_id, _ = seed_cluster(conn)
        stats = compute_ollama(conn, analyzer=analyzer_with(handler))
        assert stats["invalid"] == 1
        assert stored_payload(conn, cluster_id) is None


class TestGuards:
    def test_megacluster_is_skipped(self, conn):
        """Clusters > 15 articles never reach the model (verbatim risk)."""
        chat_calls = []

        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            chat_calls.append(1)
            return chat_response(VALID)

        seed_cluster(conn, n_left=8, n_right=8)  # 16 articles
        stats = compute_ollama(conn, analyzer=analyzer_with(handler))

        assert stats == {"analyzed": 0, "skipped_cache": 0, "skipped_size": 1, "invalid": 0}
        assert chat_calls == []

    def test_verbatim_payload_rejected(self, conn):
        """>= 8 consecutive words copied from a member title => invalid."""
        long_title = ("Le gouvernement annonce un plan massif de rénovation "
                      "des écoles rurales dès la rentrée")
        copying = {**VALID, "event_summary":
                   "Selon la presse, le gouvernement annonce un plan massif "
                   "de rénovation des écoles rurales."}

        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            return chat_response(copying)

        cluster_id, ids = seed_cluster(conn)
        conn.execute("UPDATE articles SET title = ? WHERE id = ?", (long_title, ids[0]))
        conn.commit()
        stats = compute_ollama(conn, analyzer=analyzer_with(handler))

        assert stats["invalid"] == 1
        assert stored_payload(conn, cluster_id) is None

    def test_short_quote_is_allowed(self):
        """Fewer than 8 consecutive title words is fine (quoting a phrase)."""
        payload = {"event_summary": "Le plan massif de rénovation divise la presse.",
                   "framing": {"gauche": None, "centre": None, "droite": None},
                   "omissions": None}
        titles = ["Le gouvernement annonce un plan massif de rénovation des écoles rurales"]
        assert OllamaAnalyzer._verbatim_hit(payload, titles) is False


class TestCache:
    def test_same_composition_skips(self, conn):
        chat_calls = []

        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            chat_calls.append(1)
            return chat_response(VALID)

        seed_cluster(conn)
        first = compute_ollama(conn, analyzer=analyzer_with(handler))
        second = compute_ollama(conn, analyzer=analyzer_with(handler))

        assert first["analyzed"] == 1
        assert second == {"analyzed": 0, "skipped_cache": 1, "skipped_size": 0, "invalid": 0}
        assert len(chat_calls) == 1

    def test_composition_changed_by_two_reanalyzes(self, conn):
        def handler(request):
            if request.url.path == "/api/tags":
                return httpx.Response(200, json=TAGS_OK)
            return chat_response(VALID)

        cluster_id, _ = seed_cluster(conn)
        compute_ollama(conn, analyzer=analyzer_with(handler))

        # Two more articles join the cluster.
        for i in range(2):
            aid = put_article(conn, [1, 0.002 + 0.001 * i, 0], hours_ago=0.5,
                              title=f"Nouveau {i}")
            conn.execute("UPDATE articles SET source_id = 'cd1' WHERE id = ?", (aid,))
        cluster_pending(conn, threshold=0.7)
        assert conn.execute(
            "SELECT n_members FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()[0] == 6

        stats = compute_ollama(conn, analyzer=analyzer_with(handler))
        assert stats == {"analyzed": 1, "skipped_cache": 0, "skipped_size": 0, "invalid": 0}
        assert len(stored_payload(conn, cluster_id)["article_ids"]) == 6
