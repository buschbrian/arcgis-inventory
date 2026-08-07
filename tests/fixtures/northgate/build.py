#!/usr/bin/env python3
"""Expand ``spec.yaml`` into the portal-shaped JSON tree beside it.

Run it after editing the spec::

    python tests/fixtures/northgate/build.py

Both the spec and the generated output are committed. Tests read the generated
JSON, so a fixture change shows up as a legible diff in review; the generator
exists so that adding a thirty-first item is not four hand-written files and a
paginated search response. CI re-runs this and fails if the committed output
has drifted.

Determinism is a hard requirement: same spec in, byte-identical tree out. No
timestamps, no ``uuid4``, no dict iteration that depends on insertion luck.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# The transport is the consumer of this tree, so it owns the naming rule. Two
# copies of it would drift, and the failure would look like a missing fixture.
from arcgis_inventory.transport import fixture_service_filename

ROOT = Path(__file__).parent
SPEC = ROOT / "spec.yaml"

# The fixture is generated for a page size of 10 so that pagination is actually
# exercised: 30 items become 3 pages. Real crawls use 100.
PAGE_SIZE = 10

# Anything the fixture generates lives in these directories and nowhere else,
# so the build can wipe them without touching spec.yaml or build.py.
GENERATED = ("portal", "search", "items", "services", "expected", "run2")

# Stock Web AppBuilder widgets. A widget URI outside this set is a custom widget
# package, which is what makes an app a rewrite rather than a reconfigure.
STOCK_WIDGETS = frozenset(
    {
        "AttributeTable",
        "BasemapGallery",
        "Bookmark",
        "Coordinate",
        "Draw",
        "Edit",
        "Geoprocessing",
        "HomeButton",
        "LayerList",
        "Legend",
        "MyLocation",
        "NearMe",
        "Overview",
        "Print",
        "Query",
        "Scalebar",
        "Search",
        "Select",
        "Share",
        "Splash",
        "ZoomSlider",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def item_id(n: int) -> str:
    """Deterministic, obviously fake: a0000000000000000000000000000001."""
    return f"a{n:031d}"


def folder_id(index: int) -> str:
    return f"d{index:031d}"


def epoch_ms(value: str | None) -> int | None:
    if value is None:
        return None
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False)
    # newline="" so Windows does not translate to CRLF: this output is
    # committed, and the drift check has to compare equal across platforms.
    path.write_text(text + "\n", encoding="utf-8", newline="")


def service_file(host: str, url_path: str) -> Path:
    return ROOT / "services" / host / fixture_service_filename(url_path)


class Org:
    """Resolves service keys to URLs, and item keys to ids."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.org = spec["org"]
        self.services = {s["key"]: s for s in spec["services"]}
        self.items = {i["key"]: i for i in spec["items"]}
        for extra in spec.get("run2", {}).get("added", []):
            self.items[extra["key"]] = extra

        folders = sorted(
            {i["folder"] for i in self.items.values() if i.get("folder")},
        )
        self.folders = {name: folder_id(i + 1) for i, name in enumerate(folders)}

    # -- services ----------------------------------------------------------

    def service_url(self, key: str) -> str:
        svc = self.services[key]
        scheme = svc.get("scheme", "https")
        host = svc.get("host", self.org["services_host"])
        # The spec folds the long path across lines; collapse all whitespace.
        path = "".join(str(svc["path"]).split())
        return f"{scheme}://{host}/server/rest/services/{path}"

    def layer_url(self, key: str, layer: int) -> str:
        return f"{self.service_url(key)}/{layer}"

    def service_layers(self, key: str) -> list[dict[str, Any]]:
        return list(self.services[key].get("layers") or [])

    # -- items -------------------------------------------------------------

    def id_of(self, key: str) -> str:
        return item_id(self.items[key]["n"])

    def app_url(self, item: dict[str, Any]) -> str | None:
        base = self.org["portal_url"].rsplit("/", 1)[0]
        kind = (item.get("data") or {}).get("kind")
        platform = item.get("platform")
        if platform == "web_appbuilder" or kind in ("malformed", "forbidden"):
            return f"{base}/apps/webappviewer/index.html?id={item_id(item['n'])}"
        if platform == "experience_builder":
            return f"{base}/apps/experiencebuilder/experience/?id={item_id(item['n'])}"
        if platform == "instant_app":
            return f"{base}/apps/instant/nearby/index.html?appid={item_id(item['n'])}"
        if platform == "dashboard":
            return f"{base}/apps/dashboards/{item_id(item['n'])}"
        if platform == "storymap":
            return f"{base}/apps/storymaps/stories/{item_id(item['n'])}"
        if platform == "custom_js_app":
            return f"{base}/apps/precinct-finder/"
        return None


