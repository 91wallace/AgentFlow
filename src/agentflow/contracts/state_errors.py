"""State operation failures shared by repositories and their consumers."""


class StateError(ValueError):
    """Rejected state operation."""


class IntegrityError(StateError):
    """Stored state is incompatible or damaged."""


class ConflictError(StateError):
    """Operation ID, revision, or writer conflict."""


class TransitionError(StateError):
    """Invalid transition or unmet guard."""
