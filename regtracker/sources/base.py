"""Base class every register adapter implements."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import requests


class SourceNotReady(Exception):
    """Raised when a source can't produce a complete list yet (e.g. the FCA
    seed scan is still running). The run skips it without diffing."""


@dataclass
class Context:
    config: dict          # the whole config.yaml
    session: requests.Session
    today: str            # YYYY-MM-DD (UTC)
    data_dir: Path        # repo data folder (state + snapshots)
    excluded_countries: set
    log: logging.Logger

    def source_config(self, name: str) -> dict:
        return (self.config.get("sources") or {}).get(name) or {}


class Source:
    #: short id used in record keys, file names and the --sources flag
    name = "base"
    #: human label for the summary / emails
    label = "Base"
    #: when True, a record that vanishes from the register is reported as
    #: "Removed from register" (list-style sources). Lookup-style sources such
    #: as the FCA API report status changes instead.
    removal_is_event = True

    def fetch(self, ctx: Context) -> list:
        """Return a list of model.Record for the full current population."""
        raise NotImplementedError
