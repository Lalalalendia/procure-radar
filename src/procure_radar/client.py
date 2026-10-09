from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import time
from typing import Any

import httpx


@dataclass(slots=True)
class GosplanClient:
    base_url: str = "https://v2test.gosplan.info"
    timeout: float = 30.0
    api_key: str | None = None
    max_retries: int = 4
    retry_backoff: float = 10.0

    def _request(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        headers = {"apikey": self.api_key} if self.api_key else None
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = client.get(endpoint, params=params)
                except httpx.RequestError:
                    if attempt >= self.max_retries:
                        raise
                    time.sleep(min(self.retry_backoff * (attempt + 1), 30.0))
                    continue

                if response.status_code == 429:
                    if attempt >= self.max_retries:
                        response.raise_for_status()
                    time.sleep(self._retry_delay(response, attempt))
                    continue

                if 500 <= response.status_code < 600:
                    if attempt >= self.max_retries:
                        response.raise_for_status()
                    time.sleep(min(self.retry_backoff * (attempt + 1), 30.0))
                    continue

                response.raise_for_status()
                return response
        raise RuntimeError("unreachable")

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return self._request(endpoint, params=params).json()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after)) + 0.25
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    now = parsedate_to_datetime(response.headers.get("Date")) if response.headers.get("Date") else None
                    if now is not None:
                        return max(0.0, (when - now).total_seconds()) + 0.25
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(self.retry_backoff * (attempt + 1), 30.0)

    @staticmethod
    def _rows_payload(endpoint: str, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            raise TypeError(f"Expected list response from {endpoint}, got {type(payload).__name__}")
        return [row for row in payload if isinstance(row, dict)]

    def get_rows(
        self,
        endpoint: str,
        *,
        limit: int = 10,
        skip: int = 0,
        extra: dict[str, str] | None = None,
        pagination_param: str = "skip",
    ) -> list[dict[str, Any]]:
        rows, _ = self.get_rows_with_total(
            endpoint,
            limit=limit,
            skip=skip,
            extra=extra,
            pagination_param=pagination_param,
        )
        return rows

    def get_rows_with_total(
        self,
        endpoint: str,
        *,
        limit: int = 10,
        skip: int = 0,
        extra: dict[str, str] | None = None,
        pagination_param: str = "skip",
    ) -> tuple[list[dict[str, Any]], int | None]:
        if pagination_param not in {"skip", "offset"}:
            raise ValueError("pagination_param must be 'skip' or 'offset'")
        params: dict[str, Any] = {"limit": limit, pagination_param: skip}
        if extra:
            params.update(extra)
        response = self._request(endpoint, params=params)
        rows = self._rows_payload(endpoint, response.json())
        total_raw = response.headers.get("x-total")
        try:
            total = int(total_raw) if total_raw is not None else None
        except ValueError:
            total = None
        return rows, total

    def get_purchases(
        self,
        *,
        limit: int = 10,
        skip: int = 0,
        extra: dict[str, str] | None = None,
        pagination_param: str = "skip",
    ) -> list[dict[str, Any]]:
        return self.get_rows(
            "/fz44/purchases",
            limit=limit,
            skip=skip,
            extra=extra,
            pagination_param=pagination_param,
        )

    def get_purchases_with_total(
        self,
        *,
        limit: int = 10,
        skip: int = 0,
        extra: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        return self.get_rows_with_total(
            "/fz44/purchases",
            limit=limit,
            skip=skip,
            extra=extra,
            pagination_param="skip",
        )

    def get_tenderplans(
        self,
        *,
        limit: int = 10,
        skip: int = 0,
        extra: dict[str, str] | None = None,
        pagination_param: str = "skip",
    ) -> list[dict[str, Any]]:
        return self.get_rows(
            "/fz44/tenderplans",
            limit=limit,
            skip=skip,
            extra=extra,
            pagination_param=pagination_param,
        )

    def get_tenderplans_with_total(
        self,
        *,
        limit: int = 10,
        skip: int = 0,
        extra: dict[str, str] | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        return self.get_rows_with_total(
            "/fz44/tenderplans",
            limit=limit,
            skip=skip,
            extra=extra,
            pagination_param="skip",
        )

    def get_tenderplan(self, plan_number: str) -> Any:
        return self.get_json(f"/fz44/tenderplans/{plan_number}")

    def get_purchase_protocols(self, purchase_number: str) -> Any:
        return self.get_json(f"/fz44/purchases/{purchase_number}/protocols")

    def get_purchase_result(self, purchase_number: str) -> Any:
        return self.get_json(f"/fz44/purchases/{purchase_number}/result")

    def get_purchase(self, purchase_number: str) -> Any:
        return self.get_json(f"/fz44/purchases/{purchase_number}")

    def get_contract(self, reg_num: str) -> Any:
        return self.get_json(f"/fz44/contracts/{reg_num}")
