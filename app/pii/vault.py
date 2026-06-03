"""
PII 플레이스홀더 ↔ 원본값 매핑 저장소.
운영 환경에서는 HashiCorp Vault / AWS Secrets Manager 등으로 교체한다.
"""
from __future__ import annotations

import threading
from typing import Optional


class PiiVault:
    """스레드 안전 인메모리 볼트 (PoC용). 운영 시 외부 볼트로 교체."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, placeholder: str, original: str) -> None:
        with self._lock:
            self._store[placeholder] = original

    def get(self, placeholder: str) -> Optional[str]:
        with self._lock:
            return self._store.get(placeholder)

    def restore(self, text: str) -> str:
        with self._lock:
            for ph, orig in self._store.items():
                text = text.replace(ph, orig)
        return text

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
