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
                return json.loads(resp.read().decode())
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
        """Add widget placement to dashboard."""
        return self._request("POST", f"/api/public/unstable/dashboards/{dashboard_id}/placements", {
            "type": "widget",
            "widgetId": widget_id,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        })

    def get_dashboard_placements(self, dashboard_id: str) -> list[dict]:
        """Get placements for a dashboard (top-level or definition.placements)."""
        dashboard = self._request("GET", f"/api/public/unstable/dashboards/{dashboard_id}")
        placements = dashboard.get("placements") or []
        definition = dashboard.get("definition") if isinstance(dashboard.get("definition"), dict) else {}
        if not placements:
            placements = definition.get("placements") or []
        return placements


def extract_placed_widget_ids(placements: list) -> set[str]:
    """Collect widget ids already on a dashboard regardless of placement shape."""
    ids: set[str] = set()
    for p in placements or []:
        if not isinstance(p, dict):
            continue
        wid = p.get("widgetId") or p.get("widget_id")
        widget = p.get("widget")
        if not wid and isinstance(widget, dict):
            wid = widget.get("id")
        if wid:
            ids.add(str(wid))
    return ids


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

    # Add placements (only for newly created dashboard or if widget doesn't have placement)
    if result["dashboard_id"] and not dry_run:
        try:
            existing_placements = client.get_dashboard_placements(result["dashboard_id"])
            placed_widget_ids = extract_placed_widget_ids(existing_placements)
        except Exception:
            placed_widget_ids = set()

        for i, widget_result in enumerate(result["widgets"]):
            if widget_result["id"] and widget_result["id"] not in placed_widget_ids:
                try:
                    # Grid layout: 2 columns, 6 units wide each
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


def main():
    parser = argparse.ArgumentParser(
        description="Provision Langfuse dashboards for Kiro workflow monitoring"
    )
    parser.add_argument("--dry-run", action="store_true", help="Check what would be done without making changes")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--dashboard", choices=["management", "engineering", "both"], default="both",
                       help="Which dashboard to provision")

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