# ---------------------------------------------------------------------------
# Item descriptions
# ---------------------------------------------------------------------------


def build_item(org: Org, item: dict[str, Any], *, num_views: int | None = None) -> dict[str, Any]:
    tags = item.get("tags")
    if tags == 200:
        # An item with 200 tags. Somebody's bulk-tagging script did this once
        # and every exporter downstream has to survive it.
        tags = [f"tag-{i:03d}" for i in range(200)]
    elif tags is None:
        tags = []

    views = item.get("numViews") if num_views is None else num_views

    return {
        "id": item_id(item["n"]),
        "owner": item["owner"],
        "created": epoch_ms(item.get("created")),
        "modified": epoch_ms(item.get("modified")),
        "title": item["title"],
        "type": item["type"],
        "typeKeywords": list(item.get("typeKeywords") or []),
        "description": item.get("snippet"),
        "tags": tags,
        "snippet": item.get("snippet"),
        "url": org.app_url(item),
        "access": item["access"],
        "size": 4096 + item["n"] * 137,
        "numViews": views,
        "ownerFolder": org.folders.get(item.get("folder")),
        "protected": False,
        "spatialReference": "102671",
        "culture": "en-us",
    }


# ---------------------------------------------------------------------------
# Item data builders. Each returns (data_json, edges).
#
# An edge is (to_url_or_item_key, relation, source_path, layer_index). Deriving
# them here is how the fixture declares its expected dependency graph: the crawl
# has to reproduce exactly this.
# ---------------------------------------------------------------------------

Edge = tuple[str, str, str, int | None]


def _operational_layer(
    org: Org, key: str, layer: dict[str, Any], index: int, *, with_item_id: bool
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": f"layer_{index}",
        "layerType": "ArcGISFeatureLayer",
        "url": org.layer_url(key, layer["id"]),
        "visibility": True,
        "opacity": 1,
        "title": layer["name"],
    }
    if with_item_id:
        node["itemId"] = None
    return node


