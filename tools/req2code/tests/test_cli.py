"""The command line, driven the way a person drives it.

These tests exist because the CLI is the thing we will actually point at other
people's repositories, and because it is the first consumer of the `--json`
shape the editor integration will later depend on. A format that is only ever
produced, never consumed, drifts; asserting on it here is what stops that.

The embedder is faked, as everywhere else in the offline suite, so what is under
test is the wiring -- argument parsing, exit codes, the JSON contract, and the
error messages a user sees when they ask for something that needs state they do
not have yet. Retrieval *quality* is the ablation's job, not this file's.
"""

from __future__ import annotations

import hashlib
import json
import re

import numpy as np
import pytest

from src.cli import main

DIM = 384


def _fake_embed(texts, model_name=None, cache_name=None, show_progress=False):
    out = np.zeros((len(texts), DIM), dtype=np.float32)
    for row, text in enumerate(texts):
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            out[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % DIM] += 1.0
        norm = np.linalg.norm(out[row])
        if norm > 0:
            out[row] /= norm
    return out


@pytest.fixture
def fake_embedder(monkeypatch):
    monkeypatch.setattr("src.index.embedder.embed_texts", _fake_embed)


SOURCE = '''
"""Traveller notification."""


class AlertDispatcher:
    """Sends alerts to travellers."""

    def send_alert_to_nearby_traveller(self, traveller_name, radius_km):
        """Notify the traveller about attractions located nearby."""
        return True

    def get_font(self):
        return "Arial"
'''


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "notify.py").write_text(SOURCE, encoding="utf-8")
    return root


def run(project, *args, json_out: bool = False) -> int:
    argv = ["--path", str(project)] + (["--json"] if json_out else []) + list(args)
    return main(argv)


def payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


NEARBY = "the system shall notify the traveller about attractions located nearby"


class TestIndex:
    def test_reports_what_it_found(self, project, fake_embedder, capsys):
        assert run(project, "index", json_out=True) == 0
        data = payload(capsys)
        assert data["mode"] == "workspace"
        assert data["nodes"] > 0
        assert data["languages"]["python"] > 0
        # `requirements` is the list itself, not a count: the status panel wants
        # both and one round trip beats two. See src/api.py.
        assert data["requirements"] == []

    def test_human_output_tells_a_new_user_what_to_do_next(
        self, project, fake_embedder, capsys
    ):
        """An empty requirement set is the normal first state; say so usefully."""
        assert run(project, "index") == 0
        assert "state" in capsys.readouterr().out

    def test_missing_directory_exits_nonzero(self, tmp_path, fake_embedder):
        assert main(["--path", str(tmp_path / "nope"), "index"]) == 2


class TestStateAndTrace:
    def test_state_logs_and_traces(self, project, fake_embedder, capsys):
        assert run(project, "state", NEARBY, json_out=True) == 0
        data = payload(capsys)
        assert data["req_id"] == "SR0001"
        assert data["created"] is True
        assert data["hits"][0]["name"] == "send_alert_to_nearby_traveller"
        assert data["hits"][0]["file"] == "app/notify.py"
        assert data["hits"][0]["start_line"] > 0

    def test_restating_reports_it_was_already_logged(
        self, project, fake_embedder, capsys
    ):
        run(project, "state", NEARBY)
        capsys.readouterr()
        assert run(project, "state", NEARBY, json_out=True) == 0
        data = payload(capsys)
        assert data["created"] is False
        assert data["req_id"] == "SR0001"

    def test_requirement_persists_across_invocations(
        self, project, fake_embedder, capsys
    ):
        """Each CLI run is a fresh process; the store is what carries state."""
        run(project, "state", NEARBY)
        capsys.readouterr()
        assert run(project, "reqs", json_out=True) == 0
        assert payload(capsys)["count"] == 1

    def test_trace_by_id(self, project, fake_embedder, capsys):
        run(project, "state", NEARBY)
        capsys.readouterr()
        assert run(project, "trace", "SR0001", json_out=True) == 0
        assert payload(capsys)["hits"][0]["name"] == "send_alert_to_nearby_traveller"

    def test_trace_unknown_id_fails_with_the_known_list(
        self, project, fake_embedder, capsys
    ):
        assert run(project, "trace", "SR9999") == 1
        assert "Unknown requirement" in capsys.readouterr().err

    def test_forget_removes(self, project, fake_embedder, capsys):
        run(project, "state", NEARBY)
        capsys.readouterr()
        assert run(project, "forget", "SR0001") == 0
        capsys.readouterr()
        assert run(project, "reqs", json_out=True) == 0
        assert payload(capsys)["count"] == 0


class TestSearch:
    def test_search_does_not_log(self, project, fake_embedder, capsys):
        """The difference between `search` and `state` is durability, and it
        matters: a throwaway query must not become part of the orphan baseline."""
        assert run(project, "search", NEARBY, json_out=True) == 0
        assert payload(capsys)["hits"][0]["name"] == "send_alert_to_nearby_traveller"
        assert run(project, "reqs", json_out=True) == 0
        assert payload(capsys)["count"] == 0


