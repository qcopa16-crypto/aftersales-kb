"""Circuit isolation: document-level MinerU faults must not open the global circuit."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from ai.ingest.mineru_client import _DOC_FAULT_CODES, _NON_HEALTH_CODES  # noqa: E402
from core.circuit import CircuitBreaker  # noqa: E402


def test_doc_fault_codes_are_content_level() -> None:
    assert "MINERU_EXTRACT_FAILED" in _DOC_FAULT_CODES
    assert "MINERU_EMPTY_MD" in _DOC_FAULT_CODES
    assert "MINERU_BAD_FILE" in _DOC_FAULT_CODES
    assert "MINERU_BAD_ZIP" in _DOC_FAULT_CODES
    assert not (_DOC_FAULT_CODES & _NON_HEALTH_CODES)


def test_doc_faults_do_not_open_circuit_when_skipped() -> None:
    """Mirror mineru_client policy: only service faults call record_failure."""
    br = CircuitBreaker(failure_threshold=3, cooldown_sec=60)
    service_only = {"MINERU_UPLOAD_FAILED", "MINERU_UPLOAD_URL_FAILED"}

    def handle(code: str) -> None:
        if code not in _NON_HEALTH_CODES and code not in _DOC_FAULT_CODES:
            br.record_failure("mineru", code)

    for _ in range(5):
        handle("MINERU_EXTRACT_FAILED")
    assert br.is_open("mineru") is False

    for code in service_only:
        handle(code)
    assert br.is_open("mineru") is False
    handle("MINERU_UPLOAD_FAILED")
    assert br.is_open("mineru") is True


def test_service_faults_still_trip() -> None:
    br = CircuitBreaker(failure_threshold=2, cooldown_sec=60)
    for _ in range(2):
        br.record_failure("mineru", "MINERU_UPLOAD_FAILED")
    assert br.is_open("mineru") is True


if __name__ == "__main__":
    test_doc_fault_codes_are_content_level()
    test_doc_faults_do_not_open_circuit_when_skipped()
    test_service_faults_still_trip()
    print("ok")