def build_webmap(org: Org, item: dict[str, Any]) -> tuple[dict[str, Any], list[Edge]]:
    data = item["data"]
    layers: list[dict[str, Any]] = []
    edges: list[Edge] = []

    for key in data.get("operational", []):
        for layer in org.service_layers(key):
            i = len(layers)
            layers.append(_operational_layer(org, key, layer, i, with_item_id=False))
            edges.append(
                (
                    org.service_url(key),
                    "operational_layer",
                    f"/operationalLayers/{i}/url",
                    layer["id"],
                )
            )

    # Case 18: referenced by URL with no itemId at all. This is the shape that
    # forces an endpoint node with no item_id.
    for key in data.get("operational_by_url", []):
        for layer in org.service_layers(key):
            i = len(layers)
            node = _operational_layer(org, key, layer, i, with_item_id=False)
            node.pop("itemId", None)
            layers.append(node)
            edges.append(
                (
                    org.service_url(key),
                    "operational_layer",
                    f"/operationalLayers/{i}/url",
                    layer["id"],
                )
            )

    # Case 22: the same service at two indexes, and one index twice. One
    # endpoint node; three edges, distinguished by source_path.
    for entry in data.get("operational_repeated", []):
        key, layer_index = entry["service"], entry["layer"]
        i = len(layers)
        layers.append(
            {
                "id": f"layer_{i}",
                "layerType": "ArcGISFeatureLayer",
                "url": org.layer_url(key, layer_index),
                "visibility": True,
                "opacity": 1,
                "title": f"{org.services[key]['layers'][layer_index]['name']} ({i})",
            }
        )
        edges.append(
            (org.service_url(key), "operational_layer", f"/operationalLayers/{i}/url", layer_index)
        )

    # Case 19: group layers nested four deep.
    if "group_layers" in data:
        group_layers, group_edges = _build_groups(
            org, data["group_layers"], "/operationalLayers", len(layers)
        )
        layers.extend(group_layers)
        edges.extend(group_edges)

    # Case 21: an Arcade expression reaching into another layer.
    if layers and data.get("arcade_sources"):
        expressions = []
        for j, key in enumerate(data["arcade_sources"]):
            url = org.service_url(key)
            expressions.append(
                {
                    "name": f"expr{j}",
                    "title": "Nearest street",
                    "expression": (
                        f'var streets = FeatureSetByPortalItem(Portal("{org.org["portal_url"]}"), '
                        f'"{url}", 0); First(streets).ST_NAME'
                    ),
                    "returnType": "string",
                }
            )
            edges.append(
                (
                    url,
                    "arcade_source",
                    f"/operationalLayers/0/popupInfo/expressionInfos/{j}/expression",
                    None,
                )
            )
        layers[0]["popupInfo"] = {"title": "{OBJECTID}", "expressionInfos": expressions}

    basemap_layers = []
    if key := data.get("basemap"):
        url = org.service_url(key)
        basemap_layers.append(
            {
                "id": "basemap_0",
                "layerType": "ArcGISTiledMapServiceLayer",
                "url": url,
                "title": "County Basemap",
            }
        )
        edges.append((url, "basemap", "/baseMap/baseMapLayers/0/url", None))

    payload = {
        "operationalLayers": layers,
        "baseMap": {
            "baseMapLayers": basemap_layers
            or [
                {
                    "id": "basemap_default",
                    "layerType": "VectorTileLayer",
                    "styleUrl": "https://basemaps.external-vendor.example.com/styles/topo.json",
                    "title": "Topographic",
                }
            ],
            "title": "Topographic",
        },
        "spatialReference": {"wkid": 102671, "latestWkid": 3435},
        "version": "2.31",
        "authoringApp": "ArcGISMapViewer",
        "authoringAppVersion": "2026.1",
    }
    return payload, edges


def _build_groups(
    org: Org,
    node: dict[str, Any] | list[Any],
    path: str,
    offset: int = 0,
    depth: int = 0,
) -> tuple[list[dict[str, Any]], list[Edge]]:
    """Expand the nested group-layer dict from the spec.

    Returns a *list* of sibling layers so the caller extends rather than
    appends --- collapsing a single root group into the parent is exactly the
    kind of special case that makes a recorded source_path disagree with the
    JSON it claims to point at.

    ``path`` is the JSON pointer to the array these siblings live in;
    ``offset`` is the index the first sibling lands at.
    """
    edges: list[Edge] = []
    layers: list[dict[str, Any]] = []

    if isinstance(node, list):  # leaf: a list of service keys
        for key in node:
            for layer in org.service_layers(key):
                i = offset + len(layers)
                layers.append(
                    {
                        "id": f"grp_{depth}_{i}",
                        "layerType": "ArcGISFeatureLayer",
                        "url": org.layer_url(key, layer["id"]),
                        "title": layer["name"],
                    }
                )
                edges.append(
                    (org.service_url(key), "operational_layer", f"{path}/{i}/url", layer["id"])
                )
        return layers, edges

    # A dict is one or more named groups at this level.
    for name, child in node.items():
        i = offset + len(layers)
        children, child_edges = _build_groups(org, child, f"{path}/{i}/layers", 0, depth + 1)
        layers.append(
            {
                "id": f"group_{depth}_{i}",
                "layerType": "GroupLayer",
                "title": name,
                "visibilityMode": "independent",
                "layers": children,
            }
        )
        edges.extend(child_edges)
    return layers, edges


