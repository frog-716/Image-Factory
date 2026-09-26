"""Integration contracts, NOT claims about existing project's method names.
Sol must inspect real factory/lark.py and map these to its proven typed Gateway.
"""
from typing import Protocol

class FeishuPort(Protocol):
    def fetch_task(self, task_id: str) -> dict: ...
    def list_latest_reviews(self, task_id: str) -> list[dict]: ...
    def upload_and_verify(self, asset_id: str, path: str, operation_id: str) -> dict: ...
    def project_task_status(self, task_id: str, version: int, fields: dict) -> dict: ...

class WorkerPort(Protocol):
    def capabilities(self) -> dict: ...
    def submit(self, frozen_job: dict) -> dict: ...
    def reconcile(self, external_handle: str) -> dict: ...

class CapabilityUnavailable(RuntimeError):
    pass

class UnconfiguredNativeWorker:
    """Fail visibly until a real local Codex adapter has been verified."""
    def capabilities(self):
        return {'native_image':False,'headless_native_image':False,'verified':False}
    def submit(self, frozen_job):
        raise CapabilityUnavailable('Native worker not integrated; do not replace with a fake receipt')
