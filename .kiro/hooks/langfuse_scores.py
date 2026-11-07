"""Task3: Score submission to Langfuse API.

Handles score persistence with:
- Idempotent submission (dedup by workflow run + score name)
- Retry with exponential backoff
- Correlation to workflow run trace
- Safe transport that doesn't block IDE
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Score submission constants
SCORE_API_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds

# State directory for persistence
DEFAULT_STATE_DIR = Path.home() / ".kiro" / "langfuse-state"


@dataclass
class ScoreSubmission:
    """A score to submit to Langfuse."""
    trace_id: str
    name: str
    value: Any
    data_type: str  # NUMERIC, BOOLEAN, CATEGORICAL, TEXT
    comment: str | None = None
    metadata: dict | None = None
    observation_id: str | None = None

    def to_api_payload(self) -> dict:
        """Convert to Langfuse API payload format."""
        payload = {
            "traceId": self.trace_id,
            "name": self.name,
            "value": self.value,
            "dataType": self.data_type,
        }
        if self.comment:
            payload["comment"] = self.comment
        if self.metadata:
            payload["metadata"] = self.metadata
        if self.observation_id:
            payload["observationId"] = self.observation_id
        return payload

    def dedup_key(self) -> str:
        """Generate key for deduplication."""
        raw = f"{self.trace_id}:{self.name}:{self.value}:{self.data_type}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


class LangfuseScoreClient:
    """Client for submitting scores to Langfuse API.
    
    Uses POST /api/public/scores endpoint (stable, not deprecated).
    """

    def __init__(
        self,
        base_url: str,
        public_key: str,
        secret_key: str,
        state_dir: Path | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.state_dir = state_dir or DEFAULT_STATE_DIR
        self._submitted_keys: set[str] = set()
        self._load_submitted_keys()

    def _load_submitted_keys(self) -> None:
        """Load previously submitted score keys for deduplication."""
        path = self.state_dir / "submitted_scores.json"
        try:
            data = json.loads(path.read_text())
            # Keep only keys from last 24 hours
            cutoff = time.time() - 86400
            self._submitted_keys = {
                k for k, v in data.items()
                if isinstance(v, (int, float)) and v > cutoff
            }
        except (OSError, json.JSONDecodeError):
            self._submitted_keys = set()

    def _save_submitted_key(self, key: str) -> None:
        """Save a submitted key for deduplication."""
        self._submitted_keys.add(key)
        path = self.state_dir / "submitted_scores.json"
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            # Load existing, merge, save
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            data[key] = time.time()
            # Prune old entries (keep last 1000)
            if len(data) > 1000:
                sorted_items = sorted(data.items(), key=lambda x: x[1], reverse=True)
                data = dict(sorted_items[:1000])
            path.write_text(json.dumps(data))
        except OSError:
            pass  # Best effort

    def is_already_submitted(self, submission: ScoreSubmission) -> bool:
        """Check if a score was already submitted."""
        return submission.dedup_key() in self._submitted_keys

    def submit_scores(
        self,
        submissions: list[ScoreSubmission],
        skip_duplicates: bool = True,
    ) -> dict:
        """Submit scores to Langfuse.
        
        Returns dict with submitted count, skipped count, and any errors.
        """
        results = {
            "submitted": 0,
            "skipped": 0,
            "errors": [],
        }

        # Filter duplicates
        to_submit = []
        for sub in submissions:
            if skip_duplicates and self.is_already_submitted(sub):
                results["skipped"] += 1
            else:
                to_submit.append(sub)

        if not to_submit:
            return results

        # Build payload (API accepts array)
        payloads = [s.to_api_payload() for s in to_submit]

        # Submit with retry
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                self._post_scores(payloads)
                # Mark all as submitted
                for sub in to_submit:
                    self._save_submitted_key(sub.dedup_key())
                results["submitted"] = len(to_submit)
                return results
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_BASE ** attempt)

        results["errors"].append(str(last_error))
        return results

    def _post_scores(self, payloads: list[dict]) -> dict:
        """POST scores to Langfuse API."""
        url = f"{self.base_url}/api/public/scores"
        headers = {
            "Authorization": f"Basic {self.auth}",
            "Content-Type": "application/json",
        }
        body = json.dumps(payloads).encode()

        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=SCORE_API_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            raise RuntimeError(f"Score API error {e.code}: {error_body}") from e


def submit_objective_scores(
    results: dict,  # from run_all_checks
    trace_id: str,
    workflow_run_id: str | None,
    base_url: str,
    public_key: str,
    secret_key: str,
    state_dir: Path | None = None,
) -> dict:
    """Submit Task3 objective scores to Langfuse.
    
    Args:
        results: Dict from run_all_checks (build_pass, test_pass, lint_clean)
        trace_id: The trace to attach scores to
        workflow_run_id: Optional workflow run ID for metadata
        base_url: Langfuse server URL
        public_key: Langfuse public API key
        secret_key: Langfuse secret API key
        state_dir: Optional state directory for dedup
    
    Returns:
        Dict with submission results
    """
    from objective_scores import result_to_langfuse_score, CheckResult

    client = LangfuseScoreClient(base_url, public_key, secret_key, state_dir)

    submissions = []
    for name, result in results.items():
        if not isinstance(result, CheckResult):
            continue

        score_data = result_to_langfuse_score(result, trace_id, workflow_run_id)
        submissions.append(ScoreSubmission(
            trace_id=score_data["traceId"],
            name=score_data["name"],
            value=score_data["value"],
            data_type=score_data["dataType"],
            comment=score_data.get("comment"),
            metadata=score_data.get("metadata"),
        ))

    return client.submit_scores(submissions)


class PendingScoreQueue:
    """Persistent queue for scores that failed to submit.
    
    Used for retry/recovery when Langfuse is temporarily unavailable.
    """

    def __init__(self, state_dir: Path | None = None):
        self.state_dir = state_dir or DEFAULT_STATE_DIR
        self.queue_file = self.state_dir / "pending_scores.jsonl"

    def enqueue(self, submission: ScoreSubmission) -> None:
        """Add a score to the pending queue."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": time.time(),
            "payload": submission.to_api_payload(),
            "dedup_key": submission.dedup_key(),
        }
        with open(self.queue_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def get_pending(self, max_age_hours: int = 24) -> list[dict]:
        """Get pending scores not older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        pending = []
        try:
            for line in self.queue_file.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("timestamp", 0) > cutoff:
                        pending.append(entry)
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        return pending

    def clear_submitted(self, dedup_keys: set[str]) -> None:
        """Remove successfully submitted scores from queue."""
        pending = self.get_pending()
        remaining = [p for p in pending if p.get("dedup_key") not in dedup_keys]

        # Rewrite queue
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self.queue_file, "w") as f:
                for entry in remaining:
                    f.write(json.dumps(entry) + "\n")
        except OSError:
            pass


def retry_pending_scores(
    base_url: str,
    public_key: str,
    secret_key: str,
    state_dir: Path | None = None,
) -> dict:
    """Retry submitting any pending scores from the queue.
    
    Called periodically or at startup to recover from transient failures.
    """
    state_dir = state_dir or DEFAULT_STATE_DIR
    queue = PendingScoreQueue(state_dir)
    client = LangfuseScoreClient(base_url, public_key, secret_key, state_dir)

    pending = queue.get_pending()
    if not pending:
        return {"retried": 0, "succeeded": 0, "failed": 0}

    succeeded_keys = set()
    failed_count = 0

    for entry in pending:
        payload = entry.get("payload", {})
        dedup_key = entry.get("dedup_key")

        if not payload or not dedup_key:
            continue

        if dedup_key in client._submitted_keys:
            succeeded_keys.add(dedup_key)
            continue

        try:
            client._post_scores([payload])
            client._save_submitted_key(dedup_key)
            succeeded_keys.add(dedup_key)
        except Exception:
            failed_count += 1

    queue.clear_submitted(succeeded_keys)

    return {
        "retried": len(pending),
        "succeeded": len(succeeded_keys),
        "failed": failed_count,
    }