def build_wab(org: Org, item: dict[str, Any]) -> tuple[dict[str, Any], list[Edge]]:
    data = item["data"]
    edges: list[Edge] = [(data["map"], "data_source", "/map/itemId", None)]

    widgets = []
    for i, name in enumerate(data.get("widgets", [])):
        widget: dict[str, Any] = {
            "uri": f"widgets/{name}/Widget",
            "id": f"widgets_{name}_Widget_{i}",
            "version": "2.29",
            "label": name,
            "config": {},
        }
        widgets.append(widget)

    for name in data.get("custom_widgets", []):
        widgets.append(
            {
                "uri": f"widgets/{name}/Widget",
                "id": f"widgets_{name}_Widget_{len(widgets)}",
                "version": "1.0",
                "label": name,
                "config": {},
                "isThirdParty": True,
            }
        )

    # Case 6: a widget config pointing at the dev host. The edge's source_path
    # is what makes this auditable rather than a mystery.
    for key in data.get("extra_layers", []):
        target = next((w for w in widgets if w["uri"].endswith("Search/Widget")), None)
        if target is None:
            target = widgets[0]
        index = widgets.index(target)
        url = org.service_url(key)
        target["config"] = {
            "sources": [
                {"url": f"{url}/0", "name": "Locate Requests", "searchFields": ["TICKET_NO"]}
            ]
        }
        edges.append((url, "widget_config", f"/widgetPool/widgets/{index}/config/sources/0/url", 0))

    payload: dict[str, Any] = {
        "appId": item_id(item["n"]),
        "wabVersion": "2.29",
        "title": item["title"],
        "portalUrl": org.org["portal_url"],
        "theme": {"name": "FoldableTheme", "styles": ["default"], "version": "2.29"},
        "map": {
            "3D": False,
            "2D": True,
            "itemId": org.id_of(data["map"]),
            "mapOptions": {},
            "appProxy": {"mapItemId": org.id_of(data["map"])},
        },
        "widgetOnScreen": {"widgets": []},
        "widgetPool": {"widgets": widgets},
    }

    # WAB's own multi-page layout is widget groups, not a page count.
    if pages := data.get("pages"):
        payload["widgetPool"] = {
            "groups": [
                {"label": f"Group {g + 1}", "widgets": widgets[g::pages]} for g in range(pages)
            ]
        }
        # Regenerate the widget-config path against the grouped shape.
        edges = [e for e in edges if e[1] != "widget_config"]

    if key := data.get("geocoder"):
        url = org.service_url(key)
        payload["geocoder"] = {"url": url, "name": "Northgate Locator"}
        edges.append((url, "geocoder", "/geocoder/url", None))

    if key := data.get("print"):
        url = org.service_url(key)
        payload["printTask"] = {"url": f"{url}/Export%20Web%20Map%20Task"}
        edges.append((url, "print_service", "/printTask/url", None))

    if key := data.get("gp"):
        url = org.service_url(key)
        payload["gpServices"] = [{"url": f"{url}/OptimizeRoutes", "name": "Optimize Routes"}]
        edges.append((url, "gp_service", "/gpServices/0/url", None))

    return payload, edges


