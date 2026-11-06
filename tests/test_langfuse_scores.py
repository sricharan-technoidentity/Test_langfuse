#!/usr/bin/env python3
"""Tests for Task3 Langfuse score submission."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

HOOKS = Path(__file__).resolve().parents[1] / ".kiro" / "hooks"
sys.path.insert(0, str(HOOKS))

from langfuse_scores import (
    ScoreSubmission,
    LangfuseScoreClient,
    PendingScoreQueue,
    retry_pending_scores,
)


class TestScoreSubmission(unittest.TestCase):
    """Test score submission data class."""
    
    def test_basic_submission(self):
        sub = ScoreSubmission(
            trace_id="trace123",
            name="build_pass",
            value="pass",
            data_type="CATEGORICAL",
        )
        self.assertEqual(sub.trace_id, "trace123")
        self.assertEqual(sub.name, "build_pass")
    
    def test_to_api_payload(self):
        sub = ScoreSubmission(
            trace_id="trace123",
            name="test_pass",
            value="fail",
            data_type="CATEGORICAL",
            comment="Tests failed",
            metadata={"exit_code": 1},
        )
        payload = sub.to_api_payload()
        
        self.assertEqual(payload["traceId"], "trace123")
        self.assertEqual(payload["name"], "test_pass")
        self.assertEqual(payload["value"], "fail")
        self.assertEqual(payload["dataType"], "CATEGORICAL")
        self.assertEqual(payload["comment"], "Tests failed")
        self.assertEqual(payload["metadata"]["exit_code"], 1)
    
    def test_dedup_key_deterministic(self):
        sub = ScoreSubmission(
            trace_id="trace123",
            name="build_pass",
            value="pass",
            data_type="CATEGORICAL",
        )
        key1 = sub.dedup_key()
        key2 = sub.dedup_key()
        self.assertEqual(key1, key2)
    
    def test_dedup_key_different_values(self):
        sub1 = ScoreSubmission(
            trace_id="trace123",
            name="build_pass",
            value="pass",
            data_type="CATEGORICAL",
        )
        sub2 = ScoreSubmission(
            trace_id="trace123",
            name="build_pass",
            value="fail",
            data_type="CATEGORICAL",
        )
        self.assertNotEqual(sub1.dedup_key(), sub2.dedup_key())


class TestLangfuseScoreClient(unittest.TestCase):
    """Test Langfuse score client."""
    
    def test_client_initialization(self):
        with tempfile.TemporaryDirectory() as td:
            client = LangfuseScoreClient(
                base_url="http://localhost:3000",
                public_key="pk-lf-test",
                secret_key="sk-lf-test",
                state_dir=Path(td),
            )
            self.assertEqual(client.base_url, "http://localhost:3000")
    
    def test_dedup_detection(self):
        with tempfile.TemporaryDirectory() as td:
            client = LangfuseScoreClient(
                base_url="http://localhost:3000",
                public_key="pk-lf-test",
                secret_key="sk-lf-test",
                state_dir=Path(td),
            )
            
            sub = ScoreSubmission(
                trace_id="trace123",
                name="test",
                value="pass",
                data_type="CATEGORICAL",
            )
            
            self.assertFalse(client.is_already_submitted(sub))
            
            # Manually mark as submitted
            client._save_submitted_key(sub.dedup_key())
            
            self.assertTrue(client.is_already_submitted(sub))
    
    @patch("urllib.request.urlopen")
    def test_submit_scores_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_response.read.return_value = b'{"success": true}'
        mock_urlopen.return_value = mock_response
        
        with tempfile.TemporaryDirectory() as td:
            client = LangfuseScoreClient(
                base_url="http://localhost:3000",
                public_key="pk-lf-test",
                secret_key="sk-lf-test",
                state_dir=Path(td),
            )
            
            submissions = [
                ScoreSubmission(
                    trace_id="trace123",
                    name="build_pass",
                    value="pass",
                    data_type="CATEGORICAL",
                ),
            ]
            
            result = client.submit_scores(submissions)
            
            self.assertEqual(result["submitted"], 1)
            self.assertEqual(result["skipped"], 0)
            self.assertEqual(result["errors"], [])
    
    @patch("urllib.request.urlopen")
    def test_skip_duplicates(self, mock_urlopen):
        with tempfile.TemporaryDirectory() as td:
            client = LangfuseScoreClient(
                base_url="http://localhost:3000",
                public_key="pk-lf-test",
                secret_key="sk-lf-test",
                state_dir=Path(td),
            )
            
            sub = ScoreSubmission(
                trace_id="trace123",
                name="test",
                value="pass",
                data_type="CATEGORICAL",
            )
            
            # Pre-mark as submitted
            client._save_submitted_key(sub.dedup_key())
            
            result = client.submit_scores([sub])
            
            self.assertEqual(result["submitted"], 0)
            self.assertEqual(result["skipped"], 1)
            mock_urlopen.assert_not_called()


class TestPendingScoreQueue(unittest.TestCase):
    """Test pending score queue for retry."""
    
    def test_enqueue_and_retrieve(self):
        with tempfile.TemporaryDirectory() as td:
            queue = PendingScoreQueue(Path(td))
            
            sub = ScoreSubmission(
                trace_id="trace123",
                name="test",
                value="pass",
                data_type="CATEGORICAL",
            )
            
            queue.enqueue(sub)
            
            pending = queue.get_pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["payload"]["traceId"], "trace123")
    
    def test_clear_submitted(self):
        with tempfile.TemporaryDirectory() as td:
            queue = PendingScoreQueue(Path(td))
            
            sub1 = ScoreSubmission(
                trace_id="trace1",
                name="test",
                value="pass",
                data_type="CATEGORICAL",
            )
            sub2 = ScoreSubmission(
                trace_id="trace2",
                name="test",
                value="fail",
                data_type="CATEGORICAL",
            )
            
            queue.enqueue(sub1)
            queue.enqueue(sub2)
            
            # Clear one
            queue.clear_submitted({sub1.dedup_key()})
            
            pending = queue.get_pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["payload"]["traceId"], "trace2")
    
    def test_old_entries_filtered(self):
        with tempfile.TemporaryDirectory() as td:
            queue = PendingScoreQueue(Path(td))
            
            # Write an old entry directly
            old_entry = {
                "timestamp": time.time() - 100000,  # > 24 hours ago
                "payload": {"traceId": "old"},
                "dedup_key": "old_key",
            }
            queue.state_dir.mkdir(parents=True, exist_ok=True)
            with open(queue.queue_file, "w") as f:
                f.write(json.dumps(old_entry) + "\n")
            
            pending = queue.get_pending(max_age_hours=24)
            self.assertEqual(len(pending), 0)


class TestRetryPendingScores(unittest.TestCase):
    """Test retry mechanism."""
    
    @patch("urllib.request.urlopen")
    def test_retry_empty_queue(self, mock_urlopen):
        with tempfile.TemporaryDirectory() as td:
            result = retry_pending_scores(
                base_url="http://localhost:3000",
                public_key="pk-lf-test",
                secret_key="sk-lf-test",
                state_dir=Path(td),
            )
            
            self.assertEqual(result["retried"], 0)
            self.assertEqual(result["succeeded"], 0)
            mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
