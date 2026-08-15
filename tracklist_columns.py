"""
tracklist_columns.py — Shared column-name detection for tracklist / metadata
sheets.

Single source of truth for the header-matching that several modules need.
`_normalize` and `_find_column` (and the candidate-name lists) were previously
copy-pasted across covers.py, verification.py, final_metadata_verification.py,
and remediation.py — and had already started to drift.  Centralizing them here
means a new column-name variant is a one-place edit and the matchers can't
diverge.

Matching is normalization-based: header and candidate are both upper-cased with
spaces and underscores removed, so "Album Cover Art", "album_cover_art", and
"ALBUMCOVERART" all collapse to the same key.  That's why candidate lists below
omit spacing variants (they'd be redundant after normalization).
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "_normalize",
    "_find_column",
    "POSSIBLE_LABEL_COLS",
    "POSSIBLE_ALBUMNO_COLS",
    "POSSIBLE_ALBUMNOMASTERS_COLS",
    "POSSIBLE_ALBUMTITLE_COLS",
    "POSSIBLE_FILENAME_COLS",
    "POSSIBLE_EXTERNAL_ID_COLS",
    "POSSIBLE_COVER_COLS",
    "POSSIBLE_WORKID_COLS",
    "POSSIBLE_URL_COLS",
]


# ---------------------------------------------------------------------------
# Candidate header names (normalized — no spaces/underscores, upper-case)
# ---------------------------------------------------------------------------

POSSIBLE_LABEL_COLS = [
    "LABEL",
    "LABELDESCRIPTION",
    "LABELNAME",
    "LIBRARYLABELNAME",
    "LIBRARYNAME",
    "LIBRARY",
    "CATALOG",
]
POSSIBLE_ALBUMNO_COLS = [
    "ALBUMNO",
    "ALBUMCODE",
    "ALBUMCATALOGUENUMBER",
    "CATALOGUENUMBER",
    "CATALOGNUMBER",       # Japan NTT uses "CATALOG NUMBER"
    "CATNODISPLAY",
    "CATNO",
    "ALBUMID",
]
POSSIBLE_ALBUMNOMASTERS_COLS = [
    "ALBUMNOMASTERS",
    "ALBUMNUMBERMASTERS",
    "MASTERSALBUMNO",
]
POSSIBLE_ALBUMTITLE_COLS = [
    "ALBUMTITLE",
    "CDTITLE",
    "GROUPINGNAME",
    "TITLE",
]
POSSIBLE_FILENAME_COLS = [
    "FILENAME",
    "AUDIOFILENAME",
]
POSSIBLE_EXTERNAL_ID_COLS = [
    "EXTERNALID",
    "WORKAUDIOID",
    "AUDIOID",
    "WORKID",
]
POSSIBLE_COVER_COLS = [
    "ALBUMCOVERART",
    "CDCOVER",
    "ALBUMART",
    "ALBUMCOVER",
    "CDARTWORK",
    "ARTWORK",
    "ALBUMIMAGES",
    "COVER",          # very generic — last resort
]
POSSIBLE_WORKID_COLS = [
    "WORKAUDIOID",
    "AUDIOID",
    "WORKID",
]
POSSIBLE_URL_COLS = [
    "ALBUMCOVERCDN",
    "CDNALBUMART",
    "CDNALBUMCOVER",
    "CDNCOVERART",
    "CDNALBUMIMAGES",
    "ALBUMCOVERURL",
    "COVERARTURL",
    "COVERCDN",
]


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------

def _normalize(name: str) -> str:
    """Uppercase, strip whitespace and underscores — for column matching."""
    return name.upper().replace(" ", "").replace("_", "").strip()


def _find_column(columns: list[str], candidates: list[str]) -> Optional[str]:
    """
    Return the first column in `columns` matching any candidate.

    Two-pass:
      1. Exact normalized match — handles the common case where the CSV
         column header already matches one of our well-known names
         (e.g. "AlbumCoverArt" → "ALBUMCOVERART").  Exact match wins
         even if a substring fallback would otherwise pick a different
         column.
      2. Substring containment — handles odd export variants like
         "Album_Cover_Art_v2".
    """
    normalized = {_normalize(c): c for c in columns}

    for cand in candidates:
        target = _normalize(cand)
        if target in normalized:
            return normalized[target]

    for cand in candidates:
        target = _normalize(cand)
        for norm_name, original in normalized.items():
            if target in norm_name:
                return original

    return None
