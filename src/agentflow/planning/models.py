"""Immutable, versioned planning contracts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Criterion:
    id: str
    description: str


@dataclass(frozen=True)
class AllowlistedCommand:
    command_id: str
    platform: str
    executable: str
    executable_digest: str
    arguments: tuple[str, ...]
    working_directory: str
    expected_version: str
    container_image: str = ""
    container_executable: str = ""


@dataclass(frozen=True)
class GateDefinition:
    gate_id: str
    version: int
    criterion_ids: tuple[str, ...]
    command_id: str
    depends_on: tuple[str, ...] = ()
    required: bool = True
    timeout_seconds: int = 600
    output_limit_bytes: int = 8 * 1024**2

    def __post_init__(self) -> None:
        if not self.gate_id or not self.command_id or self.version < 1:
            raise ValueError("gate id, command id and positive version are required")
        if not self.criterion_ids:
            raise ValueError("gate must cover at least one criterion")
        if len(set(self.criterion_ids)) != len(self.criterion_ids):
            raise ValueError("duplicate gate criterion")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate gate dependency")
        if self.gate_id in self.depends_on:
            raise ValueError("gate cannot depend on itself")
        if not 1 <= self.timeout_seconds <= 3600:
            raise ValueError("gate timeout must be between 1 and 3600 seconds")
        if not 1 <= self.output_limit_bytes <= 8 * 1024**2:
            raise ValueError("gate output limit must be between 1 byte and 8 MiB")


@dataclass(frozen=True)
class Specification:
    project_id: str
    title: str
    objective: str
    acceptance_criteria: tuple[Criterion, ...]
    validation_commands: tuple[AllowlistedCommand, ...] = ()
    schema_version: int = 1


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    description: str
    acceptance_criteria: tuple[Criterion, ...]
    depends_on: tuple[str, ...] = ()
    versioned_paths: tuple[str, ...] = ()
    gates: tuple[GateDefinition, ...] = ()


@dataclass(frozen=True)
class Plan:
    specification: Specification
    tasks: tuple[Task, ...]
    order: tuple[str, ...]
    spec_digest: str
    tasks_digest: str
    spec_schema_version: int = 1
    tasks_schema_version: int = 1
    gate_order: tuple[str, ...] = ()
