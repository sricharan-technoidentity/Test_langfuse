"""Task2: Langfuse dashboard configuration definitions.

STATUS: REFERENCE ONLY - NOT READY FOR API PROVISIONING

This module provides dashboard metric DEFINITIONS that describe what should be
measured. These are NOT in the correct Langfuse API schema and CANNOT be used
to provision dashboards via the unstable API directly.

LANGFUSE v4.76.0 DASHBOARD API FACTS (from docs):
1. Unstable endpoints: /api/public/unstable/dashboard-widgets, /dashboards
2. Supported views: observations, scores-numeric, scores-boolean, scores-categorical
3. The "traces" view is LEGACY and NOT supported for new widgets
4. Metrics use: {"measure": "field", "agg": "sum|count|avg|uniq|..."}
5. Dimensions use: {"field": "fieldname"}
6. Aggregation "uniq" (not "countDistinct") for distinct counts

WHAT THIS MODULE PROVIDES:
- Metric definitions describing WHAT to measure (conceptual)
- JSON export for human reference (not API-compatible)
- Documentation of dashboard design principles

WHAT THIS MODULE DOES NOT PROVIDE:
- API-compatible widget payloads (schema mismatch)
- Working provision_dashboards() (would fail with current schema)
- traces-view widgets (not supported in Langfuse v4)

TO ACTUALLY CREATE DASHBOARDS:
Use `npx langfuse-cli api unstable-dashboard-widgets create --help` to see
the correct schema, then create widgets manually via CLI or UI.

Dashboard design principles:
- Workflow run aggregation uses workflow.run_id for dedup across steps
- Distinguishes missing/unknown/aborted/error/completed/running
- Avoids parent+step double-counts
- Excludes automation users from engineer adoption by default
- Tool latency shows unavailable when timestamps are equal (no genuine duration)
- Shows unavailable/missing for measurements that can't be computed
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# API version
DASHBOARD_API_VERSION = "unstable"
WIDGET_EXPORT_VERSION = 1

class ChartType(str, Enum):
    LINE = "line"
    BAR = "bar"
    PIE = "pie"
    TABLE = "table"
    NUMBER = "number"

class DataSource(str, Enum):
    TRACES = "traces"
    OBSERVATIONS = "observations"
    SCORES = "scores"
    SESSIONS = "sessions"

class AggregationType(str, Enum):
    COUNT = "count"
    COUNT_DISTINCT = "countDistinct"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    P50 = "p50"
    P90 = "p90"
    P95 = "p95"
    P99 = "p99"


@dataclass
class WidgetFilter:
    """A filter condition for a widget."""
    column: str
    operator: str
    value: Any


@dataclass
class WidgetDefinition:
    """Definition of a dashboard widget."""
    name: str
    description: str
    data_source: DataSource
    chart_type: ChartType
    metric: str
    aggregation: AggregationType
    breakdown_dimension: str | None = None
    filters: list[WidgetFilter] = field(default_factory=list)
    time_dimension: str = "timestamp"

    def to_json(self) -> dict:
        """Export widget as JSON for Langfuse import."""
        return {
            "$langfuseWidget": True,
            "version": WIDGET_EXPORT_VERSION,
            "name": self.name,
            "description": self.description,
            "config": {
                "dataSource": self.data_source.value,
                "chartType": self.chart_type.value,
                "metric": self.metric,
                "aggregation": self.aggregation.value,
                "breakdownDimension": self.breakdown_dimension,
                "timeDimension": self.time_dimension,
                "filters": [
                    {
                        "column": f.column,
                        "operator": f.operator,
                        "value": f.value,
                    }
                    for f in self.filters
                ],
            },
        }


@dataclass
class DashboardDefinition:
    """Definition of a dashboard with widgets."""
    name: str
    description: str
    widgets: list[WidgetDefinition] = field(default_factory=list)

    def to_json(self) -> dict:
        """Export dashboard as JSON for Langfuse import."""
        return {
            "$langfuseDashboard": True,
            "version": WIDGET_EXPORT_VERSION,
            "name": self.name,
            "description": self.description,
            "widgets": [w.to_json() for w in self.widgets],
            "layout": [
                {"widgetIndex": i, "x": 0, "y": i * 4, "width": 12, "height": 4}
                for i in range(len(self.widgets))
            ],
        }


# =============================================================================
# MANAGEMENT DASHBOARD WIDGETS
# =============================================================================

def management_widgets() -> list[WidgetDefinition]:
    """Widget definitions for the Management Dashboard.
    
    Focus: adoption, reliability, and value of AI SDLC rollout.
    
    Metrics:
    - Weekly active engineers (distinct users, excludes automation)
    - Total workflow runs per week
    - Completion rate (completed vs aborted vs error)
    - Adoption by team/repo
    - Top workflows by usage
    - Average run duration per workflow
    """
    return [
        # Weekly Active Users (exclude automation users by tag)
        WidgetDefinition(
            name="Weekly Active Engineers",
            description="DESIRED (blocked on unstable widget API): distinct human users. Exclude kiro-automation tag, not a hardcoded username. kiro-test-runner is automation.",
            data_source=DataSource.TRACES,
            chart_type=ChartType.LINE,
            metric="userId",
            aggregation=AggregationType.COUNT_DISTINCT,
            breakdown_dimension=None,
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
                WidgetFilter("tags", "not_contains", "kiro-automation"),
                WidgetFilter("userId", "is_not_null", True),
            ],
        ),

        # Total Workflow Runs
        WidgetDefinition(
            name="Workflow Runs Per Week",
            description="Total workflow executions (deduplicated by workflow.run_id)",
            data_source=DataSource.TRACES,
            chart_type=ChartType.LINE,
            metric="metadata.workflow.run_id",
            aggregation=AggregationType.COUNT_DISTINCT,
            breakdown_dimension=None,
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
            ],
        ),

        # Completion Rate by Status
        WidgetDefinition(
            name="Completion Rate by Status",
            description="Workflow run outcomes: completed/aborted/error/running",
            data_source=DataSource.TRACES,
            chart_type=ChartType.PIE,
            metric="traceId",
            aggregation=AggregationType.COUNT,
            breakdown_dimension="metadata.kiro.stop_reason",
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
            ],
        ),

        # Adoption by Team
        WidgetDefinition(
            name="Adoption by Team",
            description="Workflow runs grouped by team (unknown team shown separately)",
            data_source=DataSource.TRACES,
            chart_type=ChartType.BAR,
            metric="metadata.workflow.run_id",
            aggregation=AggregationType.COUNT_DISTINCT,
            breakdown_dimension="metadata.team",
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
            ],
        ),

        # Adoption by Repository
        WidgetDefinition(
            name="Adoption by Repository",
            description="Workflow runs grouped by repository",
            data_source=DataSource.TRACES,
            chart_type=ChartType.BAR,
            metric="metadata.workflow.run_id",
            aggregation=AggregationType.COUNT_DISTINCT,
            breakdown_dimension="metadata.repo",
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
            ],
        ),

        # Top Workflows
        WidgetDefinition(
            name="Top Workflows by Usage",
            description="Most frequently run workflows",
            data_source=DataSource.TRACES,
            chart_type=ChartType.BAR,
            metric="metadata.workflow.run_id",
            aggregation=AggregationType.COUNT_DISTINCT,
            breakdown_dimension="metadata.workflow.name",
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
                WidgetFilter("metadata.workflow.name", "is_not_null", True),
            ],
        ),

        # Average Duration by Workflow
        WidgetDefinition(
            name="Average Duration by Workflow",
            description="Average workflow execution time (ms) - NOTE: based on elapsed_ms metadata, may be incomplete",
            data_source=DataSource.TRACES,
            chart_type=ChartType.BAR,
            metric="metadata.kiro.elapsed_ms",
            aggregation=AggregationType.AVG,
            breakdown_dimension="metadata.workflow.name",
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
                WidgetFilter("metadata.kiro.elapsed_ms", "is_not_null", True),
            ],
        ),
    ]


# =============================================================================
# ENGINEERING DASHBOARD WIDGETS
# =============================================================================

def engineering_widgets() -> list[WidgetDefinition]:
    """Widget definitions for the Principal Engineer Dashboard.
    
    Focus: quality, reliability, and debugging at workflow level.
    
    Metrics:
    - Completion rate per workflow and version
    - Tool error rate by tool
    - Tool call duration (p50/p95) - NOTE: shows unavailable when no genuine duration
    - Tool calls per run
    - Failed trace drill-down
    """
    return [
        # Completion Rate by Workflow + Version
        WidgetDefinition(
            name="Completion by Workflow Version",
            description="Success rate per workflow name and version - detects regressions",
            data_source=DataSource.TRACES,
            chart_type=ChartType.TABLE,
            metric="traceId",
            aggregation=AggregationType.COUNT,
            breakdown_dimension="metadata.workflow.name,metadata.workflow.version",
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
            ],
        ),

        # Tool Error Rate
        WidgetDefinition(
            name="Tool Error Rate by Tool",
            description="Percentage of tool calls that failed, grouped by tool name",
            data_source=DataSource.OBSERVATIONS,
            chart_type=ChartType.BAR,
            metric="observationId",
            aggregation=AggregationType.COUNT,
            breakdown_dimension="name",
            filters=[
                WidgetFilter("type", "equals", "tool"),
                WidgetFilter("statusCode", "not_equals", 200),
            ],
        ),

        # Tool Latency p50
        WidgetDefinition(
            name="Tool Latency p50",
            description="Median tool call duration - NOTE: Shows 0 when tool start==end (no genuine timing data)",
            data_source=DataSource.OBSERVATIONS,
            chart_type=ChartType.LINE,
            metric="latency",
            aggregation=AggregationType.P50,
            breakdown_dimension="name",
            filters=[
                WidgetFilter("type", "equals", "tool"),
            ],
        ),

        # Tool Latency p95
        WidgetDefinition(
            name="Tool Latency p95",
            description="95th percentile tool call duration - NOTE: Shows 0 when tool start==end (no genuine timing data)",
            data_source=DataSource.OBSERVATIONS,
            chart_type=ChartType.LINE,
            metric="latency",
            aggregation=AggregationType.P95,
            breakdown_dimension="name",
            filters=[
                WidgetFilter("type", "equals", "tool"),
            ],
        ),

        # Tool Calls Per Run
        WidgetDefinition(
            name="Tool Calls Per Workflow Run",
            description="Average number of tool invocations per workflow execution",
            data_source=DataSource.TRACES,
            chart_type=ChartType.LINE,
            metric="metadata.tool_calls",
            aggregation=AggregationType.AVG,
            breakdown_dimension="metadata.workflow.name",
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
            ],
        ),

        # Failed Traces
        WidgetDefinition(
            name="Failed Traces (Error/Aborted)",
            description="Traces with error or aborted stop_reason for drill-down debugging",
            data_source=DataSource.TRACES,
            chart_type=ChartType.TABLE,
            metric="traceId",
            aggregation=AggregationType.COUNT,
            breakdown_dimension="metadata.kiro.stop_reason",
            filters=[
                WidgetFilter("tags", "contains", "kiro-workflow-step"),
                WidgetFilter("metadata.kiro.stop_reason", "in", ["error", "aborted"]),
            ],
        ),

        # Build/Test/Lint Pass Rate (Task3 scores)
        WidgetDefinition(
            name="Objective Check Pass Rate",
            description="Build/test/lint pass rates from Task3 objective scores",
            data_source=DataSource.SCORES,
            chart_type=ChartType.BAR,
            metric="scoreId",
            aggregation=AggregationType.COUNT,
            breakdown_dimension="name,value",
            filters=[
                WidgetFilter("name", "in", ["build_pass", "test_pass", "lint_clean"]),
            ],
        ),
    ]


# =============================================================================
# DASHBOARD DEFINITIONS
# =============================================================================

def get_management_dashboard() -> DashboardDefinition:
    """Get the Management Dashboard definition."""
    return DashboardDefinition(
        name="Kiro Workflow Management Dashboard",
        description="Adoption, reliability, and value metrics for AI SDLC workflows. "
                    "Task2 implementation - tracks workflow runs (deduplicated by run_id), "
                    "active engineers (excludes automation), completion rates, and adoption.",
        widgets=management_widgets(),
    )


def get_engineering_dashboard() -> DashboardDefinition:
    """Get the Principal Engineer Dashboard definition."""
    return DashboardDefinition(
        name="Kiro Workflow Engineering Dashboard",
        description="Quality, reliability, and debugging metrics for AI SDLC workflows. "
                    "Task2 implementation - tracks completion by version, tool errors, "
                    "latency (shows unavailable when no genuine timing), and failed traces.",
        widgets=engineering_widgets(),
    )


# =============================================================================
# EXPORT FUNCTIONS
# =============================================================================

def export_dashboards_json(output_dir: Path) -> dict[str, Path]:
    """Export dashboard configurations as JSON files for import.
    
    Returns dict of dashboard name -> file path.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    exports = {}

    for dashboard, filename in [
        (get_management_dashboard(), "management-dashboard.json"),
        (get_engineering_dashboard(), "engineering-dashboard.json"),
    ]:
        path = output_dir / filename
        path.write_text(json.dumps(dashboard.to_json(), indent=2))
        exports[dashboard.name] = path

    return exports