def build_exb(org: Org, item: dict[str, Any]) -> tuple[dict[str, Any], list[Edge]]:
    data = item["data"]
    map_id = org.id_of(data["map"])
    well_formed = bool(data.get("well_formed"))

    map_widget: dict[str, Any] = {
        "uri": "widgets/arcgis/map/",
        "version": "1.15.0",
        "id": "widget_1",
        "useDataSources": [{"dataSourceId": "dataSource_1", "mainDataSourceId": "dataSource_1"}],
        "config": {"initialMapDataSourceID": "dataSource_1"},
    }
    text_widget: dict[str, Any] = {
        "uri": "widgets/common/text/",
        "version": "1.15.0",
        "id": "widget_2",
        "config": {"text": "<p>Street resurfacing by ward.</p>"},
    }
    image_widget: dict[str, Any] = {
        "uri": "widgets/common/image/",
        "version": "1.15.0",
        "id": "widget_3",
        "config": {"functionConfig": {"imageParam": {"url": "images/legend.png"}}},
    }

    if well_formed:
        map_widget["label"] = "Resurfacing map"
        text_widget["label"] = "Program description"
        image_widget["label"] = "Legend"
        image_widget["config"]["functionConfig"]["altText"] = "Pavement condition legend"
        layout_type = "FLOW"
    else:
        # Case 10: no widget labels, a fixed layout that fixes tab order to
        # visual position, an image with no alt text, and a heading level that
        # skips. All statically detectable in the config JSON, with no browser.
        image_widget["config"]["functionConfig"]["altText"] = ""
        text_widget["config"]["text"] = "<h4>Find a park</h4><p>Parks and trails.</p>"
        layout_type = "FIXED"

    payload = {
        "__esri_exb_version": "1.15.0",
        "name": item["title"],
        "widgets": {"widget_1": map_widget, "widget_2": text_widget, "widget_3": image_widget},
        "dataSources": {
            "dataSource_1": {
                "id": "dataSource_1",
                "type": "WEB_MAP",
                "itemId": map_id,
                "portalUrl": org.org["portal_url"],
                "sourceLabel": "Web map",
            }
        },
        "views": {"view_1": {"id": "view_1", "label": "Page 1", "layout": {"LARGE": "layout_1"}}},
        "layouts": {"layout_1": {"id": "layout_1", "type": layout_type, "order": ["widget_1"]}},
        "pages": {"page_1": {"id": "page_1", "label": "Main", "view": "view_1"}},
    }
    return payload, [(data["map"], "data_source", "/dataSources/dataSource_1/itemId", None)]


def build_dashboard(org: Org, item: dict[str, Any]) -> tuple[dict[str, Any], list[Edge]]:
    data = item["data"]
    payload = {
        "version": 66,
        "layout": {"rootElement": {"type": "stackLayoutElement", "orientation": "col"}},
        "widgets": [
            {
                "type": "mapWidget",
                "id": "map_widget_1",
                "name": "Utilities",
                "itemId": org.id_of(data["map"]),
                "mapTools": [{"type": "mapDefaultTools"}],
            },
            {"type": "indicatorWidget", "id": "indicator_1", "name": "Open breaks"},
        ],
        "theme": "light",
    }
    return payload, [(data["map"], "data_source", "/widgets/0/itemId", None)]


def build_instant_app(org: Org, item: dict[str, Any]) -> tuple[dict[str, Any], list[Edge]]:
    data = item["data"]
    payload = {
        "source": f"instant/{data.get('template', 'nearby')}",
        "folderId": None,
        "values": {
            "webmap": org.id_of(data["map"]),
            "theme": "light",
            "searchConfiguration": {"activeSourceIndex": "all"},
            "mapA11yDesc": "Map of address points",
        },
    }
    return payload, [(data["map"], "data_source", "/values/webmap", None)]


def build_storymap(org: Org, item: dict[str, Any]) -> tuple[dict[str, Any], list[Edge]]:
    data = item["data"]
    payload = {
        "root": "n-root",
        "nodes": {
            "n-root": {"type": "story", "data": {"storyTheme": "summit"}, "children": ["n-map-1"]},
            "n-map-1": {
                "type": "webmap",
                "data": {"itemId": org.id_of(data["map"]), "itemType": "Web Map", "caption": ""},
            },
        },
    }
    return payload, [(data["map"], "data_source", "/nodes/n-map-1/data/itemId", None)]


def build_webscene(org: Org, item: dict[str, Any]) -> tuple[dict[str, Any], list[Edge]]:
    data = item["data"]
    layers: list[dict[str, Any]] = []
    edges: list[Edge] = []
    for key in data.get("operational", []):
        for layer in org.service_layers(key):
            i = len(layers)
            layers.append(
                {
                    "id": f"scene_layer_{i}",
                    "layerType": "ArcGISSceneServiceLayer",
                    "url": org.layer_url(key, layer["id"]),
                    "title": layer["name"],
                }
            )
            edges.append(
                (
                    org.service_url(key),
                    "operational_layer",
                    f"/operationalLayers/{i}/url",
                    layer["id"],
                )
            )
    payload = {
        "operationalLayers": layers,
        "baseMap": {"baseMapLayers": [], "title": "Topographic"},
        "version": "1.34",
        "authoringApp": "ArcGISSceneViewer",
    }
    return payload, edges


