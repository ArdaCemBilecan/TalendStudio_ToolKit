"""Configuration (``config.yaml``). Every value has a default, so a config file is optional."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tmt.errors import TmtError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Weights(_Strict):
    """Points added to a job's complexity score (spec 7.2)."""

    component: int = Field(1, ge=0, description="each component")
    tmap_join: int = Field(2, ge=0, description="each tMap lookup join")
    tmap_java_expression: int = Field(3, ge=0, description="each tMap expression that calls Java methods")
    custom_code: int = Field(10, ge=0, description="each tJava/tJavaRow/tJavaFlex or custom routine call")
    unsupported_component: int = Field(5, ge=0, description="each unsupported component")
    run_job_child: int = Field(2, ge=0, description="each tRunJob child")
    unresolved_context: int = Field(2, ge=0, description="each table/SQL target that depends on a context variable")


class Thresholds(_Strict):
    easy_max: int = Field(10, ge=0)
    medium_max: int = Field(25, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if self.medium_max < self.easy_max:
            raise ValueError("medium_max must be >= easy_max")
        return self


class Scoring(_Strict):
    weights: Weights = Field(default_factory=Weights)
    thresholds: Thresholds = Field(default_factory=Thresholds)


class Config(_Strict):
    dialect: str | None = "oracle"  # SQL dialect used to parse queries found in jobs
    scoring: Scoring = Field(default_factory=Scoring)


def load_config(path: Path | None) -> Config:
    """Load ``path`` (or ``./config.yaml`` if it exists, else defaults). Raises :class:`TmtError`."""
    if path is None:
        default = Path("config.yaml")
        if not default.is_file():
            return Config()
        path = default
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TmtError(f"cannot read config {path}: {exc}") from exc
    if raw is None:
        return Config()
    if not isinstance(raw, dict):
        raise TmtError(f"config {path}: top level must be a mapping")
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise TmtError(f"invalid config {path}: {problems}") from exc
