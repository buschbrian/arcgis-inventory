"""Typed classification of portal items.

Classification is heuristic. A Web AppBuilder app is identified by a mix of the
portal's ``type``, its typeKeywords, the shape of its item data, and its URL ---
and any of those can be absent or lying. So every verdict carries the signal
that produced it: when the tool tells someone they have 47 Web AppBuilder apps,
they will ask how it knows, and "which signal fired" has to be answerable per
item.

The portal's own ``type`` string is kept verbatim in ``resource.item_type`` and
never overwritten. Esri adds item types; a crawl must not lose information it
did not understand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Classification", "DataStatus", "classify", "is_data_bearing"]

# How the item's data document turned out. Only `error` reduces confidence ---
# an item type that simply has no data document (a code attachment) is not a
# less certain classification, it is a different one.
DataStatus = str  # 'ok' | 'absent' | 'error'

_JS_3X = re.compile(r"js\.arcgis\.com/3\.", re.IGNORECASE)

# Item types whose `/data` document is JSON worth fetching. Anything else --- a
# code attachment's zip, a service definition, an image --- is either binary or
# useless here, and requesting it wastes a round trip against someone's portal.
DATA_BEARING_TYPES = frozenset(
    {
        "Dashboard",
        "Form",
        "StoryMap",
        "Web Experience",
        "Web Map",
        "Web Mapping Application",
        "Web Scene",
        "Notebook",
    }
)

_SIMPLE_TYPES = {
    "Web Map": "web_map",
    "Web Scene": "web_scene",
    "Dashboard": "dashboard",
    "StoryMap": "storymap",
    "Form": "form",
    "Notebook": "notebook",
    "Hub Site Application": "hub_site",
    "Feature Service": "feature_service",
    "Map Service": "map_service",
    "Image Service": "image_service",
    "Geocoding Service": "geocode_service",
    "Geoprocessing Service": "gp_service",
}

_WAB_KEYWORDS = frozenset({"Web AppBuilder", "WAB2D", "WAB3D"})
_EXB_KEYWORDS = frozenset({"ArcGIS Experience Builder", "EXB Experience"})
_INSTANT_KEYWORDS = frozenset({"selfConfigured", "Ready To Use"})


@dataclass(frozen=True, slots=True)
class Classification:
    platform: str
    confidence: str
    evidence: dict[str, Any] = field(default_factory=dict)


def is_data_bearing(item_type: str | None) -> bool:
    return item_type in DATA_BEARING_TYPES


def classify(
    item: dict[str, Any],
    data: Any = None,
    data_status: DataStatus = "absent",
) -> Classification:
    """Return the platform, how sure we are, and why."""
    item_type = item.get("type")
    keywords = set(item.get("typeKeywords") or [])

    if simple := _SIMPLE_TYPES.get(item_type or ""):
        return Classification(simple, "certain", {"item_type": item_type})

    if item_type == "Code Attachment":
        platform = "widget_package" if "Widget" in keywords else "other"
        return Classification(
            platform,
            "likely",
            {"item_type": item_type, "type_keywords": sorted(keywords & {"Widget", "Code"})},
        )

    if item_type == "Web Experience" or keywords & _EXB_KEYWORDS:
        return Classification(
            "experience_builder",
            "certain",
            {"item_type": item_type, "type_keywords": sorted(keywords & _EXB_KEYWORDS)},
        )

    if item_type == "Web Mapping Application":
        return _classify_application(item_type, keywords, data, data_status)

    return Classification("other", "guess", {"item_type": item_type})


def _classify_application(
    item_type: str,
    keywords: set[str],
    data: Any,
    data_status: DataStatus,
) -> Classification:
    """`Web Mapping Application` covers four genuinely different things.

    WAB apps, Instant Apps, and hand-rolled JS 3.x apps all land under this one
    portal type, which is why the data document matters here and nowhere else.
    """
    if isinstance(data, dict) and data_status == "ok":
        if "wabVersion" in data:
            return Classification(
                "web_appbuilder",
                "certain",
                {"item_type": item_type, "data_marker": "wabVersion"},
            )
        if "__esri_exb_version" in data:
            return Classification(
                "experience_builder",
                "certain",
                {"item_type": item_type, "data_marker": "__esri_exb_version"},
            )
        values = data.get("values")
        if isinstance(values, dict) and "webmap" in values:
            return Classification(
                "instant_app",
                "certain",
                {"item_type": item_type, "data_marker": "values.webmap"},
            )
        markers = _legacy_js_markers(data)
        if markers:
            # Only 'likely': these are the fingerprints legacy templates leave
            # behind, but a hand-built app can be built any way at all.
            return Classification(
                "custom_js_app",
                "likely",
                {"item_type": item_type, "data_markers": markers},
            )

    evidence: dict[str, Any] = {"item_type": item_type}
    if data_status == "error":
        # The item data could not be read, so the strongest signal is missing.
        # Say so rather than quietly reporting keyword-level confidence.
        evidence["data_status"] = "error"

    if matched := sorted(keywords & _WAB_KEYWORDS):
        evidence["type_keywords"] = matched
        return Classification(
            "web_appbuilder", "guess" if data_status == "error" else "certain", evidence
        )

    if matched := sorted(keywords & _INSTANT_KEYWORDS):
        evidence["type_keywords"] = matched
        return Classification(
            "instant_app", "guess" if data_status == "error" else "certain", evidence
        )

    return Classification("custom_js_app", "guess", evidence)


def _legacy_js_markers(data: dict[str, Any]) -> list[str]:
    """Signals a retired ArcGIS API for JavaScript 3.x app leaves in its config."""
    markers: list[str] = []
    if "dojoConfig" in data:
        markers.append("dojoConfig")
    for key in ("apiUrl", "jsapi", "scriptUrl"):
        value = data.get(key)
        if isinstance(value, str) and _JS_3X.search(value):
            markers.append(f"{key}:js.arcgis.com/3.x")
    return markers
