"""Deterministic repairs applied before an output is judged.

Empty by design. The planning autofixer lived here and repaired plans by snapping a
ref's file and line range onto the nearest indexed candidate. Plans now cite opaque
evidence ids, so a wrong ref cannot be nudged into a right one - guessing which id a
planner meant would be inventing evidence, which is the thing the boundary exists to
prevent (CLAUDE.md section 2).
"""

__all__: list[str] = []
