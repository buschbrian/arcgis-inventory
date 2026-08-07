"""Pull dependencies out of stored item data documents.

Pure functions over JSON: no database, no network. Given an item's data
document and what the item was classified as, return the edges it declares.

Every edge carries a **JSON pointer to where it came from**. That is not
decoration. It makes a dependency auditable --- "this comes from
``/widgetPool/widgets/3/config/sources/0/url``" is answerable, where "this app
uses that layer" is not --- and it is what makes the same layer referenced from
two different widgets two genuine dependencies with different remediation work,
rather than one deduplicated edge.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .urls import normalize_url, service_root

__all__ = ["ExtractedEdge", "extract_edges"]

# Only URLs that look like ArcGIS REST services are dependencies. An Arcade
# expression also mentions the portal URL, and a popup can link anywhere.
_SERVICE_URL = re.compile(r"https?://[^\s\"'<>)]+/rest/services/[^\s\"'<>)]*", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExtractedEdge:
    """One dependency, and where in the config it was declared."""

    relation: str
    source_path: str
    item_id: str | None = None
    url: str | None = None
    layer_index: int | None = None

    # Carried alongside the normalized URL because they are properties of how
    # the dependency was *written*, not of its canonical identity: a service
    # reached over http:// is the same node as over https://, and the fact that
    # somebody wrote http:// is the finding.
    host: str | None = None
    is_https: bool | None = None
    service_type: str | None = None

    def __post_init__(self) -> None:
        if bool(self.item_id) == bool(self.url):
            raise ValueError("an edge targets exactly one of item_id or url")


def extract_edges(platform: str | None, data: Any) -> list[ExtractedEdge]:
    """Return every dependency declared by one item's data document."""
    if not isinstance(data, dict):
        return []
    extractor = _EXTRACTORS.get(platform or "")
    if extractor is None:
        return []
    return list(extractor(data))


# ---------------------------------------------------------------------------
# Web maps and scenes
# ---------------------------------------------------------------------------


def _web_map(data: dict[str, Any]) -> Iterator[ExtractedEdge]:
    yield from _operational_layers(data.get("operationalLayers"), "/operationalLayers")
    yield from _layer_urls(data.get("tables"), "/tables", "table")

    basemap = data.get("baseMap")
    if isinstance(basemap, dict):
        yield from _layer_urls(basemap.get("baseMapLayers"), "/baseMap/baseMapLayers", "basemap")


def _operational_layers(layers: Any, path: str) -> Iterator[ExtractedEdge]:
    """Walk operational layers, descending into group layers to any depth."""
    if not isinstance(layers, list):
        return
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict):
            continue
        here = f"{path}/{i}"

        if isinstance(layer.get("layers"), list):
            yield from _operational_layers(layer["layers"], f"{here}/layers")
            continue

        if edge := _url_edge(layer.get("url"), f"{here}/url", "operational_layer"):
            yield edge

        yield from _arcade(layer.get("popupInfo"), f"{here}/popupInfo")


def _arcade(popup: Any, path: str) -> Iterator[ExtractedEdge]:
    """An Arcade expression can reach into a layer the map never lists.

    These dependencies are invisible to anything that only reads
    `operationalLayers`, and they break exactly as hard when the target moves.
    """
    if not isinstance(popup, dict):
        return
    expressions = popup.get("expressionInfos")
    if not isinstance(expressions, list):
        return
    for j, expression in enumerate(expressions):
        if not isinstance(expression, dict):
            continue
        script = expression.get("expression")
        if not isinstance(script, str):
            continue
        for url in _SERVICE_URL.findall(script):
            yield _make_edge(url, f"{path}/expressionInfos/{j}/expression", "arcade_source")


def _layer_urls(layers: Any, path: str, relation: str) -> Iterator[ExtractedEdge]:
    if not isinstance(layers, list):
        return
    for i, layer in enumerate(layers):
        if not isinstance(layer, dict):
            continue
        if edge := _url_edge(layer.get("url"), f"{path}/{i}/url", relation):
            yield edge


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


def _web_appbuilder(data: dict[str, Any]) -> Iterator[ExtractedEdge]:
    web_map = data.get("map")
    if isinstance(web_map, dict) and isinstance(web_map.get("itemId"), str):
        yield ExtractedEdge("data_source", "/map/itemId", item_id=web_map["itemId"])

    pool = data.get("widgetPool")
    if isinstance(pool, dict):
        yield from _widgets(pool.get("widgets"), "/widgetPool/widgets")
        groups = pool.get("groups")
        if isinstance(groups, list):
            for g, group in enumerate(groups):
                if isinstance(group, dict):
                    yield from _widgets(group.get("widgets"), f"/widgetPool/groups/{g}/widgets")

    if isinstance(data.get("widgetOnScreen"), dict):
        yield from _widgets(data["widgetOnScreen"].get("widgets"), "/widgetOnScreen/widgets")

    geocoder = data.get("geocoder")
    if isinstance(geocoder, dict) and (
        edge := _url_edge(geocoder.get("url"), "/geocoder/url", "geocoder", trim=True)
    ):
        yield edge

    print_task = data.get("printTask")
    if isinstance(print_task, dict) and (
        edge := _url_edge(print_task.get("url"), "/printTask/url", "print_service", trim=True)
    ):
        yield edge

    gp_services = data.get("gpServices")
    if isinstance(gp_services, list):
        for i, service in enumerate(gp_services):
            if isinstance(service, dict) and (
                edge := _url_edge(
                    service.get("url"), f"/gpServices/{i}/url", "gp_service", trim=True
                )
            ):
                yield edge


