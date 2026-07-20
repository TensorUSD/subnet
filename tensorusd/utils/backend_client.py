"""
All HTTP calls from the validator to the TensorUSD backend, in one place.
"""

from __future__ import annotations

from typing import Any

import bittensor as bt
import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from tensorusd.auth.auth import fetch_validator_nonce, sign_message
from tensorusd.auth.config import settings
from tensorusd.utils.logging import get_logger

log = get_logger(__name__)

# Retry on transient network errors only — never retry 4xx
_RETRY = {
    "retry": retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    "stop": stop_after_attempt(4),
    "wait": wait_exponential(multiplier=1, min=2, max=30),
    "reraise": True,
}


def _log_http_error(exc: requests.HTTPError, context: str) -> None:
    """Log the full response body on HTTP errors so we can see Pydantic detail."""
    status = exc.response.status_code if exc.response is not None else "?"
    try:
        body = exc.response.json() if exc.response is not None else str(exc)
    except Exception:
        body = exc.response.text if exc.response is not None else str(exc)
    log.error("%s failed (HTTP %s): %s", context, status, body)


class BackendClient:
    """
    Thin HTTP client for the TensorUSD backend.
    """

    def __init__(self, wallet: bt.Wallet) -> None:
        self._wallet = wallet
        self._base = settings.backend_url
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "tensorusd-validator/0.1"

    # Internal helpers

    def _fresh_auth_post(self) -> dict[str, str]:
        """
        Auth dict for POST request bodies.
        """
        nonce_data = fetch_validator_nonce(self._wallet.hotkey.ss58_address)
        signature = sign_message(self._wallet, nonce_data["message_to_sign"])
        return {
            "validator_hotkey": self._wallet.hotkey.ss58_address,
            "nonce": nonce_data["nonce"],
            "signature": signature,
        }

    def _fresh_auth_get(self) -> dict[str, str]:
        """
        Auth dict for GET query params.
        """
        nonce_data = fetch_validator_nonce(self._wallet.hotkey.ss58_address)
        signature = sign_message(self._wallet, nonce_data["message_to_sign"])
        return {
            "hotkey": self._wallet.hotkey.ss58_address,
            "nonce": nonce_data["nonce"],
            "signature": signature,
        }

    @retry(**_RETRY)
    def _get(self, path: str, **kwargs: Any) -> requests.Response:
        r = self._session.get(
            f"{self._base}{path}",
            timeout=settings.http_timeout,
            **kwargs,
        )
        r.raise_for_status()
        return r

    @retry(**_RETRY)
    def _post(self, path: str, **kwargs: Any) -> requests.Response:
        r = self._session.post(
            f"{self._base}{path}",
            timeout=settings.http_timeout,
            **kwargs,
        )
        r.raise_for_status()
        return r

    # Submission queue

    def get_unevaluated_submission(self) -> dict | None:
        """GET /v1/validator/submissions/unevaluated"""
        try:
            r = self._get(
                "/v1/validator/submissions/unevaluated", params=self._fresh_auth_get()
            )
            body = r.json()
            data = (
                body.get("data") if isinstance(body, dict) and "data" in body else body
            )
            log.debug("Unevaluated submission response: %s", data)
            return data or None
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            if exc.response is not None and exc.response.status_code == 403:
                log.error(
                    "Validator hotkey has no permit — cannot authenticate with backend.\n"
                    "  This hotkey is not recognised as a validator on netuid %d.",
                    settings.netuid,
                )
                raise
            _log_http_error(exc, "get_unevaluated_submission")
            return None

    def download_submission_file(self, submission_id: str) -> bytes:
        """GET /v1/validator/submissions/{submission_id}/file"""
        r = self._get(
            f"/v1/validator/submissions/{submission_id}/file",
            params=self._fresh_auth_get(),
            stream=True,
        )
        return r.content

    # Best-agent

    def get_best_submission_meta(self) -> dict | None:
        """GET /v1/validator/leaderboard/best"""
        try:
            r = self._get(
                "/v1/validator/leaderboard/best", params=self._fresh_auth_get()
            )
            body = r.json()
            log.debug("Leaderboard best raw response: %s", body)
            if isinstance(body, dict):
                data = body.get("data") or body.get("submission") or body
                return (
                    data
                    if data and isinstance(data, dict) and data.get("submission_id")
                    else None
                )
            return None
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            _log_http_error(exc, "get_best_submission_meta")
            return None

    def download_best_agent_file(self) -> bytes:
        """GET /v1/validator/submissions/best"""
        r = self._get(
            "/v1/validator/submissions/best",
            params=self._fresh_auth_get(),
            stream=True,
        )
        return r.content

    # Agent output upload

    def upload_agent_output(
        self,
        csv_bytes: bytes,
        agent_filename: str,
        submission_id: str,
        csv_filename: str | None = None,
    ) -> dict:
        from pathlib import Path

        auth = self._fresh_auth_post()

        derived_name = csv_filename or f"{Path(agent_filename).stem}.csv"
        files = {
            "file": (derived_name, csv_bytes, "text/csv"),
        }
        data = {
            **auth,
            "agent_filename": agent_filename,
            "submission_id": submission_id,
            "validator_hotkey": self._wallet.hotkey.ss58_address,
        }

        try:
            r = self._session.post(
                f"{self._base}/v1/validator/agent-output",
                files=files,
                data=data,
                timeout=settings.http_timeout,
            )
            r.raise_for_status()
            body = r.json()
            payload = (
                body.get("data") if isinstance(body, dict) and "data" in body else body
            )
            log.info(
                "Uploaded agent output CSV — file_id=%s  eval_date=%s",
                payload.get("file_id", "?"),
                payload.get("eval_date", "?"),
            )
            return payload
        except requests.HTTPError as exc:
            _log_http_error(exc, "upload_agent_output")
            raise

    # Ground truth

    def download_ground_truth(self, eval_date: str | None = None) -> bytes:
        """
        GET /v1/validator/benchmark/ground-truth

        If *eval_date* is provided (ISO-8601 date string), streams the
        per-eval-date ground-truth CSV. Otherwise streams the default file.
        """
        params = self._fresh_auth_get()
        if eval_date:
            params["eval_date"] = eval_date
        r = self._get(
            "/v1/validator/benchmark/ground-truth",
            params=params,
            stream=True,
        )
        return r.content

    # Unscored submissions (Phase 2 scoring)

    def get_unscored_submission(self) -> dict | None:
        """
        GET /v1/validator/submissions/unscored

        Claim and return the oldest unscored (but evaluated) submission
        whose evaluation window has matured.
        """
        try:
            r = self._get(
                "/v1/validator/submissions/unscored",
                params=self._fresh_auth_get(),
            )
            body = r.json()
            data = (
                body.get("data") if isinstance(body, dict) and "data" in body else body
            )
            log.debug("Unscored submission response: %s", data)
            return data or None
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            _log_http_error(exc, "get_unscored_submission")
            return None

    def download_agent_output_csv(self, submission_id: str) -> bytes:
        """
        GET /v1/validator/submissions/{submission_id}/output

        Stream the stored agent output CSV for a given submission from MinIO.
        """
        r = self._get(
            f"/v1/validator/submissions/{submission_id}/output",
            params=self._fresh_auth_get(),
            stream=True,
        )
        return r.content

    # Mark evaluated

    def mark_submission_evaluated(self, submission_id: str) -> None:
        """
        POST /v1/validator/submissions/{submission_id}/mark-evaluated

        Tell the backend that Phase 1 sandbox evaluation is complete and the
        output CSV has been stored.  This flips ``evaluated=True`` so that
        the submission becomes eligible for Phase 2 delayed scoring.
        """
        auth = self._fresh_auth_post()
        try:
            self._post(
                f"/v1/validator/submissions/{submission_id}/mark-evaluated",
                json={**auth},
            )
            log.info(
                "Marked submission %s as evaluated (Phase 1 complete).", submission_id
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                log.warning(
                    "Submission %s not found — cannot mark evaluated.", submission_id
                )
            else:
                _log_http_error(exc, "mark_submission_evaluated")
                raise

    # Scoring

    def post_score(self, submission_id: str, score: float) -> None:
        """
        POST /v1/validator/scores
        """
        payload = {
            **self._fresh_auth_post(),
            "submission_id": submission_id,
            "score": score,
        }
        log.debug("post_score payload keys: %s", list(payload.keys()))
        try:
            self._post("/v1/validator/scores", json=payload)
            log.info("Posted score %.4f for submission %s", score, submission_id)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                log.debug(
                    "Submission %s already scored by this validator.", submission_id
                )
            else:
                _log_http_error(exc, "post_score")
                raise

    # Blacklist

    def blacklist_miner(
        self, miner_hotkey: str, submission_id: str, reason: str
    ) -> None:
        """
        POST /v1/validator/blacklist
        """
        payload = {
            **self._fresh_auth_post(),
            "miner_hotkey": miner_hotkey,
            "submission_id": submission_id,
            "reason": reason,
        }
        log.debug("blacklist_miner payload keys: %s", list(payload.keys()))
        try:
            self._post("/v1/validator/blacklist", json=payload)
            log.warning(
                "Blacklisted miner %s for submission %s — reason: %s",
                miner_hotkey,
                submission_id,
                reason,
            )
        except requests.HTTPError as exc:
            _log_http_error(exc, "blacklist_miner")
            raise

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> BackendClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