def build_custom_js(org: Org, item: dict[str, Any]) -> tuple[dict[str, Any], list[Edge]]:
    """A hand-built app on the retired ArcGIS API for JavaScript 3.x.

    The `dojoConfig` block and a pinned `js.arcgis.com/3.x` URL are the real
    signals a legacy template leaves behind in its config.
    """
    data = item["data"]
    edges: list[Edge] = []
    layers = []
    for i, key in enumerate(data.get("layers", [])):
        url = org.service_url(key)
        layers.append({"url": f"{url}/0", "title": "Voting Precincts", "visible": True})
        edges.append((url, "operational_layer", f"/operationalLayers/{i}/url", 0))

    payload: dict[str, Any] = {
        "appName": item["title"],
        "apiUrl": f"https://js.arcgis.com/{data['jsapi_version']}/",
        "dojoConfig": {"async": True, "parseOnLoad": False, "packages": ["dijit", "dgrid"]},
        "operationalLayers": layers,
    }
    if key := data.get("geocoder"):
        url = org.service_url(key)
        payload["locatorUrl"] = url
        edges.append((url, "geocoder", "/locatorUrl", None))
    return payload, edges


BUILDERS = {
    "webmap": build_webmap,
    "webscene": build_webscene,
    "wab": build_wab,
    "exb": build_exb,
    "dashboard": build_dashboard,
    "instant_app": build_instant_app,
    "storymap": build_storymap,
    "custom_js": build_custom_js,
}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build() -> None:
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    org = Org(spec)

    for name in GENERATED:
        shutil.rmtree(ROOT / name, ignore_errors=True)

    _write_portal(org)
    all_edges = _write_items(org, spec["items"])
    _write_services(org)
    _write_search(org, spec["items"], ROOT / "search")
    _write_expected(org, spec["items"], all_edges, ROOT / "expected")
    _write_run2(org, spec)

    print(f"built {sum(1 for _ in ROOT.rglob('*.json'))} JSON files under {ROOT}")


def _write_portal(org: Org) -> None:
    o = org.org
    write_json(
        ROOT / "portal" / "self.json",
        {
            "id": o["org_id"],
            "name": o["name"],
            "portalName": "ArcGIS Enterprise",
            "portalHostname": o["portal_url"].split("//", 1)[1],
            "isPortal": o["kind"] == "enterprise",
            "currentVersion": o["version"],
            "urlKey": None,
            "customBaseUrl": None,
            "user": {"username": "svc_inventory", "role": "org_admin"},
        },
    )

    users = [u for u in org.spec["users"] if u.get("exists", True)]
    write_json(
        ROOT / "portal" / "users.json",
        {
            "total": len(users),
            "start": 1,
            "num": len(users),
            "nextStart": -1,
            # The departed account is deliberately absent. Item 4's owner
            # resolves to nothing, which is what owner_exists=0 means.
            "users": [
                {
                    "username": u["username"],
                    "fullName": u["fullName"],
                    "role": u.get("role", "org_user"),
                    "disabled": False,
                }
                for u in users
            ],
        },
    )

    write_json(
        ROOT / "portal" / "groups.json",
        {
            "total": len(org.spec["groups"]),
            "start": 1,
            "num": len(org.spec["groups"]),
            "nextStart": -1,
            "results": [
                {"id": g["id"], "title": g["title"], "access": "org"} for g in org.spec["groups"]
            ],
        },
    )


