"""Tests for Task2 dashboard_provision module."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / ".kiro" / "hooks"))
from dashboard_provision import (
    WidgetSpec,
    DashboardSpec,
    LangfuseClient,
    management_widgets,
    engineering_widgets,
    get_management_dashboard,
    get_engineering_dashboard,
    provision_dashboard,
    validate_widget_spec,
    extract_placed_widget_ids,
)


class TestWidgetSpec:
    """Test widget specification matches Langfuse API schema."""

    def test_widget_to_api_payload(self):
        """Widget should produce valid API payload."""
        widget = WidgetSpec(
            name="Test Widget",
            description="A test widget",
            view="observations",
            chart_type="LINE_TIME_SERIES",
            dimensions=[{"field": "name"}],
            metrics=[{"measure": "count", "agg": "sum"}],
            filters=[{"column": "type", "operator": "=", "value": "GENERATION", "type": "string"}],
        )
        
        payload = widget.to_api_payload()
        
        assert payload["name"] == "Test Widget"
        assert payload["view"] == "observations"
        assert payload["chartType"] == "LINE_TIME_SERIES"
        assert payload["dimensions"] == [{"field": "name"}]
        assert payload["metrics"] == [{"measure": "count", "agg": "sum"}]
        assert len(payload["filters"]) == 1

    def test_widget_has_required_fields(self):
        """Widget payload should have all required fields."""
        widget = WidgetSpec(
            name="Min Widget",
            description="",
            view="observations",
            chart_type="NUMBER",
            dimensions=[],
            metrics=[{"measure": "count", "agg": "sum"}],
        )
        
        payload = widget.to_api_payload()
        
        # Required fields per API docs
        assert "name" in payload
        assert "view" in payload
        assert "chartType" in payload
        assert "dimensions" in payload
        assert "metrics" in payload
        assert "filters" in payload


class TestWidgetViews:
    """Test widgets use only supported views."""

    def test_management_widgets_use_supported_views(self):
        """All management widgets should use supported views."""
        supported = {"observations", "scores-numeric", "scores-boolean", "scores-categorical"}
        
        for widget in management_widgets():
            assert widget.view in supported, f"Widget '{widget.name}' uses unsupported view: {widget.view}"

    def test_engineering_widgets_use_supported_views(self):
        """All engineering widgets should use supported views."""
        supported = {"observations", "scores-numeric", "scores-boolean", "scores-categorical"}
        
        for widget in engineering_widgets():
            assert widget.view in supported, f"Widget '{widget.name}' uses unsupported view: {widget.view}"

    def test_no_traces_view_widgets(self):
        """No widgets should use the legacy traces view."""
        all_widgets = management_widgets() + engineering_widgets()
        
        for widget in all_widgets:
            assert widget.view != "traces", f"Widget '{widget.name}' uses unsupported traces view"


class TestWidgetChartTypes:
    """Test widgets use valid chart types."""

    def test_valid_chart_types(self):
        """All widgets should use valid chart types."""
        valid_types = {
            "LINE_TIME_SERIES", "AREA_TIME_SERIES", "BAR_TIME_SERIES",
            "HORIZONTAL_BAR", "VERTICAL_BAR", "PIE", "NUMBER", "HISTOGRAM", "PIVOT_TABLE"
        }
        
        all_widgets = management_widgets() + engineering_widgets()
        
        for widget in all_widgets:
            assert widget.chart_type in valid_types, f"Widget '{widget.name}' uses invalid chart type: {widget.chart_type}"


class TestWidgetMetrics:
    """Test widgets use valid metrics."""

    def test_observations_metrics_valid(self):
        """Observations view widgets should use valid measures."""
        valid_measures = {
            "count", "latency", "streamingLatency", "inputTokens", "outputTokens",
            "totalTokens", "outputTokensPerSecond", "tokensPerSecond",
            "inputCost", "outputCost", "totalCost", "timeToFirstToken", "countScores"
        }
        valid_aggs = {"sum", "avg", "count", "max", "min", "p50", "p95", "p99"}
        
        for widget in management_widgets() + engineering_widgets():
            if widget.view == "observations":
                for metric in widget.metrics:
                    assert metric["measure"] in valid_measures, \
                        f"Widget '{widget.name}' uses invalid measure: {metric['measure']}"
                    assert metric["agg"] in valid_aggs, \
                        f"Widget '{widget.name}' uses invalid agg: {metric['agg']}"


class TestDashboardSpec:
    """Test dashboard specifications."""

    def test_management_dashboard_has_widgets(self):
        """Management dashboard should have widgets."""
        dashboard = get_management_dashboard()
        assert len(dashboard.widgets) >= 4  # At least several metrics
        assert dashboard.name
        assert dashboard.description

    def test_engineering_dashboard_has_widgets(self):
        """Engineering dashboard should have widgets."""
        dashboard = get_engineering_dashboard()
        assert len(dashboard.widgets) >= 4
        assert dashboard.name
        assert dashboard.description

    def test_dashboards_have_different_names(self):
        """Management and engineering dashboards should have different names."""
        mgmt = get_management_dashboard()
        eng = get_engineering_dashboard()
        assert mgmt.name != eng.name


class TestLangfuseClientMocked:
    """Test Langfuse client with mocked HTTP."""

    @mock.patch("dashboard_provision.urllib.request.urlopen")
    def test_check_api_available_success(self, mock_urlopen):
        """API check should return True when dashboard API responds."""
        mock_response = mock.MagicMock()
        mock_response.read.return_value = b'{"data": []}'
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        
        client = LangfuseClient("http://localhost:3000", "pk-test", "sk-test")
        available, message = client.check_api_available()
        
        assert available is True

    @mock.patch("dashboard_provision.urllib.request.urlopen")
    def test_list_dashboards(self, mock_urlopen):
        """Should parse dashboard list response."""
        mock_response = mock.MagicMock()
        mock_response.read.return_value = b'{"data": [{"id": "d1", "name": "Test Dashboard"}]}'
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        
        client = LangfuseClient("http://localhost:3000", "pk-test", "sk-test")
        dashboards = client.list_dashboards()
        
        assert len(dashboards) == 1
        assert dashboards[0]["name"] == "Test Dashboard"


class TestProvisionIdempotency:
    """Test idempotent dashboard provisioning."""

    @mock.patch.object(LangfuseClient, "find_widget_by_name")
    @mock.patch.object(LangfuseClient, "find_dashboard_by_name")
    @mock.patch.object(LangfuseClient, "create_dashboard")
    @mock.patch.object(LangfuseClient, "create_widget")
    @mock.patch.object(LangfuseClient, "update_widget")
    @mock.patch.object(LangfuseClient, "add_placement")
    @mock.patch.object(LangfuseClient, "get_dashboard_placements")
    def test_existing_dashboard_not_recreated(
        self,
        mock_get_placements,
        mock_add_placement,
        mock_update_widget,
        mock_create_widget,
        mock_create_dashboard,
        mock_find_dashboard,
        mock_find_widget,
    ):
        """Existing dashboard should be reused, not recreated."""
        # Dashboard exists
        mock_find_dashboard.return_value = {"id": "existing-dashboard-id", "name": "Test"}
        mock_find_widget.return_value = None  # Widgets don't exist
        mock_create_widget.return_value = {"id": "new-widget-id"}
        mock_get_placements.return_value = []
        mock_add_placement.return_value = {"id": "placement-id"}
        
        spec = DashboardSpec(
            name="Test",
            description="Test dashboard",
            widgets=[WidgetSpec(
                name="W1", description="", view="observations",
                chart_type="NUMBER", dimensions=[], metrics=[{"measure": "count", "agg": "sum"}]
            )],
        )
        
        client = LangfuseClient("http://localhost:3000", "pk-test", "sk-test")
        result = provision_dashboard(client, spec, dry_run=False)
        
        # Dashboard should not be created (it exists)
        mock_create_dashboard.assert_not_called()
        assert result["dashboard_id"] == "existing-dashboard-id"
        assert result["dashboard_action"] == "exists"

    @mock.patch.object(LangfuseClient, "find_widget_by_name")
    @mock.patch.object(LangfuseClient, "find_dashboard_by_name")
    @mock.patch.object(LangfuseClient, "update_widget")
    def test_existing_widget_updated(
        self,
        mock_update_widget,
        mock_find_dashboard,
        mock_find_widget,
    ):
        """Existing widget should be updated, not recreated."""
        mock_find_dashboard.return_value = {"id": "d1"}
        mock_find_widget.return_value = {"id": "existing-widget-id", "name": "W1"}
        mock_update_widget.return_value = {"id": "existing-widget-id"}
        
        spec = DashboardSpec(
            name="Test",
            description="",
            widgets=[WidgetSpec(
                name="W1", description="", view="observations",
                chart_type="NUMBER", dimensions=[], metrics=[{"measure": "count", "agg": "sum"}]
            )],
        )
        
        client = LangfuseClient("http://localhost:3000", "pk-test", "sk-test")
        
        # Mock get_dashboard_placements to avoid placement logic
        with mock.patch.object(client, "get_dashboard_placements", return_value=[{"widgetId": "existing-widget-id"}]):
            result = provision_dashboard(client, spec, dry_run=False)
        
        mock_update_widget.assert_called_once()
        assert result["widgets"][0]["action"] == "updated"


class TestDryRun:
    """Test dry run mode."""

    def test_dry_run_does_not_create_resources(self):
        """Dry run should check existing but not create/update/delete."""
        spec = DashboardSpec(
            name="Test",
            description="",
            widgets=[WidgetSpec(
                name="W1", description="", view="observations",
                chart_type="NUMBER", dimensions=[], metrics=[{"measure": "count", "agg": "sum"}]
            )],
        )
        
        client = LangfuseClient("http://localhost:3000", "pk-test", "sk-test")
        
        # Mock lookup methods to return "not found"
        with mock.patch.object(client, "find_dashboard_by_name", return_value=None):
            with mock.patch.object(client, "find_widget_by_name", return_value=None):
                with mock.patch.object(client, "create_dashboard") as mock_create_dash:
                    with mock.patch.object(client, "create_widget") as mock_create_widget:
                        result = provision_dashboard(client, spec, dry_run=True)
        
        # Should NOT call create methods in dry run
        mock_create_dash.assert_not_called()
        mock_create_widget.assert_not_called()
        
        assert result["dry_run"] is True
        assert result["dashboard_action"] == "would_create"
        assert result["widgets"][0]["action"] == "would_create"


class TestPivotMultiDimension:
    """Regression: VERTICAL_BAR + two dimensions returns HTTP 400."""

    def test_boolean_score_rates_is_pivot(self):
        widget = next(w for w in engineering_widgets() if w.name == "Boolean Score Rates")
        assert widget.chart_type == "PIVOT_TABLE"
        assert len(widget.dimensions) == 2
        validate_widget_spec(widget)

    def test_vertical_bar_multi_dim_rejected(self):
        bad = WidgetSpec(
            name="Boolean Score Rates",
            description="bad",
            view="scores-boolean",
            chart_type="VERTICAL_BAR",
            dimensions=[{"field": "name"}, {"field": "booleanValue"}],
            metrics=[{"measure": "count", "agg": "sum"}],
        )
        try:
            validate_widget_spec(bad)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "PIVOT_TABLE" in str(e)

    def test_all_shipped_widgets_pass_schema(self):
        for w in management_widgets() + engineering_widgets():
            validate_widget_spec(w)

    def test_observation_volume_does_not_claim_workflow_run_count(self):
        widget = next(w for w in management_widgets() if w.name == "Observation Volume")
        desc = widget.description.lower()
        assert "not native workflow-run count" in desc or "not native" in desc


class TestPlacementIdempotency:
    def test_extract_ids_from_nested_widget_object(self):
        ids = extract_placed_widget_ids([
            {"widgetId": "a"},
            {"widget": {"id": "b"}},
            {"widget_id": "c"},
        ])
        assert ids == {"a", "b", "c"}

    def test_second_apply_does_not_repost_existing_placement(self):
        spec = DashboardSpec(
            name="Test",
            description="",
            widgets=[WidgetSpec(
                name="W1", description="", view="observations",
                chart_type="NUMBER", dimensions=[], metrics=[{"measure": "count", "agg": "sum"}]
            )],
        )
        client = LangfuseClient("http://localhost:3000", "pk-test", "sk-test")
        with mock.patch.object(client, "find_dashboard_by_name", return_value={"id": "d1", "name": "Test"}):
            with mock.patch.object(client, "find_widget_by_name", return_value={"id": "w1", "name": "W1"}):
                with mock.patch.object(client, "update_widget", return_value={"id": "w1"}):
                    with mock.patch.object(
                        client, "get_dashboard_placements",
                        return_value=[{"widget": {"id": "w1"}}],
                    ):
                        with mock.patch.object(client, "add_placement") as add_pl:
                            result = provision_dashboard(client, spec, dry_run=False)
        add_pl.assert_not_called()
        assert result["placements"] == []
