"""Optional, resident Laya decision backend. Browser execution remains in the upstream loop."""

import time

MODEL_REPO = "convaiinnovations/laya"
MODEL_REVISION = "1c5edc17a7acd8701df6fc341c0d179f1c62c982"


class LayaBackend:
    def __init__(self, *, device=None, checkpoint="english", max_len=None, head_max_len=None):
        if checkpoint not in {"english", "multilingual", "typed-decisions"}:
            raise ValueError("Unknown Laya checkpoint")
        overrides = {k: v for k, v in (("max_len", max_len), ("head_max_len", head_max_len)) if v is not None}
        if any(value <= 0 for value in overrides.values()):
            raise ValueError("Input budgets must be positive")
        try:
            import laya
            from huggingface_hub import snapshot_download
        except ImportError:
            raise RuntimeError("Install the optional backend with: uv sync --extra laya") from None

        started = time.perf_counter()
        subfolder = None if checkpoint == "english" else checkpoint
        prefix = f"{subfolder}/" if subfolder else ""
        folder = snapshot_download(
            MODEL_REPO,
            revision=MODEL_REVISION,
            allow_patterns=[
                prefix + p for p in ("rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")
            ],
        )
        self.agent = laya.load(folder, device=device, subfolder=subfolder)
        self.agent.cfg.update(overrides)
        if self.agent.cfg["head_max_len"] >= self.agent.cfg["max_len"]:
            raise ValueError("head_max_len must leave room for state within max_len")
        self.load_ms = round((time.perf_counter() - started) * 1000)
        self.model = f"{MODEL_REPO}/{checkpoint}@{MODEL_REVISION}"

    def predict(self, state, questions):
        # Audit the actual sequence builder: typed output validity does not prove input survival.
        audit = input_audit(self.agent, state, questions)
        result = self.agent.predict(state, questions)
        return {
            **result,
            "model": self.model,
            "backend": {
                "device": str(self.agent.device),
                "load_ms": self.load_ms,
                "max_len": self.agent.cfg["max_len"],
                "head_max_len": self.agent.cfg["head_max_len"],
                "input_audit": audit,
            },
        }


def input_audit(agent, state, questions):
    """Report text shortened by the pinned upstream builder without modifying model inputs."""
    from laya.common import build_sequence, render_options, serialize_state

    tok, cfg = agent.tok, agent.cfg
    state_tokens = tok(serialize_state(state).replace(tok.mask_token, " "), add_special_tokens=False)["input_ids"]
    audit = {}
    for qid, definition in questions.items():
        q = agent._to_internal(definition)
        ids, markers = build_sequence(tok, state, q, cfg["max_len"], cfg["head_max_len"])
        options = render_options(q)
        instruction = f"{q['t']} question: {str(q['ins']).replace(tok.mask_token, ' ')}"
        instruction_count = len(tok(instruction, add_special_tokens=False)["input_ids"])
        separators = [i for i, token in enumerate(ids) if token == tok.sep_token_id]
        # A clipped sequence may not retain the separator before the state.
        option_end = next((i for i in separators if markers and i > markers[-1]), len(ids))
        retained_state = max(0, len(ids) - option_end - 2)
        shortened = 0
        for i, marker in enumerate(markers):
            end = markers[i + 1] if i + 1 < len(markers) else option_end
            full = len(tok(" " + options[i].replace(tok.mask_token, " "), add_special_tokens=False)["input_ids"])
            shortened += end - marker - 1 < full
        audit[qid] = {
            "instruction_truncated": not markers or markers[0] - 2 < instruction_count,
            "options_truncated": shortened,
            "options_dropped": len(options) - len(markers),
            "state_tokens": len(state_tokens),
            "state_tokens_retained": retained_state,
            "state_truncated": retained_state < len(state_tokens),
        }
    return audit