def _widgets(widgets: Any, path: str) -> Iterator[ExtractedEdge]:
    """Widget configuration is where the surprises live.

    A search widget can point at a layer the app's web map never mentions ---
    frequently a dev or staging service somebody wired in during testing.
    """
    if not isinstance(widgets, list):
        return
    for i, widget in enumerate(widgets):
        if not isinstance(widget, dict):
            continue
        config = widget.get("config")
        if not isinstance(config, dict):
            continue
        sources = config.get("sources")
        if not isinstance(sources, list):
            continue
        for j, source in enumerate(sources):
            if not isinstance(source, dict):
                continue
            if edge := _url_edge(
                source.get("url"), f"{path}/{i}/config/sources/{j}/url", "widget_config"
            ):
                yield edge


def _experience_builder(data: dict[str, Any]) -> Iterator[ExtractedEdge]:
    sources = data.get("dataSources")
    if not isinstance(sources, dict):
        return
    for key in sorted(sources):
        source = sources[key]
        if not isinstance(source, dict):
            continue
        if isinstance(source.get("itemId"), str):
            yield ExtractedEdge(
                "data_source", f"/dataSources/{key}/itemId", item_id=source["itemId"]
            )
        elif edge := _url_edge(source.get("url"), f"/dataSources/{key}/url", "data_source"):
            yield edge


def _dashboard(data: dict[str, Any]) -> Iterator[ExtractedEdge]:
    widgets = data.get("widgets")
    if not isinstance(widgets, list):
        return
    for i, widget in enumerate(widgets):
        if isinstance(widget, dict) and isinstance(widget.get("itemId"), str):
            yield ExtractedEdge("data_source", f"/widgets/{i}/itemId", item_id=widget["itemId"])


def _instant_app(data: dict[str, Any]) -> Iterator[ExtractedEdge]:
    values = data.get("values")
    if isinstance(values, dict) and isinstance(values.get("webmap"), str):
        yield ExtractedEdge("data_source", "/values/webmap", item_id=values["webmap"])


def _storymap(data: dict[str, Any]) -> Iterator[ExtractedEdge]:
    nodes = data.get("nodes")
    if not isinstance(nodes, dict):
        return
    for key in sorted(nodes):
        node = nodes[key]
        if not isinstance(node, dict) or node.get("type") not in ("webmap", "webscene"):
            continue
        node_data = node.get("data")
        if isinstance(node_data, dict) and isinstance(node_data.get("itemId"), str):
            yield ExtractedEdge(
                "data_source", f"/nodes/{key}/data/itemId", item_id=node_data["itemId"]
            )


def _custom_js(data: dict[str, Any]) -> Iterator[ExtractedEdge]:
    """A hand-built JS 3.x app: no schema, so read the config conservatively."""
    yield from _layer_urls(data.get("operationalLayers"), "/operationalLayers", "operational_layer")
    if edge := _url_edge(data.get("locatorUrl"), "/locatorUrl", "geocoder", trim=True):
        yield edge


_EXTRACTORS = {
    "web_map": _web_map,
    "web_scene": _web_map,
    "web_appbuilder": _web_appbuilder,
    "experience_builder": _experience_builder,
    "dashboard": _dashboard,
    "instant_app": _instant_app,
    "storymap": _storymap,
    "custom_js_app": _custom_js,
}


# ---------------------------------------------------------------------------


def _url_edge(
    value: Any, source_path: str, relation: str, *, trim: bool = False
) -> ExtractedEdge | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _make_edge(value, source_path, relation, trim=trim)
    except ValueError:
        # A relative or malformed URL in somebody's config. Not worth failing
        # the whole crawl over; it simply is not a resolvable dependency.
        return None


def _make_edge(value: str, source_path: str, relation: str, *, trim: bool = False) -> ExtractedEdge:
    normalized = service_root(value) if trim else normalize_url(value)
    return ExtractedEdge(
        relation=relation,
        source_path=source_path,
        url=normalized.url,
        layer_index=normalized.layer_index,
        host=normalized.host,
        is_https=normalized.is_https,
        service_type=normalized.service_type,
    )
