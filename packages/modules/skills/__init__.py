"""Shared capabilities agents use (CLAUDE.md section 3).

`reading` lives here as a sub-part rather than as a standalone module.
"""

from packages.modules.skills.reading import Reader, ReadingError

__all__ = ["Reader", "ReadingError"]