def _write_items(org: Org, items: list[dict[str, Any]]) -> dict[str, list[Edge]]:
    edges_by_item: dict[str, list[Edge]] = {}

    for item in items:
        write_json(ROOT / "items" / f"{item_id(item['n'])}.json", build_item(org, item))

        data = item.get("data")
        if not data:
            edges_by_item[item["key"]] = []
            continue

        kind = data["kind"]
        path = ROOT / "items" / f"{item_id(item['n'])}.data.json"

        if kind == "malformed":
            # Case 14: genuinely invalid JSON on disk. The transport has to fail
            # in a way the crawler records rather than dies on.
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"appId": "a0000000000000000000000000000014", "wabVersion": "2.29",\n'
                '  "map": {"itemId": "a00000000000000000000000000000\n',
                encoding="utf-8",
                newline="",
            )
            edges_by_item[item["key"]] = []
            continue

        if kind == "forbidden":
            # Case 15: the REST API answers HTTP 200 with an error object. That
            # is genuinely how it reports a permission failure.
            write_json(
                path,
                {
                    "error": {
                        "code": 403,
                        "messageCode": "GWM_0003",
                        "message": (
                            "You do not have permissions to access this resource "
                            "or perform this operation."
                        ),
                        "details": [],
                    }
                },
            )
            edges_by_item[item["key"]] = []
            continue

        payload, edges = BUILDERS[kind](org, item)
        write_json(path, payload)
        edges_by_item[item["key"]] = edges

    return edges_by_item


def _write_services(org: Org) -> None:
    for key, svc in org.services.items():
        url = org.service_url(key)
        host, _, path = url.split("//", 1)[1].partition("/")
        target = service_file(host, path)

        if svc.get("http_status") == 404:
            write_json(
                target,
                {"error": {"code": 404, "message": "Service not found.", "details": []}},
            )
            continue

        layers = svc.get("layers") or []
        write_json(
            target,
            {
                "currentVersion": 11.4,
                "serviceDescription": "",
                "serviceItemId": None,
                "capabilities": "Query,Extract" if svc["type"] == "FeatureServer" else "Map,Query",
                "layers": [
                    {"id": lyr["id"], "name": lyr["name"], "type": "Feature Layer"}
                    for lyr in layers
                ],
                "tables": [],
                "spatialReference": {"wkid": 102671, "latestWkid": 3435},
            },
        )


def _write_search(org: Org, items: list[dict[str, Any]], out: Path) -> None:
    results = [build_item(org, i) for i in items]
    pages = [results[i : i + PAGE_SIZE] for i in range(0, len(results), PAGE_SIZE)] or [[]]

    for index, page in enumerate(pages, start=1):
        start = (index - 1) * PAGE_SIZE + 1
        next_start = start + PAGE_SIZE if index < len(pages) else -1
        write_json(
            out / f"page-{index}.json",
            {
                "query": "",
                "total": len(results),
                "start": start,
                "num": PAGE_SIZE,
                "nextStart": next_start,
                "results": page,
            },
        )


def _write_expected(
    org: Org, items: list[dict[str, Any]], edges_by_item: dict[str, list[Edge]], out: Path
) -> None:
    """The golden files. These are the assertions, not the inputs.

    `findings.json` and `recommendations.json` from the fixture spec are
    deliberately absent until the rules that produce them exist -- a golden file
    generated by the code under test asserts nothing.
    """
    known = {u["username"] for u in org.spec["users"] if u.get("exists", True)}

    write_json(
        out / "inventory.json",
        [
            {
                "item_id": item_id(i["n"]),
                "key": i["key"],
                "title": i["title"],
                "item_type": i["type"],
                "platform": i["platform"],
                "platform_confidence": i["confidence"],
                "access": i["access"],
                "owner": i["owner"],
                "owner_exists": i["owner"] in known,
                "num_views": i.get("numViews"),
                "exercises": i.get("exercises"),
            }
            for i in items
        ],
    )

    rows = []
    for item in items:
        for to, relation, source_path, layer_index in edges_by_item.get(item["key"], []):
            # Edge targets are either an item key (resolved to an item id) or an
            # already-normalized service URL.
            target = {"to_item_id": org.id_of(to)} if to in org.items else {"to_url": to}
            rows.append(
                {
                    "from_item_id": item_id(item["n"]),
                    **target,
                    "relation": relation,
                    "source_path": source_path,
                    "layer_index": layer_index,
                }
            )
    rows.sort(key=lambda r: (r["from_item_id"], r["relation"], r["source_path"] or ""))
    write_json(out / "edges.json", rows)

    write_json(
        out / "endpoints.json",
        sorted(
            {
                org.service_url(key): {
                    "url_normalized": org.service_url(key).replace("http://", "https://"),
                    "key": key,
                    "service_type": svc["type"],
                    "is_https": svc.get("scheme", "https") == "https",
                    "host": svc.get("host", org.org["services_host"]),
                    "access": svc["access"],
                    "reachable": svc.get("http_status", 200) == 200,
                }
                for key, svc in org.services.items()
            }.values(),
            key=lambda r: r["key"],
        ),
    )


