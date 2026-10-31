"""Pack errors. Messages never contain secrets."""
from __future__ import annotations


class PackError(Exception):
    """A pack (or one of its expressions, statements, argv lists or requests) is invalid or refused."""