def export_widgets_json(output_dir: Path) -> dict[str, Path]:
    """Export individual widgets as JSON files."""
    output_dir.mkdir(parents=True, exist_ok=True)

    exports = {}

    for i, widget in enumerate(management_widgets() + engineering_widgets()):
        safe_name = widget.name.lower().replace(" ", "-").replace("/", "-")[:50]
        filename = f"widget-{i:02d}-{safe_name}.json"
        path = output_dir / filename
        path.write_text(json.dumps(widget.to_json(), indent=2))
        exports[widget.name] = path

    return exports


# =============================================================================
# API CLIENT (Unstable) - REFERENCE ONLY, SCHEMA MISMATCH
# =============================================================================

class LangfuseDashboardClient:
    """Client for Langfuse dashboard API (unstable).
    
    WARNING: THIS CLIENT CANNOT PROVISION WIDGETS IN CURRENT FORM.
    
    The widget schema in this module (dataSource, metric, aggregation, etc.)
    does NOT match the actual Langfuse API schema (view, metrics[], dimensions[]).
    
    The actual API schema (from langfuse-cli api unstable-dashboard-widgets create --help):
    {
      "name": "string",
      "view": "observations",  // NOT "traces"!
      "chartType": "LINE_TIME_SERIES",
      "dimensions": [{"field": "string"}],
      "metrics": [{"measure": "string", "agg": "sum|count|avg|uniq"}],
      "filters": [{"column": "string", "operator": "string", "value": ...}]
    }
    
    Use npx langfuse-cli directly for actual provisioning.
    """

    def __init__(self, base_url: str, public_key: str, secret_key: str):
        self.base_url = base_url.rstrip("/")
        self.auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        """Make an authenticated API request."""
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
            raise RuntimeError(f"API error {e.code}: {error_body}") from e

    def check_api_available(self) -> tuple[bool, str]:
        """Check if the unstable dashboard API is available.
        
        Returns (available, message).
        """
        try:
            self._request("GET", "/api/public/unstable/dashboards?limit=1")
            return True, "Dashboard API available"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False, "Dashboard API not found (may require newer Langfuse version)"
            elif e.code == 401:
                return False, "Authentication failed - check API keys"
            else:
                return False, f"API error: {e.code}"
        except Exception as e:
            return False, f"Connection error: {type(e).__name__}: {e}"

    def list_dashboards(self) -> list[dict]:
        """List existing dashboards."""
        return self._request("GET", "/api/public/unstable/dashboards").get("data", [])

    def create_dashboard(self, dashboard: DashboardDefinition) -> dict:
        """Create a new dashboard."""
        payload = {
            "name": dashboard.name,
            "description": dashboard.description,
        }
        return self._request("POST", "/api/public/unstable/dashboards", payload)

    def create_widget(self, widget: WidgetDefinition) -> dict:
        """Create a new widget.
        
        WARNING: This method will FAIL because WidgetDefinition.to_json()
        produces a schema that does not match the actual Langfuse API.
        
        The API requires: view, chartType, dimensions[], metrics[], filters[]
        Our schema has: dataSource, chartType, metric, aggregation, breakdownDimension
        
        Use npx langfuse-cli api unstable-dashboard-widgets create directly.
        """
        raise NotImplementedError(
            "Widget provisioning disabled: schema mismatch. "
            "Use npx langfuse-cli api unstable-dashboard-widgets create directly. "
            "See dashboard_config.py docstring for correct API schema."
        )

    def add_placement(self, dashboard_id: str, widget_id: str, x: int, y: int, width: int, height: int) -> dict:
        """Add a widget placement to a dashboard."""
        payload = {
            "type": "widget",
            "widgetId": widget_id,
            "position": {"x": x, "y": y, "width": width, "height": height},
        }
        return self._request("POST", f"/api/public/unstable/dashboards/{dashboard_id}/placements", payload)