def _write_run2(org: Org, spec: dict[str, Any]) -> None:
    """The same org a month later.

    Without a second run the diff machinery is untested and 'authored data
    survives a re-crawl' is a claim rather than a test.
    """
    run2 = spec["run2"]
    deleted = set(run2.get("deleted", []))
    views = run2.get("view_increases", {})
    reshared = run2.get("reshared", {})
    resolved = set(run2.get("resolved", []))

    items: list[dict[str, Any]] = []
    for item in spec["items"]:
        if item["key"] in deleted:
            continue
        clone = dict(item)
        if item["key"] in views:
            clone["numViews"] = (item.get("numViews") or 0) + views[item["key"]]
        if item["key"] in reshared:
            clone["access"] = reshared[item["key"]]
        items.append(clone)

    items.extend(run2.get("added", []))
    items.sort(key=lambda i: i["n"])

    # Only what actually changed gets an item file here. The overlay falls back
    # to run 1 for everything else, so a run2 diff shows the delta and nothing
    # else. The search pages still carry every item, because that is what the
    # real API returns.
    changed = set(views) | set(reshared) | {i["key"] for i in run2.get("added", [])}
    for item in items:
        if item["key"] in changed:
            write_json(
                ROOT / "run2" / "items" / f"{item_id(item['n'])}.json", build_item(org, item)
            )

    edges = _write_items_data_only(org, run2.get("added", []), ROOT / "run2" / "items")
    _write_search(org, items, ROOT / "run2" / "search")

    # The 404 service is back, so its reachability finding stops firing.
    for key in resolved:
        url = org.service_url(key)
        host, _, path = url.split("//", 1)[1].partition("/")
        write_json(
            ROOT / "run2" / "services" / host / fixture_service_filename(path),
            {
                "currentVersion": 11.4,
                "serviceDescription": "",
                "capabilities": "Map,Query",
                "layers": [{"id": 0, "name": "Storm Mains (restored)", "type": "Feature Layer"}],
                "tables": [],
                "spatialReference": {"wkid": 102671, "latestWkid": 3435},
            },
        )

    write_json(
        ROOT / "run2" / "expected" / "diff.json",
        {
            "captured_at": run2["captured_at"],
            "disappeared": sorted(item_id(org.items[k]["n"]) for k in deleted),
            "added": sorted(item_id(i["n"]) for i in run2.get("added", [])),
            "view_increases": {item_id(org.items[k]["n"]): v for k, v in sorted(views.items())},
            "reshared": {item_id(org.items[k]["n"]): v for k, v in sorted(reshared.items())},
            "resolved_endpoints": sorted(org.service_url(k) for k in resolved),
            "new_edges": [
                {
                    "from_item_id": item_id(i["n"]),
                    "relation": r,
                    "source_path": p,
                }
                for i in run2.get("added", [])
                for _, r, p, _ in edges.get(i["key"], [])
            ],
        },
    )


def _write_items_data_only(
    org: Org, items: list[dict[str, Any]], out: Path
) -> dict[str, list[Edge]]:
    edges: dict[str, list[Edge]] = {}
    for item in items:
        data = item.get("data")
        if not data:
            continue
        payload, item_edges = BUILDERS[data["kind"]](org, item)
        write_json(out / f"{item_id(item['n'])}.data.json", payload)
        edges[item["key"]] = item_edges
    return edges


if __name__ == "__main__":
    build()
