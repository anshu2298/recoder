"""Ingestion of meetings recorded elsewhere (spec §4.6).

Recoder normally captures a meeting itself. This package covers the other
case: a meeting the user could not record — they were not present, or the
recorder was not running — that a third-party notetaker did capture. The
ingested meeting joins the SAME pipeline as a locally captured one, entering
at ``diarized`` because the transcript already exists.
"""
