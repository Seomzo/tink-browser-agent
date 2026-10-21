"""Explicit model comparison: synthetic decision diagnostics or verified local browser tasks."""

import argparse
import hashlib
import json
import os
import platform
import statistics
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jev_ultrafast import Agent
from jev_ultrafast.demo import load_environment
from jev_ultrafast.model import choose

ROOT = Path(__file__).resolve().parents[1]
JEV_INPUT_USD_PER_MILLION = 0.042  # https://docs.typesafe.ai/models, checked 2026-09-22
GOAL = (
    "Use the destination search and filters to find Design stays in Lisbon with Free cancellation, "
    "then open Casa Flora."
)


def summarize(rows):
    summary = {}
    for backend in dict.fromkeys(row["backend"] for row in rows):
        measured = [r for r in rows if r["backend"] == backend and r["phase"] == "measured"]
        if not measured:
            continue
        passed = sum(r["passed"] for r in measured)
        times = sorted(r["elapsed_ms"] for r in measured)
        jev_cost = sum(r["jev_input_tokens"] for r in measured) * JEV_INPUT_USD_PER_MILLION / 1_000_000
        helper_cost = sum(r["reported_text_cost_usd"] for r in measured)
        summary[backend] = {
            "attempts": len(measured), "passed": passed, "pass_rate": passed / len(measured),
            "errors": sum(r["error"] is not None for r in measured),
            "latency_p50_ms": statistics.median(times),
            "latency_p95_ms": times[max(0, (95 * len(times) + 99) // 100 - 1)],
            "estimated_jev_cost_usd": jev_cost,
            "reported_text_cost_usd": helper_cost,
            "model_usd_per_success": (jev_cost + helper_cost) / passed if passed else None,
            "cost_incomplete": any(r["error"] or r.get("text_cost_missing") for r in measured),
        }
    return summary


@contextmanager
def fixture_server():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "jev_ultrafast" / "static"))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture.html?scenario=travel"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def browser_trial(url, backend, max_decisions):
    trace, error, verified = {}, None, False
    setup = time.perf_counter()
    with Agent(url, GOAL, decision_model=backend) as agent:
        setup_ms = round((time.perf_counter() - setup) * 1000)
        started = time.perf_counter()
        try:
            while agent.state["status"] not in {"done", "blocked"}:
                if len(agent.state["decisions"]) >= max_decisions:
                    raise RuntimeError("Comparison decision budget reached")
                agent.command("tick")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        trace = agent.snapshot()
        # Fresh, independent verification is outside the upstream-compatible task clock.
        try:
            final = agent.browser.observe(screenshot=False)
            text = agent.browser.evaluate("document.body.innerText")
            verified = final["url"].endswith("#casa-flora") and (
                "Your filters: Design · Free cancellation enabled · Destination Lisbon" in text
            )
            trace["verification"] = {"passed": verified, "url": final["url"], "text": text}
        except Exception as exc:
            error = error or f"Verification {type(exc).__name__}: {exc}"
    return trace, error, verified and error is None, elapsed_ms, setup_ms


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["decisions", "browser"], default="decisions")
    parser.add_argument("--backends", nargs="+", choices=["jev", "laya"], default=["jev"])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--max-decisions", type=int, default=20)
    parser.add_argument("--device", choices=["cpu", "mps", "cuda"])
    parser.add_argument("--checkpoint", choices=["english", "multilingual", "typed-decisions"], default="english")
    parser.add_argument("--max-len", type=int)
    parser.add_argument("--head-max-len", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or args.warmups < 0 or args.max_decisions < 1:
        parser.error("repeats/max-decisions must be positive and warmups cannot be negative")
    load_environment()
    os.environ.setdefault("TYPESAFE_MODEL", "jev-1.13.0")
    needed = (["TYPESAFE_API_KEY"] if "jev" in args.backends else [])
    if args.suite == "browser":
        needed.append("TEXT_MODEL_API_KEY")
    missing = [key for key in needed if not os.environ.get(key)]
    if missing:
        parser.error("Configure these in the ignored local .env before live calls: " + ", ".join(missing))

    cases_path = ROOT / "benchmarks" / "decisions.json"
    cases = json.loads(cases_path.read_text())
    output = args.output or ROOT / "artifacts" / "comparison" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    backends = {name: None for name in dict.fromkeys(args.backends)}
    if "laya" in backends:
        from jev_ultrafast.laya_backend import LayaBackend
        backends["laya"] = LayaBackend(
            device=args.device, checkpoint=args.checkpoint, max_len=args.max_len, head_max_len=args.head_max_len
        )
    report = {
        "suite": args.suite,
        "scope": "Synthetic next-action diagnostics; not browser completion" if args.suite == "decisions"
        else "Repeated local hotel fixture; not a broad web reliability benchmark",
        "pricing": {"jev_input_usd_per_million": JEV_INPUT_USD_PER_MILLION, "checked": "2026-09-22",
                    "source": "https://docs.typesafe.ai/models",
                    "excludes": "local compute, browser hosting, parent agent"},
        "configuration": {k: os.environ.get(k) for k in (
            "TYPESAFE_MODEL", "TYPESAFE_BASE_URL", "TEXT_MODEL", "TEXT_MODEL_BASE_URL", "TEXT_MODEL_REASONING",
            "TEXT_MODEL_RESPONSE_FORMAT",
        )},
        "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
        "system": {"os": platform.system(), "architecture": platform.machine(), "python": platform.python_version()},
        "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in [*sorted((ROOT / "jev_ultrafast").glob("*.py")),
                                    ROOT / "jev_ultrafast" / "snapshot.js", Path(__file__), cases_path]},
        "load_ms": {k: v.load_ms if v is not None else 0 for k, v in backends.items()},
        "rows": [],
    }

    def run_trial(name, case, phase, repeat, url=None):
        row = {"backend": name, "case": case["id"], "phase": phase, "repeat": repeat,
               "passed": False, "error": None, "jev_input_tokens": 0, "reported_text_cost_usd": 0}
        start = time.perf_counter()
        try:
            if url is not None:
                trace, row["error"], row["passed"], elapsed, row["browser_setup_ms"] = browser_trial(
                    url, backends[name], args.max_decisions
                )
                decisions, text_calls = trace["decisions"], trace["text_calls"]
            else:
                trace = choose(case["state"], case["goal"], case.get("history", []), decision_model=backends[name])
                elapsed = round((time.perf_counter() - start) * 1000)
                row["passed"] = trace["choice"] in case["expected_choices"]
                decisions, text_calls = [trace], []
            row["elapsed_ms"] = elapsed
            row["trace"] = trace
            row["decision_requests"] = len(decisions)
            row["jev_input_tokens"] = sum(d["usage"].get("input_tokens", 0) for d in decisions) if name == "jev" else 0
            row["reported_text_cost_usd"] = sum(c.get("usage", {}).get("cost", 0) for c in text_calls)
            row["text_cost_missing"] = any("cost" not in c.get("usage", {}) for c in text_calls)
        except Exception as exc:
            row["elapsed_ms"] = round((time.perf_counter() - start) * 1000)
            row["error"] = f"{type(exc).__name__}: {exc}"
        report["rows"].append(row)
        report["summary"] = summarize(report["rows"])
        (output / "results.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({k: v for k, v in row.items() if k != "trace"}), flush=True)

    # Warm-up calls are retained in raw results; scoring/latency summaries exclude them.
    for name in backends:
        for repeat in range(args.warmups):
            run_trial(name, cases[0], "warmup", repeat)
    names = list(backends)

    def measured(url=None):
        for repeat in range(args.repeats):
            for case in ([{"id": "hotel-fixture"}] if url else cases):
                for name in (names if repeat % 2 == 0 else list(reversed(names))):
                    run_trial(name, case, "measured", repeat, url)

    if args.suite == "browser":
        with fixture_server() as url:
            measured(url)
    else:
        measured()
    print(json.dumps({"output": str(output / "results.json"), "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    main()