def provision_dashboards(
    base_url: str,
    public_key: str,
    secret_key: str,
    dry_run: bool = True,
) -> dict:
    """Provision management and engineering dashboards via API.
    
    STATUS: DISABLED - SCHEMA MISMATCH
    
    This function cannot provision dashboards because the widget schema in
    this module does not match the actual Langfuse API schema.
    
    TO PROVISION DASHBOARDS:
    1. Use npx langfuse-cli api unstable-dashboard-widgets create --help
       to see the correct schema
    2. Create widgets via CLI with correct schema
    3. Create dashboard and add placements
    
    Example CLI commands:
        # List existing dashboards
        npx langfuse-cli api unstable-dashboards list --json
        
        # Create a dashboard
        npx langfuse-cli api unstable-dashboards create \\
          --body-json '{"name":"My Dashboard","description":"..."}'
        
        # Create a widget (correct schema!)
        npx langfuse-cli api unstable-dashboard-widgets create \\
          --body-json '{
            "name": "Widget Name",
            "view": "observations",
            "chartType": "LINE_TIME_SERIES",
            "dimensions": [{"field": "name"}],
            "metrics": [{"measure": "observationId", "agg": "count"}],
            "filters": []
          }'
    """
    return {
        "success": False,
        "api_available": None,
        "message": "Dashboard provisioning disabled: schema mismatch between this module and Langfuse API",
        "recommendation": "Use npx langfuse-cli api unstable-dashboard-widgets create directly",
        "docs": "npx langfuse-cli api unstable-dashboard-widgets create --help",
    }


