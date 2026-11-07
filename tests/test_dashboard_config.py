#!/usr/bin/env python3
"""Tests for Task2 dashboard configuration and provisioning."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HOOKS = Path(__file__).resolve().parents[1] / ".kiro" / "hooks"
sys.path.insert(0, str(HOOKS))

from dashboard_config import (
    WidgetDefinition,
    WidgetFilter,
    DashboardDefinition,
    ChartType,
    DataSource,
    AggregationType,
    management_widgets,
    engineering_widgets,
    get_management_dashboard,
    get_engineering_dashboard,
    export_dashboards_json,
    export_widgets_json,
    LangfuseDashboardClient,
    provision_dashboards,
    WIDGET_EXPORT_VERSION,
)


class TestWidgetDefinition(unittest.TestCase):
    """Test widget definition and export."""
    
    def test_basic_widget(self):
        widget = WidgetDefinition(
            name="Test Widget",
            description="A test widget",
            data_source=DataSource.TRACES,
            chart_type=ChartType.LINE,
            metric="traceId",
            aggregation=AggregationType.COUNT,
        )
        self.assertEqual(widget.name, "Test Widget")
        self.assertEqual(widget.data_source, DataSource.TRACES)
    
    def test_widget_to_json(self):
        widget = WidgetDefinition(
            name="Test Widget",
            description="A test widget",
            data_source=DataSource.TRACES,
            chart_type=ChartType.BAR,
            metric="userId",
            aggregation=AggregationType.COUNT_DISTINCT,
            breakdown_dimension="metadata.team",
            filters=[
                WidgetFilter("tags", "contains", "kiro"),
            ],
        )
        json_data = widget.to_json()
        
        self.assertTrue(json_data["$langfuseWidget"])
        self.assertEqual(json_data["version"], WIDGET_EXPORT_VERSION)
        self.assertEqual(json_data["name"], "Test Widget")
        self.assertEqual(json_data["config"]["dataSource"], "traces")
        self.assertEqual(json_data["config"]["chartType"], "bar")
        self.assertEqual(json_data["config"]["aggregation"], "countDistinct")
        self.assertEqual(len(json_data["config"]["filters"]), 1)
    
    def test_widget_json_serializable(self):
        widget = WidgetDefinition(
            name="Test",
            description="Test",
            data_source=DataSource.OBSERVATIONS,
            chart_type=ChartType.TABLE,
            metric="latency",
            aggregation=AggregationType.P95,
        )
        # Should not raise
        json_str = json.dumps(widget.to_json())
        self.assertIn("Test", json_str)


class TestDashboardDefinition(unittest.TestCase):
    """Test dashboard definition and export."""
    
    def test_basic_dashboard(self):
        dashboard = DashboardDefinition(
            name="Test Dashboard",
            description="A test dashboard",
            widgets=[],
        )
        self.assertEqual(dashboard.name, "Test Dashboard")
    
    def test_dashboard_to_json(self):
        widget1 = WidgetDefinition(
            name="Widget 1",
            description="First widget",
            data_source=DataSource.TRACES,
            chart_type=ChartType.LINE,
            metric="traceId",
            aggregation=AggregationType.COUNT,
        )
        widget2 = WidgetDefinition(
            name="Widget 2",
            description="Second widget",
            data_source=DataSource.SCORES,
            chart_type=ChartType.PIE,
            metric="scoreId",
            aggregation=AggregationType.COUNT,
        )
        
        dashboard = DashboardDefinition(
            name="Test Dashboard",
            description="Test description",
            widgets=[widget1, widget2],
        )
        json_data = dashboard.to_json()
        
        self.assertTrue(json_data["$langfuseDashboard"])
        self.assertEqual(json_data["version"], WIDGET_EXPORT_VERSION)
        self.assertEqual(json_data["name"], "Test Dashboard")
        self.assertEqual(len(json_data["widgets"]), 2)
        self.assertEqual(len(json_data["layout"]), 2)
        
        # Check layout has correct structure
        for i, layout in enumerate(json_data["layout"]):
            self.assertEqual(layout["widgetIndex"], i)
            self.assertIn("x", layout)
            self.assertIn("y", layout)
            self.assertIn("width", layout)
            self.assertIn("height", layout)


class TestManagementWidgets(unittest.TestCase):
    """Test management dashboard widget definitions."""
    
    def test_widget_count(self):
        widgets = management_widgets()
        self.assertGreater(len(widgets), 5)  # At least 6 widgets expected
    
    def test_required_widgets_present(self):
        widgets = management_widgets()
        names = [w.name for w in widgets]
        
        self.assertIn("Weekly Active Engineers", names)
        self.assertIn("Workflow Runs Per Week", names)
        self.assertIn("Completion Rate by Status", names)
        self.assertIn("Adoption by Team", names)
        self.assertIn("Top Workflows by Usage", names)
    
    def test_automation_exclusion_filter(self):
        """Adoption exclusion is identity-tag based, not a hardcoded username."""
        widgets = management_widgets()
        active_users = next(w for w in widgets if "Active Engineers" in w.name)

        filter_values = [f.value for f in active_users.filters]
        self.assertIn("kiro-automation", filter_values)
        self.assertNotIn("chomu-local", filter_values)
        self.assertNotIn("kiro-test-runner", filter_values)
    
    def test_workflow_run_dedup(self):
        """Verify workflow runs use run_id for deduplication."""
        widgets = management_widgets()
        runs_widget = next(w for w in widgets if "Workflow Runs" in w.name)
        
        self.assertEqual(runs_widget.metric, "metadata.workflow.run_id")
        self.assertEqual(runs_widget.aggregation, AggregationType.COUNT_DISTINCT)


class TestEngineeringWidgets(unittest.TestCase):
    """Test engineering dashboard widget definitions."""
    
    def test_widget_count(self):
        widgets = engineering_widgets()
        self.assertGreater(len(widgets), 5)  # At least 6 widgets expected
    
    def test_required_widgets_present(self):
        widgets = engineering_widgets()
        names = [w.name for w in widgets]
        
        self.assertIn("Completion by Workflow Version", names)
        self.assertIn("Tool Error Rate by Tool", names)
        self.assertIn("Tool Latency p50", names)
        self.assertIn("Tool Latency p95", names)
        self.assertIn("Tool Calls Per Workflow Run", names)
    
    def test_latency_caveat_in_description(self):
        """Verify latency widgets document the timing caveat."""
        widgets = engineering_widgets()
        latency_widgets = [w for w in widgets if "Latency" in w.name]
        
        for widget in latency_widgets:
            self.assertIn("0", widget.description.lower())  # Shows 0 when no genuine timing
    
    def test_task3_scores_widget(self):
        """Verify Task3 objective scores are included."""
        widgets = engineering_widgets()
        score_widgets = [w for w in widgets if "Objective" in w.name or "Pass Rate" in w.name]
        
        self.assertGreater(len(score_widgets), 0)


class TestGetDashboards(unittest.TestCase):
    """Test dashboard factory functions."""
    
    def test_management_dashboard(self):
        dashboard = get_management_dashboard()
        
        self.assertIn("Management", dashboard.name)
        self.assertIn("Task2", dashboard.description)
        self.assertEqual(len(dashboard.widgets), len(management_widgets()))
    
    def test_engineering_dashboard(self):
        dashboard = get_engineering_dashboard()
        
        self.assertIn("Engineering", dashboard.name)
        self.assertIn("Task2", dashboard.description)
        self.assertEqual(len(dashboard.widgets), len(engineering_widgets()))


class TestExportFunctions(unittest.TestCase):
    """Test JSON export functions."""
    
    def test_export_dashboards(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "dashboards"
            exports = export_dashboards_json(output_dir)
            
            self.assertEqual(len(exports), 2)
            
            for name, path in exports.items():
                self.assertTrue(path.exists())
                data = json.loads(path.read_text())
                self.assertTrue(data["$langfuseDashboard"])
    
    def test_export_widgets(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "widgets"
            exports = export_widgets_json(output_dir)
            
            total_widgets = len(management_widgets()) + len(engineering_widgets())
            self.assertEqual(len(exports), total_widgets)
            
            for name, path in exports.items():
                self.assertTrue(path.exists())
                data = json.loads(path.read_text())
                self.assertTrue(data["$langfuseWidget"])


class TestLangfuseDashboardClient(unittest.TestCase):
    """Test dashboard API client."""
    
    def test_client_initialization(self):
        client = LangfuseDashboardClient(
            base_url="http://localhost:3000",
            public_key="pk-lf-test",
            secret_key="sk-lf-test",
        )
        self.assertEqual(client.base_url, "http://localhost:3000")
    
    def test_url_trailing_slash_stripped(self):
        client = LangfuseDashboardClient(
            base_url="http://localhost:3000/",
            public_key="pk-lf-test",
            secret_key="sk-lf-test",
        )
        self.assertEqual(client.base_url, "http://localhost:3000")
    
    @patch("urllib.request.urlopen")
    def test_check_api_available(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b'{"data": []}'
        mock_urlopen.return_value = mock_response
        
        client = LangfuseDashboardClient(
            base_url="http://localhost:3000",
            public_key="pk-lf-test",
            secret_key="sk-lf-test",
        )
        available, message = client.check_api_available()
        
        self.assertTrue(available)


class TestProvisionDashboards(unittest.TestCase):
    """Test dashboard provisioning."""
    
    def test_provisioning_disabled(self):
        """provision_dashboards now returns disabled status due to schema mismatch."""
        result = provision_dashboards(
            base_url="http://localhost:3000",
            public_key="pk-lf-test",
            secret_key="sk-lf-test",
            dry_run=True,
        )
        
        # Provisioning is now disabled - schema mismatch documented
        self.assertFalse(result["success"])
        self.assertIn("schema mismatch", result["message"])
        self.assertIn("langfuse-cli", result["recommendation"])
    
    def test_schema_mismatch_documented(self):
        """Verify provisioning includes schema mismatch documentation."""
        result = provision_dashboards(
            base_url="http://localhost:3000",
            public_key="pk-lf-test",
            secret_key="sk-lf-test",
            dry_run=False,  # Even without dry_run, should fail
        )
        
        self.assertFalse(result["success"])
        self.assertIn("docs", result)
        self.assertIn("langfuse-cli", result["docs"])


class TestWorkflowRunDeduplication(unittest.TestCase):
    """Test that dashboard configs properly handle workflow run deduplication."""
    
    def test_distinct_run_id_aggregation(self):
        """All workflow-level metrics should use COUNT_DISTINCT on run_id."""
        widgets = management_widgets()
        
        workflow_widgets = [
            w for w in widgets 
            if "workflow" in w.name.lower() and "run" in w.name.lower()
        ]
        
        for widget in workflow_widgets:
            if "run_id" in widget.metric:
                self.assertEqual(
                    widget.aggregation,
                    AggregationType.COUNT_DISTINCT,
                    f"Widget '{widget.name}' should use COUNT_DISTINCT for run dedup"
                )
    
    def test_no_double_counting_parent_step(self):
        """Verify widgets filter to workflow steps only (not parent chats)."""
        all_widgets = management_widgets() + engineering_widgets()
        
        workflow_widgets = [
            w for w in all_widgets 
            if any(f.value == "kiro-workflow-step" for f in w.filters if hasattr(f, 'value'))
        ]
        
        # Most workflow widgets should have this filter
        self.assertGreater(len(workflow_widgets), 5)


class TestCompletionStatusMapping(unittest.TestCase):
    """Test that completion status is properly handled."""
    
    def test_completion_widget_exists(self):
        widgets = management_widgets()
        completion_widgets = [w for w in widgets if "completion" in w.name.lower()]
        
        self.assertGreater(len(completion_widgets), 0)
    
    def test_failed_traces_widget(self):
        widgets = engineering_widgets()
        failed_widgets = [w for w in widgets if "failed" in w.name.lower()]
        
        self.assertGreater(len(failed_widgets), 0)
        
        for widget in failed_widgets:
            # Should filter to error/aborted
            error_filters = [
                f for f in widget.filters 
                if "stop_reason" in f.column and isinstance(f.value, list)
            ]
            self.assertGreater(len(error_filters), 0)


if __name__ == "__main__":
    unittest.main()
