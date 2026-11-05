"""Immutable, versioned planning contracts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Criterion:
    id: str
    description: str


@dataclass(frozen=True)
class Specification:
    project_id: str
    title: str
    objective: str
    acceptance_criteria: tuple[Criterion, ...]


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    description: str
    acceptance_criteria: tuple[Criterion, ...]
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    specification: Specification
    tasks: tuple[Task, ...]
    order: tuple[str, ...]
    spec_digest: str
    tasks_digest: str