class TestWhose:
    def test_resolves_a_cursor_to_its_requirement(self, project, fake_embedder, capsys):
        run(project, "state", NEARBY)
        capsys.readouterr()
        target = str(project / "app" / "notify.py")
        assert run(project, "whose", target, "8", json_out=True) == 0
        data = payload(capsys)
        assert data["name"] == "send_alert_to_nearby_traveller"
        assert data["requirements"][0]["req_id"] == "SR0001"

    def test_needs_a_requirement_set(self, project, fake_embedder, capsys):
        code = run(project, "whose", str(project / "app" / "notify.py"), "8")
        assert code == 1
        assert "No requirements" in capsys.readouterr().err

    def test_line_in_no_declaration(self, project, fake_embedder, capsys):
        run(project, "state", NEARBY)
        capsys.readouterr()
        assert run(project, "whose", str(project / "app" / "notify.py"), "1") == 1
        assert "No parsed declaration" in capsys.readouterr().err


class TestApiShapes:
    """The payloads the VS Code extension reads.

    The extension is a separate language in a separate directory with no type
    checking across the boundary, so a renamed field here fails there at
    runtime, in a webview, with no stack trace worth reading. These assertions
    are the only thing standing between a refactor and that.
    """

    def _corpus(self, project):
        from src.pipeline import load_corpus

        return load_corpus(project)

    def test_status_shape(self, project, fake_embedder):
        from src import api

        s = api.status(self._corpus(project))
        assert {"root", "mode", "nodes", "files", "methods", "languages",
                "requirements", "git", "read_only"} <= set(s)
        assert set(s["git"]) == {"tracked", "head", "dirty"}
        assert s["read_only"] is False

    def test_hit_shape(self, project, fake_embedder):
        from src import api
        from src.index.vector_store import search

        corpus = self._corpus(project)
        corpus.add_requirement(NEARBY)
        hits = search(corpus.req_vectors[0], corpus.node_vectors, top_k=1)
        payload = api.trace(corpus, hits, req=corpus.requirements[0], created=True)
        assert {"query", "req_id", "created", "hits"} == set(payload)
        assert {"rank", "score", "node_id", "name", "enclosing", "kind", "file",
                "start_line", "end_line"} <= set(payload["hits"][0])

    def test_file_map_is_one_call_for_the_whole_file(self, project, fake_embedder):
        """CodeLens needs every node in a file at once, not one call per method."""
        from src import api

        corpus = self._corpus(project)
        corpus.add_requirement(NEARBY)
        payload = api.file_map(corpus, "app/notify.py")
        assert payload["file"] == "app/notify.py"
        names = {n["name"] for n in payload["nodes"]}
        assert {"send_alert_to_nearby_traveller", "get_font"} <= names

        by_name = {n["name"]: n for n in payload["nodes"]}
        target = by_name["send_alert_to_nearby_traveller"]
        assert target["requirements"][0]["req_id"] == "SR0001"
        assert target["best_score"] > by_name["get_font"]["best_score"]
        # Source order, because that is the order the editor draws them.
        lines = [n["start_line"] for n in payload["nodes"]]
        assert lines == sorted(lines)

    def test_file_map_without_requirements_is_empty_not_broken(
        self, project, fake_embedder
    ):
        from src import api

        payload = api.file_map(self._corpus(project), "app/notify.py")
        assert payload["nodes"] == []
        assert payload["requirements"] == 0

    def test_forget_drops_the_row_from_the_matrix(self, project, fake_embedder):
        """A zeroed row would still win argmax somewhere; it has to be removed."""
        corpus = self._corpus(project)
        corpus.add_requirement(NEARBY)
        corpus.add_requirement("render the itinerary as a printable document")
        assert corpus.req_vectors.shape[0] == 2

        assert corpus.remove_requirement("SR0001") is True
        assert corpus.req_vectors.shape[0] == 1
        assert [r.req_id for r in corpus.requirements] == ["SR0002"]
        assert corpus.remove_requirement("SR0001") is False


class TestOrphans:
    def test_refuses_without_requirements(self, project, fake_embedder, capsys):
        """Every method is an orphan when nothing has been asked for. Say that,
        rather than printing a list that is technically true and useless."""
        assert run(project, "orphans") == 1
        assert "no requirements stated yet" in capsys.readouterr().err

    def test_flags_the_unclaimed_method(self, project, fake_embedder, capsys):
        run(project, "state", NEARBY)
        capsys.readouterr()
        assert run(project, "orphans", "--threshold", "0.05", json_out=True) == 0
        data = payload(capsys)
        flagged = {o["name"] for o in data["orphans"]}
        assert "get_font" in flagged
        assert "send_alert_to_nearby_traveller" not in flagged
        assert data["distribution"], "the distribution is how a threshold is chosen"
