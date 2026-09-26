"""Exact versioned path profiles: executable integration contracts.

A path profile names what is being tested, the exact request, the
expected observable result, the controlled condition, and the limits.
It is frozen and fingerprinted; a result binds to the exact profile
version it ran under. These fixtures are Town-authored integration
tests, not universal commerce standards.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .records import fingerprint

PATH_EVALUATOR = "path-evaluator@0.1"
STRICT_PATH_EVALUATOR = "path-evaluator@0.2"
QUOTE_INTENT_EVALUATOR = "quote-intent-evaluator@0.1"
STRICT_QUOTE_INTENT_EVALUATOR = "quote-intent-evaluator@0.2"
CITY_PATH_EVALUATOR = "city-a2a-protocol-evaluator@0.1"
QUOTE_INTENT_FIELDS = ("sku", "color", "quantity", "merchant_id", "currency")


class PathProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_id: str
    version: str
    protocol: Literal["a2a"]
    capability: str
    request: dict[str, Any]
    expected: dict[str, Any]
    controlled_condition: Literal["duplicate_request"]
    limits: dict[str, float]
    evaluator: str

    @property
    def ref(self) -> str:
        return f"{self.profile_id}@{self.version}"

    def fingerprint(self) -> str:
        return fingerprint(self.model_dump())


PATH_PROFILES: dict[str, PathProfile] = {
    "city-a2a-protocol@0.1": PathProfile(
        profile_id="city-a2a-protocol", version="0.1", protocol="a2a",
        capability="city-a2a-structured-task",
        request={"source": "manifest-bound synthetic signed JSON-RPC bytes",
                 "method": "message/send", "retry": "identical bytes"},
        expected={"protocol_version": "0.3.0",
                  "request_type": "org.nandacity.city-request",
                  "result_type": "org.nandacity.city-result",
                  "required_stages": ["pinned_card", "structured_send",
                                      "acceptance_task", "exact_retry",
                                      "terminal_task"]},
        controlled_condition="duplicate_request",
        limits={"run_seconds": 30.0, "response_seconds": 5.0,
                "max_card_bytes": 65_536, "max_message_bytes": 1_048_576,
                "max_http_bytes": 4_194_304, "max_polls": 20,
                "max_json_depth": 32},
        evaluator=CITY_PATH_EVALUATOR,
    ),
    "a2a-quote-intent@0.1": PathProfile(
        profile_id="a2a-quote-intent",
        version="0.1",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2, "unit_price_cents": 1995,
                 "color": "blue", "merchant_id": "town-reference", "currency": "USD"},
        expected={"max_total_cents": 3990,
                  "quote": {"sku": "widget", "quantity": 2, "color": "blue",
                            "merchant_id": "town-reference", "currency": "USD"}},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0, "max_response_bytes": 1_048_576},
        evaluator=QUOTE_INTENT_EVALUATOR,
    ),
    "a2a-quote-intent@0.2": PathProfile(
        profile_id="a2a-quote-intent",
        version="0.2",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2, "unit_price_cents": 1995,
                 "color": "blue", "merchant_id": "town-reference",
                 "currency": "USD"},
        expected={"max_total_cents": 3990, "terminal_fulfillments": 1,
                  "quote": {"sku": "widget", "quantity": 2,
                            "color": "blue",
                            "merchant_id": "town-reference",
                            "currency": "USD"}},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0,
                "max_response_bytes": 1_048_576},
        evaluator=STRICT_QUOTE_INTENT_EVALUATOR,
    ),
    "a2a-capability-fulfillment@0.1": PathProfile(
        profile_id="a2a-capability-fulfillment",
        version="0.1",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2,
                 "unit_price_cents": 1995},
        expected={"total_cents": 3990, "terminal_fulfillments": 1},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0},
        evaluator=PATH_EVALUATOR,
    ),
    "a2a-capability-fulfillment@0.2": PathProfile(
        profile_id="a2a-capability-fulfillment",
        version="0.2",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2,
                 "unit_price_cents": 1995},
        expected={"total_cents": 3990, "terminal_fulfillments": 1},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0, "max_response_bytes": 1_048_576},
        evaluator=PATH_EVALUATOR,
    ),
    "a2a-capability-fulfillment@0.3": PathProfile(
        profile_id="a2a-capability-fulfillment",
        version="0.3",
        protocol="a2a",
        capability="quote",
        request={"sku": "widget", "quantity": 2,
                 "unit_price_cents": 1995},
        expected={"total_cents": 3990, "terminal_fulfillments": 1},
        controlled_condition="duplicate_request",
        limits={"timeout_seconds": 15.0,
                "max_response_bytes": 1_048_576},
        evaluator=STRICT_PATH_EVALUATOR,
    ),
}

DEFAULT_PATH_PROFILE = "a2a-capability-fulfillment@0.3"


def get_path_profile(ref: str) -> PathProfile:
    if ref not in PATH_PROFILES:
        raise KeyError(f"no path profile {ref!r};"
                       f" available: {sorted(PATH_PROFILES)}")
    return PATH_PROFILES[ref]
