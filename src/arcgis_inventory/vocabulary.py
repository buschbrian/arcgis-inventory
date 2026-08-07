"""Closed vocabularies used across the schema.

These are *derived* values. The portal's own ``type`` string is kept verbatim in
``resource.item_type`` and never overwritten --- Esri adds item types, and a
crawl must not lose information it did not understand.
"""

from __future__ import annotations

from typing import Final, Literal

__all__ = [
    "CATEGORIES",
    "CONFIDENCES",
    "FINDING_STATUSES",
    "MIGRATION_STATUSES",
    "PLATFORMS",
    "RELATIONS",
    "SEVERITIES",
    "TARGETS",
]

Confidence = Literal["certain", "likely", "guess"]

PLATFORMS: Final[tuple[str, ...]] = (
    "web_appbuilder",
    "experience_builder",
    "dashboard",
    "instant_app",
    "storymap",
    "hub_site",
    "web_map",
    "web_scene",
    "feature_service",
    "map_service",
    "image_service",
    "geocode_service",
    "gp_service",
    "print_service",
    "custom_js_app",
    "widget_package",
    "form",
    "notebook",
    "other",
)

# Classification is heuristic --- a WAB app is identified by a mix of `type`,
# typeKeywords, the shape of its data JSON, and its URL, and any of those can be
# absent. When the tool says an org has 47 Web AppBuilder apps, someone will ask
# how it knows, and "which signal fired" has to be answerable per item.
CONFIDENCES: Final[tuple[str, ...]] = ("certain", "likely", "guess")

RELATIONS: Final[tuple[str, ...]] = (
    "operational_layer",
    "basemap",
    "table",
    "geocoder",
    "gp_service",
    "print_service",
    "elevation_service",
    "widget_config",
    "arcade_source",
    "attachment_source",
    "linked_item",
    "embedded_app",
    "data_source",
)

CATEGORIES: Final[tuple[str, ...]] = (
    "deprecated_tech",
    "sharing",
    "ownership",
    "hygiene",
    "reachability",
)

SEVERITIES: Final[tuple[str, ...]] = ("critical", "high", "medium", "low", "info")

FINDING_STATUSES: Final[tuple[str, ...]] = ("open", "acknowledged", "wontfix", "fixed")

# Biased toward Instant Apps for simple single-map apps rather than defaulting
# everything to Experience Builder --- which is also Esri's own guidance.
TARGETS: Final[tuple[str, ...]] = (
    "retire",
    "instant_app",
    "experience_builder",
    "custom",
    "keep",
    "unknown",
)

MIGRATION_STATUSES: Final[tuple[str, ...]] = (
    "not_started",
    "in_progress",
    "built",
    "validated",
    "cutover",
    "retired",
    "blocked",
)