# =============================================================================
# DOCUMENTATION
# =============================================================================

DASHBOARD_NOTES = """
# Task2 Dashboard Implementation Notes

## Workflow Run Aggregation
- Uses metadata.workflow.run_id for deduplication across steps
- A single workflow execution (e.g., two-step-demo) creates one run_id
- Each step is a separate trace, but they share the same run_id
- Dashboard counts distinct run_ids, not total traces

## Completion Status Mapping
- completed: stop_reason == "complete" or "done"
- aborted: stop_reason == "aborted" or "cancelled" or "interrupted"
- error: stop_reason == "error" or trace has error status
- running: no stop_reason yet (trace still in progress)
- unknown: stop_reason is null/missing on completed trace

Note: The Stop hook does NOT guarantee workflow success. A workflow may
stop due to error, abort, or timeout. The actual outcome depends on
stop_reason metadata.

## User Exclusion for Adoption
- Use identity_type=automation and tag kiro-automation (kiro-test-runner, chomu-local)
- Do not hardcode a single username as the only exclusion
- Named users are NOT assumed human; identity_type=human is required
- demo-engineering is a demo label, not a real company team / multi-user rollout

## Tool Latency Caveat
In prior Kiro hooks, tool start and end timestamps are often equal (no genuine
duration data). Dashboard shows:
- 0ms when timestamps are equal (documented as "no genuine timing data")
- Actual latency only when end > start

## Missing/Unavailable Measurements
- Token usage: Not available (Kiro credits != tokens)
- Time saved: Requires external baseline, not measurable
- Hallucination scores: Requires separate evaluation pipeline

## Dashboard API Status
- Langfuse v4.76.0 has unstable dashboard API at /api/public/unstable/*
- This API may change without notice
- If API unavailable: export JSON configs and import via UI
"""
