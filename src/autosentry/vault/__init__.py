"""Obsidian-compatible markdown vault.

Writes human-readable summaries of supervisor events to
``.autosentry/vault/`` as wikilinked markdown notes. The vault is
*derived* from existing on-disk data (incidents, attempts ledger,
state) — never the source of truth.

Public surface:

- :class:`VaultStore` — writer; wired into ``Monitor``.
- :class:`NoteRef` — a lightweight handle to a vault note (path +
  wikilink form).

The pattern-detection and significant-event logic lives in
:mod:`autosentry.vault.patterns`; the templates in
:mod:`autosentry.vault.templates`.
"""

from autosentry.vault.store import NoteRef, VaultStore

__all__ = ["NoteRef", "VaultStore"]
