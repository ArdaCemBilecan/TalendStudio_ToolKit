from __future__ import annotations

from pathlib import Path

import pytest

from tmt.config import Config, load_config
from tmt.errors import TmtError

ROOT = Path(__file__).resolve().parents[2]


def test_defaults_match_the_spec() -> None:
    w = Config().scoring.weights
    assert (w.component, w.tmap_join, w.tmap_java_expression, w.custom_code) == (1, 2, 3, 10)
    assert (w.unsupported_component, w.run_job_child, w.unresolved_context) == (5, 2, 2)
    t = Config().scoring.thresholds
    assert (t.easy_max, t.medium_max) == (10, 25)


def test_example_config_equals_defaults() -> None:
    assert load_config(ROOT / "config.example.yaml") == Config()


def test_partial_config_overrides_only_what_it_names(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("scoring:\n  weights:\n    tmap_join: 7\n", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.scoring.weights.tmap_join == 7 and cfg.scoring.weights.component == 1


def test_no_file_gives_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_config(None) == Config()


def test_config_yaml_in_cwd_is_picked_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.yaml").write_text("dialect: postgres\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_config(None).dialect == "postgres"


def test_empty_file_gives_defaults(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("", encoding="utf-8")
    assert load_config(p) == Config()


@pytest.mark.parametrize(
    "text",
    [
        "scoring:\n  weights:\n    compnent: 1\n",  # typo
        "scoring:\n  weights:\n    component: -1\n",  # negative
        "scoring:\n  thresholds:\n    easy_max: 30\n    medium_max: 20\n",  # unordered
        "- a\n- b\n",  # not a mapping
        "scoring: [unclosed\n",  # not yaml
        "bogus: 1\n",  # unknown top-level key
    ],
)
def test_invalid_config_is_a_clean_error(tmp_path: Path, text: str) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(TmtError):
        load_config(p)


def test_missing_explicit_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(TmtError):
        load_config(tmp_path / "nope.yaml")
