"""Offline contracts for backend substitution, resident loading, and comparison accounting."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jev_ultrafast import laya_backend, model
from scripts.compare import summarize

CASES = json.loads((Path(__file__).parents[1] / "benchmarks" / "decisions.json").read_text())


def answer(criteria, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {k: float(k == selected) for k in criteria}}


@pytest.mark.parametrize("base,expected_url", [
    (None, "https://api.typesafe.ai/v1/systemone"),
    ("https://openrouter.ai/api/", "https://openrouter.ai/api/v1/systemone"),
])
def test_hosted_endpoint_preserves_batched_heads_validation_and_cost(monkeypatch, base, expected_url):
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    if base:
        monkeypatch.setenv("TYPESAFE_BASE_URL", base)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_MODEL", "typesafe/jev-1.13")

    def respond(url, key, body):
        assert url == expected_url and key == "test-key"
        assert body["model"] == "typesafe/jev-1.13"
        questions = body["questions"]
        assert {"operation", "type_text_target", "click_target"} <= questions.keys()
        return {"model": "typesafe/jev-1.13-test", "usage": {"input_tokens": 100, "cost": 0.0000042},
                "answers": {
                    "operation": answer(questions["operation"]["criteria"], "TYPE_TEXT"),
                    "type_text_target": answer(questions["type_text_target"]["criteria"], "1"),
                }}

    post = Mock(side_effect=respond)
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(CASES[0]["state"], CASES[0]["goal"], [])
    assert result["choice"] == "e1"
    assert result["model"] == "typesafe/jev-1.13-test"
    assert result["usage"]["cost"] == 0.0000042
    post.assert_called_once()


def test_local_backend_needs_no_typesafe_key_and_still_maps_observed_target(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    post = Mock(side_effect=AssertionError("Local decisions must not call TypeSafe"))
    monkeypatch.setattr(model, "post_json", post)

    def predict(_state, questions):
        return {"model": "local", "usage": {"input_tokens": 10, "output_tokens": 0}, "answers": {
            "operation": answer(questions["operation"]["criteria"], "TYPE_TEXT"),
            "type_text_target": answer(questions["type_text_target"]["criteria"], "1"),
            "click_target": {"choice": "invalid-unused-head"},
        }}

    backend = SimpleNamespace(model="local", predict=predict)
    case = CASES[0]
    result = model.choose(case["state"], case["goal"], [], decision_model=backend)
    assert result["choice"] == "e1"
    assert result["request"]["model"] == "local"
    post.assert_not_called()


@pytest.mark.parametrize("content,valid", [
    ('{"text": "Lisbon"}', True),
    ('{"text": "Lisbon"}\n```', False),
    ('{"text": null}', False),
    ('{"text": "Lisbon", "extra": "ignored?"}', False),
])
def test_strict_helper_schema_keeps_executor_validation(monkeypatch, content, valid):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TEXT_MODEL_RESPONSE_FORMAT", "json_schema")
    post = Mock(return_value={"choices": [{"message": {"content": content}}]})
    monkeypatch.setattr(model, "post_json", post)
    if valid:
        value, _ = model.field_text({"goal": "Find a hotel in Lisbon"})
        assert value == "Lisbon"
    else:
        with pytest.raises(ValueError, match="no valid field value"):
            model.field_text({"goal": "Find a hotel in Lisbon"})
    schema = post.call_args.args[2]["response_format"]["json_schema"]
    assert schema["strict"]
    assert schema["schema"]["properties"]["text"]["type"] == ["string", "null"]
    assert schema["schema"]["required"] == ["text"]
    assert not schema["schema"]["additionalProperties"]


def test_invalid_local_target_cannot_bypass_upstream_validation():
    def predict(_state, questions):
        return {"answers": {
            "operation": answer(questions["operation"]["criteria"], "CLICK"),
            "click_target": answer({"999": "fabricated"}, "999"),
        }}

    backend = SimpleNamespace(model="local", predict=predict)
    with pytest.raises(ValueError, match="Invalid TypeSafe response"):
        model.choose(CASES[0]["state"], CASES[0]["goal"], [], decision_model=backend)


def test_laya_loads_pinned_weights_once_and_reuses_the_agent(monkeypatch):
    resident = SimpleNamespace(
        cfg={"max_len": 512, "head_max_len": 192}, device="cpu",
        predict=Mock(return_value={"answers": {}, "usage": {"output_tokens": 0}}),
    )
    load = Mock(return_value=resident)
    download = Mock(return_value="/unused-model-cache")
    monkeypatch.setitem(sys.modules, "laya", SimpleNamespace(load=load))
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=download))
    monkeypatch.setattr(laya_backend, "input_audit", Mock(return_value={}))
    backend = laya_backend.LayaBackend(device="cpu")
    for _ in range(2):
        result = backend.predict({"page": "same"}, {})
        assert result["model"] == backend.model
    assert load.call_count == download.call_count == 1
    assert resident.predict.call_count == 2
    assert download.call_args.kwargs["revision"] == laya_backend.MODEL_REVISION
    assert download.call_args.kwargs["allow_patterns"] == [
        "rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*",
    ]


def test_failed_attempt_cost_counts_toward_cost_per_success_and_warmups_are_excluded():
    base = {"backend": "jev", "phase": "measured", "elapsed_ms": 100, "error": None,
            "jev_input_tokens": 1000, "reported_text_cost_usd": 0}
    rows = [{**base, "passed": True}, {**base, "passed": False},
            {**base, "passed": True, "phase": "warmup", "elapsed_ms": 9000}]
    result = summarize(rows)["jev"]
    assert result["attempts"] == 2 and result["passed"] == 1
    assert result["latency_p50_ms"] == 100
    assert result["model_usd_per_success"] == pytest.approx(2000 * 0.042 / 1_000_000)


def test_all_failed_runs_have_no_cost_per_success_and_missing_cost_is_flagged():
    result = summarize([{"backend": "laya", "phase": "measured", "elapsed_ms": 50,
                         "error": "provider failed", "passed": False, "jev_input_tokens": 0,
                         "reported_text_cost_usd": 0}])["laya"]
    assert result["model_usd_per_success"] is None
    assert result["cost_incomplete"]


def test_done_does_not_pass_without_independent_browser_outcome(monkeypatch):
    from scripts import compare

    agent = Mock()
    agent.state = {"status": "done"}
    agent.snapshot.return_value = {"decisions": [], "text_calls": []}
    agent.browser.observe.return_value = {"url": "https://fixture.test/#wrong-property"}
    agent.browser.evaluate.return_value = (
        "Your filters: Design · Free cancellation enabled · Destination Lisbon"
    )
    context = Mock(__enter__=Mock(return_value=agent), __exit__=Mock(return_value=False))
    monkeypatch.setattr(compare, "Agent", Mock(return_value=context))
    trace, error, passed, _elapsed, _setup = compare.browser_trial("https://fixture.test/", None, 10)
    assert not passed and error is None
    assert trace["verification"]["passed"] is False
    agent.browser.observe.assert_called_once_with(screenshot=False)


def test_laya_budget_audit_exposes_truncated_goal_options_and_state():
    pytest.importorskip("laya")  # Optional extra; no model weights or network are used.

    class WordTokenizer:
        mask_token, mask_token_id, cls_token_id, sep_token_id = "[MASK]", 1, 2, 3

        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": [4] * len(text.split())}

    agent = SimpleNamespace(
        tok=WordTokenizer(), cfg={"max_len": 256, "head_max_len": 64},
        _to_internal=lambda q: {"t": q["type"], "ins": q["instructions"], "crit": q["criteria"]},
    )
    questions = {"target": {"type": "choice", "instructions": "preserve this goal " * 30,
                            "criteria": {str(i): "long distinct target description here" for i in range(40)}}}
    audit = laya_backend.input_audit(agent, {"text": "page information " * 300}, questions)["target"]
    assert audit["instruction_truncated"]
    assert audit["options_truncated"] == 40
    assert audit["state_truncated"]
    assert audit["options_dropped"] == 0
