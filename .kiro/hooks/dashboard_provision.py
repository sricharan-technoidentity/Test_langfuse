#!/usr/bin/env python3
"""Task2: Functional Langfuse dashboard provisioning via public unstable API.

This module creates and manages two dashboards in Langfuse:
1. Management Dashboard - adoption, reliability, value metrics
2. Engineering Dashboard - quality, debugging, workflow analysis

LANGFUSE v4.76.0 PUBLIC API SCHEMA (from langfuse-cli):
- Widget creation: POST /api/public/unstable/dashboard-widgets
- Dashboard creation: POST /api/public/unstable/dashboards
- Placement: POST /api/public/unstable/dashboards/{id}/placements

Widget schema (required fields):
  name: string
  view: "observations" | "scores-numeric" | "scores-boolean" | "scores-categorical"
  chartType: "LINE_TIME_SERIES" | "BAR_TIME_SERIES" | "HORIZONTAL_BAR" | "VERTICAL_BAR" | "PIE" | "NUMBER" | "HISTOGRAM" | "PIVOT_TABLE"
  dimensions: [{"field": "fieldname"}]
  metrics: [{"measure": "measurename", "agg": "sum|avg|count|max|min|p50|p95|p99"}]
  filters: [{"column": "col", "operator": "=|!=|>|<|contains|...", "value": ..., "type": "string|number|boolean"}]

OBSERVATIONS VIEW DIMENSIONS (from Metrics API v2 docs):
  environment, type, name, level, version, tags, release, traceName, traceRelease,
  traceVersion, providedModelName, promptName, promptVersion, isRootObservation, startTimeMonth

OBSERVATIONS VIEW MEASURES:
  count, latency, streamingLatency, inputTokens, outputTokens, totalTokens,
  outputTokensPerSecond, tokensPerSecond, inputCost, outputCost, totalCost,
  timeToFirstToken, countScores

NOTE: The legacy 'traces' view is NOT supported in unstable API.
Widget-level metadata filtering (workflow.run_id, team, repo) is NOT a standard
dimension. Widgets below count OBSERVATIONS (or scores), not native workflow runs.
Do not read observation counts as workflow-run counts. Parent-chat generation
latency is NOT native workflow duration (use workflow-state root startedAt/endedAt).

BLOCKED on public unstable widget API (documented, not faked):
- Distinct workflow.run_id counts (custom metadata is not a widget dimension)
- Distinct human engineers (userId is high-cardinality; kiro-test-runner is automation)
- Native completion rate from workflow-state
- Native workflow duration
- True per-tool duration (Kiro tool spans often start==end → 0ms)
- Team/repo adoption breakdowns as first-class dimensions
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WidgetSpec:
    """Widget specification matching Langfuse unstable API schema."""
    name: str
    description: str
    view: str  # observations, scores-numeric, scores-boolean, scores-categorical
    chart_type: str  # LINE_TIME_SERIES, BAR_TIME_SERIES, HORIZONTAL_BAR, VERTICAL_BAR, PIE, NUMBER, HISTOGRAM, PIVOT_TABLE
    dimensions: list[dict]  # [{"field": "name"}]
    metrics: list[dict]  # [{"measure": "count", "agg": "sum"}]
    filters: list[dict] = field(default_factory=list)  # [{"column": "...", "operator": "=", "value": ..., "type": "string"}]

    def to_api_payload(self) -> dict:
        """Convert to Langfuse API request body."""
        validate_widget_spec(self)
        return {
            "name": self.name,
            "description": self.description,
            "view": self.view,
            "chartType": self.chart_type,
            "dimensions": self.dimensions,
            "metrics": self.metrics,
            "filters": self.filters,
        }


CHARTS_ALLOWING_MULTI_DIM = frozenset({"PIVOT_TABLE"})


def validate_widget_spec(spec: WidgetSpec) -> None:
    """Langfuse unstable API: only PIVOT_TABLE may have multiple dimensions.

    Citation: runtime HTTP 400 'Only pivot table widgets can have multiple dimensions'
    plus langfuse-cli `unstable-dashboard-widgets create` chartType enum.
    """
    if len(spec.dimensions) > 1 and spec.chart_type not in CHARTS_ALLOWING_MULTI_DIM:
        raise ValueError(
            f"Widget '{spec.name}': chartType {spec.chart_type} cannot have "
            f"{len(spec.dimensions)} dimensions. Use PIVOT_TABLE."
        )


@dataclass
class DashboardSpec:
    """Dashboard specification."""
    name: str
    description: str
    widgets: list[WidgetSpec] = field(default_factory=list)


def management_widgets() -> list[WidgetSpec]:
    """Management dashboard widgets using supported observations view.
    
    Focus: volume, latency, cost trends across the project.
    Note: User/team/workflow metadata grouping requires custom dimensions
    that may not be standard. Using available standard dimensions.
    """
    return [
        # Total observation count over time
        WidgetSpec(
            name="Observation Volume",
            description="OBSERVATION count over time — not native workflow-run count. One two-step workflow typically emits many observations across parent+step traces.",
            view="observations",
            chart_type="LINE_TIME_SERIES",
            dimensions=[],  # Time series implicit
            metrics=[{"measure": "count", "agg": "sum"}],
            filters=[],
        ),

        # Observations by type breakdown
        WidgetSpec(
            name="Observations by Type",
            description="Distribution of SPAN, GENERATION, EVENT observations",
            view="observations",
            chart_type="PIE",
            dimensions=[{"field": "type"}],
            metrics=[{"measure": "count", "agg": "sum"}],
            filters=[],
        ),

        # Total cost over time
        WidgetSpec(
            name="Total Cost Trend",
            description="LLM cost over time (USD)",
            view="observations",
            chart_type="LINE_TIME_SERIES",
            dimensions=[],
            metrics=[{"measure": "totalCost", "agg": "sum"}],
            filters=[],
        ),

        # Cost by model
        WidgetSpec(
            name="Cost by Model",
            description="LLM cost breakdown by model",
            view="observations",
            chart_type="HORIZONTAL_BAR",
            dimensions=[{"field": "providedModelName"}],
            metrics=[{"measure": "totalCost", "agg": "sum"}],
            filters=[{"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}],
        ),

        # Token usage over time
        WidgetSpec(
            name="Token Usage",
            description="Total tokens consumed over time",
            view="observations",
            chart_type="LINE_TIME_SERIES",
            dimensions=[],
            metrics=[{"measure": "totalTokens", "agg": "sum"}],
            filters=[{"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}],
        ),

        # Observations by environment
        WidgetSpec(
            name="Volume by Environment",
            description="Observation count by deployment environment",
            view="observations",
            chart_type="VERTICAL_BAR",
            dimensions=[{"field": "environment"}],
            metrics=[{"measure": "count", "agg": "sum"}],
            filters=[],
        ),
    ]


def engineering_widgets() -> list[WidgetSpec]:
    """Engineering dashboard widgets for debugging and quality analysis.
    
    Focus: latency, errors, model performance.
    """
    return [
        # Latency p50 over time
        WidgetSpec(
            name="Latency p50",
            description="Median OBSERVATION latency (ms). Not native workflow duration. Hung parent-chat turns inflate this. Tool spans with start==end contribute 0ms and should be treated as missing, not fast.",
            view="observations",
            chart_type="LINE_TIME_SERIES",
            dimensions=[],
            metrics=[{"measure": "latency", "agg": "p50"}],
            filters=[],
        ),

        # Latency p95 over time
        WidgetSpec(
            name="Latency p95",
            description="p95 OBSERVATION latency (ms). Not native workflow wall time from workflow-state.json.",
            view="observations",
            chart_type="LINE_TIME_SERIES",
            dimensions=[],
            metrics=[{"measure": "latency", "agg": "p95"}],
            filters=[],
        ),

        # Latency by observation name
        WidgetSpec(
            name="Latency by Operation",
            description="p95 latency by observation name (top operations)",
            view="observations",
            chart_type="HORIZONTAL_BAR",
            dimensions=[{"field": "name"}],
            metrics=[{"measure": "latency", "agg": "p95"}],
            filters=[],
        ),

        # Generation latency (time to first token)
        WidgetSpec(
            name="Time to First Token",
            description="Time to first token for generations (ms)",
            view="observations",
            chart_type="LINE_TIME_SERIES",
            dimensions=[],
            metrics=[{"measure": "timeToFirstToken", "agg": "p50"}],
            filters=[{"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}],
        ),

        # Observations by level (for error detection)
        WidgetSpec(
            name="Observations by Level",
            description="Volume by log level (detect ERROR spikes)",
            view="observations",
            chart_type="LINE_TIME_SERIES",
            dimensions=[{"field": "level"}],
            metrics=[{"measure": "count", "agg": "sum"}],
            filters=[],
        ),

        # Score distribution (if scores exist)
        # NOTE: VERTICAL_BAR only supports single dimension. Use PIVOT_TABLE for name+booleanValue breakdown.
        WidgetSpec(
            name="Boolean Score Rates",
            description="Pass/fail rates for boolean scores (Task3 build_pass/test_pass/lint_clean)",
            view="scores-boolean",
            chart_type="PIVOT_TABLE",  # Multiple dimensions require PIVOT_TABLE
            dimensions=[{"field": "name"}, {"field": "booleanValue"}],
            metrics=[{"measure": "count", "agg": "sum"}],
            filters=[],
        ),
    ]


class LangfuseClient:
    """Client for Langfuse unstable dashboard API."""

    def __init__(self, base_url: str, public_key: str, secret_key: str):
        self.base_url = base_url.rstrip("/")
        self.auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        """Make authenticated API request."""
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Basic {self.auth}",
            "Content-Type": "application/json",
        }
        body = json.dumps(data).encode() if data else None

        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"HTTP {e.code}: {error_body[:500]}") from e

    def check_api_available(self) -> tuple[bool, str]:
        """Check if dashboard API is available."""
        try:
            self._request("GET", "/api/public/unstable/dashboards?limit=1")
            return True, "Dashboard API available"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, "Dashboard API not found (requires Langfuse v4+)"
            elif e.code == 401:
                return False, "Authentication failed"
            return False, f"HTTP {e.code}"
        except Exception as e:
            return False, f"Connection error: {e}"

    def list_dashboards(self) -> list[dict]:
        """List existing dashboards."""
        resp = self._request("GET", "/api/public/unstable/dashboards")
        return resp.get("data", [])

    def find_dashboard_by_name(self, name: str) -> dict | None:
        """Find dashboard by exact name."""
        for d in self.list_dashboards():
            if d.get("name") == name:
                return d
        return None

    def create_dashboard(self, name: str, description: str) -> dict:
        """Create a new dashboard."""
        return self._request("POST", "/api/public/unstable/dashboards", {
            "name": name,
            "description": description,
        })

    def list_widgets(self) -> list[dict]:
        """List existing widgets."""
        resp = self._request("GET", "/api/public/unstable/dashboard-widgets")
        return resp.get("data", [])

    def find_widget_by_name(self, name: str) -> dict | None:
        """Find widget by exact name."""
        for w in self.list_widgets():
            if w.get("name") == name:
                return w
        return None

    def create_widget(self, spec: WidgetSpec) -> dict:
        """Create a new widget."""
        return self._request("POST", "/api/public/unstable/dashboard-widgets", spec.to_api_payload())

    def update_widget(self, widget_id: str, spec: WidgetSpec) -> dict:
        """Update an existing widget."""
        return self._request("PATCH", f"/api/public/unstable/dashboard-widgets/{widget_id}", spec.to_api_payload())

    def add_placement(self, dashboard_id: str, widget_id: str, x: int, y: int, width: int = 6, height: int = 6) -> dict:
        """Add widget placement. POST body uses documented widgetId (not widget.id)."""
        return self._request("POST", f"/api/public/unstable/dashboards/{dashboard_id}/placements", {
            "type": "widget",
            "widgetId": widget_id,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        })

    def get_dashboard(self, dashboard_id: str) -> dict:
        """GET /api/public/unstable/dashboards/{dashboardId}.

        Documented 200 shape (langfuse public unstable API):
          {id, name, description, createdAt, updatedAt,
           definition: {widgets: [{type, id, widgetId, x, y, width, height}]}}
        `definition.widgets[].id` is the *placement* UUID.
        `definition.widgets[].widgetId` is the widget id (same field as POST).
        There is no top-level placements array on GET-by-id.
        """
        return self._request("GET", f"/api/public/unstable/dashboards/{dashboard_id}")

    def get_dashboard_placements(self, dashboard_id: str) -> list[dict]:
        """Return placement objects from GET-by-id. Raises PlacementParseError on list-count shapes."""
        dashboard = self.get_dashboard(dashboard_id)
        placements, _source = collect_placements_from_dashboard(dashboard)
        return placements

    def delete_placement(self, dashboard_id: str, placement_id: str) -> dict:
        """DELETE /api/public/unstable/dashboards/{dashboardId}/placements/{placementId}.

        Removes a tile without deleting the widget object.
        """
        return self._request(
            "DELETE",
            f"/api/public/unstable/dashboards/{dashboard_id}/placements/{placement_id}",
        )


class PlacementParseError(ValueError):
    """GET dashboard did not contain a documented placement array. Fail closed."""


class CleanupSafetyError(ValueError):
    """Cleanup would drop below the original tile counts. Refuse."""


def collect_placements_from_dashboard(dashboard: dict) -> tuple[list[dict], str]:
    """Parse placements from the documented GET dashboard 200 body.

    Evidence (local acceptance 9d5390ba / evidence_d8fc.json):
    - LIST `/unstable/dashboards` reports `placements` as an INTEGER COUNT
      (management 6→12→18, engineering 13→19→25). That is not iterable tiles.
    - GET `/unstable/dashboards/{id}` 200 is `{definition: {widgets: [...]}}`
      with each tile `{type, id, widgetId, x, y, width, height}`.
    - Treating a missing array or an integer count as `[]` made pass2 POST 6+6
      more tiles. Swallowing TypeError did the same.

    Fail closed: integer `placements`, missing `definition.widgets`, or a
    non-object body must raise — never look like "nothing placed yet".
    """
    if not isinstance(dashboard, dict):
        raise PlacementParseError(
            f"GET dashboard returned {type(dashboard).__name__}, expected object"
        )

    definition = dashboard.get("definition")
    if isinstance(definition, dict) and isinstance(definition.get("widgets"), list):
        widgets = definition["widgets"]
        placements = [p for p in widgets if isinstance(p, dict)]
        if len(placements) != len(widgets):
            raise PlacementParseError(
                "definition.widgets contains non-object entries; refusing to treat as empty"
            )
        return placements, "definition.widgets"

    raw = dashboard.get("placements")
    if isinstance(raw, int):
        raise PlacementParseError(
            f"placements is an integer count ({raw}), not a tile array. "
            "This is the LIST summary field from evidence_d8fc.json. "
            "Use GET /dashboards/{{id}} and parse definition.widgets."
        )
    if isinstance(raw, list):
        placements = [p for p in raw if isinstance(p, dict)]
        if len(placements) != len(raw):
            raise PlacementParseError(
                "top-level placements contains non-object entries; refusing to treat as empty"
            )
        return placements, "placements"

    keys = list(dashboard.keys())
    def_keys = list(definition.keys()) if isinstance(definition, dict) else None
    raise PlacementParseError(
        f"No documented placement array. top-level keys={keys} definition_keys={def_keys}. "
        "Expected definition.widgets (GET-by-id) with widgetId on each tile."
    )


def extract_placed_widget_ids(placements: list) -> set[str]:
    """Collect widget ids from documented placement objects.

    Documented field is `widgetId` (POST addPlacement and GET definition.widgets).
    Placement `id` is the tile UUID — never treat it as the widget id.
    Nested `widget.id` is an undocumented guess that hid the 9d5390b failure.
    """
    if isinstance(placements, int):
        raise PlacementParseError(
            f"placements is an integer count ({placements}), not a tile array"
        )
    if not isinstance(placements, list):
        raise PlacementParseError(
            f"placements is {type(placements).__name__}, expected list of objects"
        )
    ids: set[str] = set()
    for p in placements:
        if not isinstance(p, dict):
            raise PlacementParseError("placement entry is not an object")
        wid = p.get("widgetId")
        if not isinstance(wid, str) or not wid:
            wid = p.get("widget_id") if isinstance(p.get("widget_id"), str) else None
        if isinstance(wid, str) and wid:
            ids.add(wid)
    return ids


def extract_placement_ids(placements: list) -> set[str]:
    """Collect placement tile UUIDs (`id` on definition.widgets), not widgetId."""
    if not isinstance(placements, list):
        raise PlacementParseError("placements is not a list")
    ids: set[str] = set()
    for p in placements:
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if isinstance(pid, str) and pid:
            ids.add(pid)
    return ids


def plan_failed_acceptance_cleanup(
    current_placements: list,
    delete_placement_ids: list[str],
    keep_minimum: int,
) -> dict:
    """Plan DELETE of failed-acceptance tiles only.

    Original management 6 and engineering 13 must survive. Only placement UUIDs
    created by the failed 9d5390b apply (pass1+pass2) are eligible.
    """
    if not isinstance(current_placements, list):
        raise PlacementParseError("current placements is not a list")
    present = extract_placement_ids(current_placements)
    requested = [pid for pid in delete_placement_ids if isinstance(pid, str) and pid]
    to_delete = [pid for pid in requested if pid in present]
    missing = [pid for pid in requested if pid not in present]
    remaining = len(current_placements) - len(to_delete)
    if remaining < keep_minimum:
        raise CleanupSafetyError(
            f"Cleanup would leave {remaining} tiles, below keep_minimum={keep_minimum}. "
            "Original tiles must survive. Refusing."
        )
    unknown_on_dashboard = [
        pid for pid in requested if pid not in present
    ]
    return {
        "delete_ids": to_delete,
        "already_absent": missing,
        "current_count": len(current_placements),
        "remaining_after": remaining,
        "keep_minimum": keep_minimum,
        "unknown_on_dashboard": unknown_on_dashboard,
        "safe": remaining >= keep_minimum,
    }


def provision_dashboard(
    client: LangfuseClient,
    spec: DashboardSpec,
    dry_run: bool = False,
) -> dict:
    """Provision a dashboard with widgets. Idempotent - updates existing.
    
    Returns dict with created/updated dashboard and widget IDs.
    """
    result = {
        "dashboard_name": spec.name,
        "dashboard_id": None,
        "widgets": [],
        "placements": [],
        "placements_already": None,
        "placements_skipped": 0,
        "placement_parse": None,
        "errors": [],
        "dry_run": dry_run,
    }

    # Find or create dashboard
    existing_dashboard = client.find_dashboard_by_name(spec.name)
    if existing_dashboard:
        result["dashboard_id"] = existing_dashboard["id"]
        result["dashboard_action"] = "exists"
    elif not dry_run:
        try:
            dashboard = client.create_dashboard(spec.name, spec.description)
            result["dashboard_id"] = dashboard["id"]
            result["dashboard_action"] = "created"
        except Exception as e:
            result["errors"].append(f"Dashboard creation failed: {e}")
            return result
    else:
        result["dashboard_action"] = "would_create"

    # Create/update widgets
    for i, widget_spec in enumerate(spec.widgets):
        existing_widget = client.find_widget_by_name(widget_spec.name)
        widget_result = {"name": widget_spec.name, "id": None, "action": None}

        if existing_widget:
            widget_result["id"] = existing_widget["id"]
            if not dry_run:
                try:
                    client.update_widget(existing_widget["id"], widget_spec)
                    widget_result["action"] = "updated"
                except Exception as e:
                    widget_result["action"] = "update_failed"
                    result["errors"].append(f"Widget '{widget_spec.name}' update failed: {e}")
            else:
                widget_result["action"] = "would_update"
        elif not dry_run:
            try:
                widget = client.create_widget(widget_spec)
                widget_result["id"] = widget["id"]
                widget_result["action"] = "created"
            except Exception as e:
                widget_result["action"] = "create_failed"
                result["errors"].append(f"Widget '{widget_spec.name}' creation failed: {e}")
        else:
            widget_result["action"] = "would_create"

        result["widgets"].append(widget_result)

    # Place widgets only when GET-by-id definition.widgets does not already
    # contain this widgetId. Fail closed if the GET shape cannot be parsed —
    # never treat an integer LIST count or a missing array as "nothing placed".
    if result["dashboard_id"] and not dry_run:
        placed_widget_ids: set[str] | None = None
        try:
            existing_placements = client.get_dashboard_placements(result["dashboard_id"])
            placed_widget_ids = extract_placed_widget_ids(existing_placements)
            result["placements_already"] = len(existing_placements)
            result["placement_parse"] = "definition.widgets widgetId"
        except PlacementParseError as e:
            result["errors"].append(
                f"Placement GET parse failed (fail-closed, not adding tiles): {e}"
            )
        except Exception as e:
            result["errors"].append(
                f"Placement GET failed (fail-closed, not adding tiles): {e}"
            )

        if placed_widget_ids is not None:
            for i, widget_result in enumerate(result["widgets"]):
                if not widget_result["id"]:
                    continue
                if widget_result["id"] in placed_widget_ids:
                    result["placements_skipped"] += 1
                    continue
                try:
                    x = (i % 2) * 6
                    y = (i // 2) * 6
                    placement = client.add_placement(
                        result["dashboard_id"],
                        widget_result["id"],
                        x=x, y=y, width=6, height=6
                    )
                    result["placements"].append({
                        "widget_id": widget_result["id"],
                        "placement_id": placement.get("id"),
                    })
                except Exception as e:
                    result["errors"].append(f"Placement for '{widget_result['name']}' failed: {e}")

    return result


def get_management_dashboard() -> DashboardSpec:
    """Get management dashboard specification."""
    return DashboardSpec(
        name="Kiro Management Dashboard",
        description=(
            "Observation/cost volume for local Kiro traces. NOT native workflow-run counts, "
            "NOT human engineer adoption (kiro-test-runner is automation), NOT team/repo unique runs."
        ),
        widgets=management_widgets(),
    )


def get_engineering_dashboard() -> DashboardSpec:
    """Get engineering dashboard specification."""
    return DashboardSpec(
        name="Kiro Engineering Dashboard",
        description=(
            "Observation latency/level plus boolean score pivot. Tool duration is missing when "
            "start==end. Parent-chat latency is not workflow duration."
        ),
        widgets=engineering_widgets(),
    )


def load_cleanup_plan(path: str) -> dict:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not isinstance(data.get("dashboards"), list):
        raise ValueError("cleanup file must be an object with dashboards[]")
    return data


def run_inspect_placements(client: LangfuseClient, dashboard_ids: list[str]) -> list[dict]:
    """Dump GET-by-id placement parse for parent debugging. No mutations."""
    rows = []
    for did in dashboard_ids:
        row = {"dashboard_id": did, "ok": False}
        try:
            dashboard = client.get_dashboard(did)
            row["name"] = dashboard.get("name")
            row["top_level_keys"] = list(dashboard.keys()) if isinstance(dashboard, dict) else None
            definition = dashboard.get("definition") if isinstance(dashboard, dict) else None
            row["definition_keys"] = list(definition.keys()) if isinstance(definition, dict) else None
            placements, source = collect_placements_from_dashboard(dashboard)
            row["ok"] = True
            row["placement_source"] = source
            row["placement_count"] = len(placements)
            row["widgetIds"] = sorted(extract_placed_widget_ids(placements))
            row["placement_ids"] = sorted(extract_placement_ids(placements))
            row["list_placements_field_type"] = type(dashboard.get("placements")).__name__
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)
    return rows


def run_cleanup_placements(
    client: LangfuseClient,
    plan: dict,
    execute: bool,
) -> list[dict]:
    """Delete only listed failed-acceptance placement UUIDs. Default is dry-run."""
    results = []
    for entry in plan.get("dashboards") or []:
        dashboard_id = entry.get("id")
        keep_minimum = int(entry.get("keep_count_after", 0))
        delete_ids = list(entry.get("delete_placement_ids") or [])
        row = {
            "dashboard_name": entry.get("name"),
            "dashboard_id": dashboard_id,
            "execute": execute,
            "errors": [],
            "deleted": [],
            "would_delete": [],
        }
        try:
            current = client.get_dashboard_placements(dashboard_id)
            safety = plan_failed_acceptance_cleanup(current, delete_ids, keep_minimum)
            row["plan"] = safety
            if not safety["safe"]:
                row["errors"].append("safety gate failed")
                results.append(row)
                continue
            for pid in safety["delete_ids"]:
                if execute:
                    client.delete_placement(dashboard_id, pid)
                    row["deleted"].append(pid)
                else:
                    row["would_delete"].append(pid)
            if execute:
                after = client.get_dashboard_placements(dashboard_id)
                row["remaining_after"] = len(after)
                if len(after) < keep_minimum:
                    row["errors"].append(
                        f"Post-delete count {len(after)} below keep_minimum {keep_minimum}"
                    )
        except Exception as e:
            row["errors"].append(str(e))
        results.append(row)
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Provision Langfuse dashboards for Kiro workflow monitoring"
    )
    parser.add_argument("--dry-run", action="store_true", help="Check what would be done without making changes")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--dashboard", choices=["management", "engineering", "both"], default="both",
                       help="Which dashboard to provision")
    parser.add_argument(
        "--inspect-placements",
        action="store_true",
        help="GET dashboards by id and print documented definition.widgets parse. No mutations.",
    )
    parser.add_argument(
        "--cleanup-placements",
        action="store_true",
        help="Plan/delete failed-acceptance placement UUIDs from --from-file. Dry-run unless --execute-cleanup.",
    )
    parser.add_argument(
        "--from-file",
        help="JSON cleanup plan (failed-acceptance placement UUIDs). Original 6/13 must survive.",
    )
    parser.add_argument(
        "--execute-cleanup",
        action="store_true",
        help="Actually DELETE listed placements. Default for --cleanup-placements is dry-run.",
    )
    parser.add_argument(
        "--dashboard-id",
        action="append",
        dest="dashboard_ids",
        help="Dashboard id for --inspect-placements (repeatable)",
    )

    # Credentials from environment
    parser.add_argument("--base-url", default=os.environ.get("LANGFUSE_BASE_URL", "http://127.0.0.1:3000"))
    parser.add_argument("--public-key", default=os.environ.get("LANGFUSE_PUBLIC_KEY"))
    parser.add_argument("--secret-key", default=os.environ.get("LANGFUSE_SECRET_KEY"))

    args = parser.parse_args()

    if not args.public_key or not args.secret_key:
        print("ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY required", file=sys.stderr)
        print("Set environment variables or use --public-key and --secret-key", file=sys.stderr)
        sys.exit(1)

    client = LangfuseClient(args.base_url, args.public_key, args.secret_key)

    # Check API availability
    available, message = client.check_api_available()
    if not available:
        print(f"ERROR: {message}", file=sys.stderr)
        sys.exit(1)

    if args.inspect_placements:
        ids = list(args.dashboard_ids or [])
        if args.from_file:
            plan = load_cleanup_plan(args.from_file)
            ids.extend(d.get("id") for d in plan.get("dashboards") or [] if d.get("id"))
        if not ids:
            print("ERROR: --inspect-placements needs --dashboard-id or --from-file", file=sys.stderr)
            sys.exit(1)
        rows = run_inspect_placements(client, ids)
        print(json.dumps(rows, indent=2))
        sys.exit(0 if all(r.get("ok") for r in rows) else 1)

    if args.cleanup_placements:
        if not args.from_file:
            print("ERROR: --cleanup-placements requires --from-file", file=sys.stderr)
            sys.exit(1)
        plan = load_cleanup_plan(args.from_file)
        execute = bool(args.execute_cleanup) and not args.dry_run
        rows = run_cleanup_placements(client, plan, execute=execute)
        print(json.dumps(rows, indent=2))
        has_errors = any(r.get("errors") for r in rows)
        sys.exit(1 if has_errors else 0)

    results = []
    dashboards_to_provision = []

    if args.dashboard in ("management", "both"):
        dashboards_to_provision.append(get_management_dashboard())
    if args.dashboard in ("engineering", "both"):
        dashboards_to_provision.append(get_engineering_dashboard())

    for spec in dashboards_to_provision:
        result = provision_dashboard(client, spec, dry_run=args.dry_run)
        results.append(result)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for result in results:
            print(f"\n=== {result['dashboard_name']} ===")
            print(f"Dashboard ID: {result['dashboard_id']} ({result.get('dashboard_action', 'unknown')})")
            print("Widgets:")
            for w in result["widgets"]:
                print(f"  - {w['name']}: {w['action']} (id={w['id']})")
            if result.get("placements_already") is not None:
                print(f"Placements already on dashboard: {result['placements_already']}")
            if result.get("placements_skipped"):
                print(f"Placements skipped (widgetId already present): {result['placements_skipped']}")
            if result["placements"]:
                print(f"Placements added: {len(result['placements'])}")
            if result["errors"]:
                print("Errors:")
                for e in result["errors"]:
                    print(f"  ! {e}")

    # Exit with error if any failures
    has_errors = any(r["errors"] for r in results)
    sys.exit(1 if has_errors else 0)


if __name__ == "__main__":
    main()
