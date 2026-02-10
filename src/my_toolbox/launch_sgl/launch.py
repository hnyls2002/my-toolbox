#!/usr/bin/env python3
import argparse
import dataclasses
import os
from typing import Dict, Optional, Union

import tabulate


def print_launch_envs():
    env_vars = [
        ("SGLANG_ENABLE_SPEC_V2", False),
        ("CUDA_LAUNCH_BLOCKING", False),
        ("SGLANG_TORCH_PROFILER_DIR", "N/A"),
        ("SGLANG_ENABLE_JIT_DEEPGEMM", True),
        ("SGLANG_ENABLE_STRICT_MEM_CHECK_DURING_BUSY", 0),
    ]
    env_status = [(var[0], os.getenv(var[0], var[1])) for var in env_vars]
    env_msg = tabulate.tabulate(
        env_status, headers=["Variable", "Value"], tablefmt="fancy_grid"
    )
    print(env_msg)


def set_default_envs():
    os.environ["SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN"] = "1"
    os.environ["SGLANG_TORCH_PROFILER_DIR"] = "./"
    os.environ["SGLANG_ENABLE_JIT_DEEPGEMM"] = "0"


@dataclasses.dataclass
class ModelInfo:
    model_path: str
    draft_path: Optional[Union[str, Dict[str, str]]] = None
    tp: Optional[int] = None

    reasoning_parser: Optional[str] = None


MODEL_MAP = {
    "llama2": ModelInfo(
        model_path="meta-llama/Llama-2-7b-chat-hf",
        draft_path="lmsys/sglang-EAGLE-llama2-chat-7B",
    ),
    "llama3": ModelInfo(
        model_path="meta-llama/Meta-Llama-3.1-8B-Instruct",
        draft_path={
            "EAGLE": "lmsys/sglang-EAGLE-LLaMA3-Instruct-8B",
            "EAGLE3": "lmsys/sglang-EAGLE3-LLaMA3.1-Instruct-8B",
        },
    ),
    "gpt": ModelInfo(
        model_path="openai/gpt-oss-120b",
        draft_path="lmsys/EAGLE3-gpt-oss-120b-bf16",
        reasoning_parser="gpt-oss",
        tp=2,
    ),
    "ds-mini": ModelInfo(
        model_path="lmsys/sglang-ci-dsv3-test",
        draft_path="lmsys/sglang-ci-dsv3-test-NextN",
    ),
    "qwen3-next": ModelInfo(
        model_path="Qwen/Qwen3-Next-80B-A3B-Instruct",
        tp=2,
    ),
}

# default ports
DEFAULT_PORT = 23333
DEFAULT_PREFILL_PORT = 25000
DEFAULT_DECODE_PORT = 27000


def extend_basic_args(args):
    basic_args = ["--model", MODEL_MAP[args.model].model_path]
    basic_args += ["--host", args.host]

    if args.port is not None:
        basic_args += ["--port", str(args.port)]
    else:
        if args.prefill:
            port = DEFAULT_PREFILL_PORT
        elif args.decode:
            port = DEFAULT_DECODE_PORT
        else:
            port = DEFAULT_PORT
        basic_args += ["--port", str(port)]

    if args.chunk is not None:
        basic_args += ["--chunk", str(args.chunk)]

    return basic_args


def extend_spec_args(args):
    if args.spec_v2:
        os.environ["SGLANG_ENABLE_SPEC_V2"] = "1"
        args.spec = True

    if not args.spec:
        return []

    spec_args = []
    spec_tokens = args.spec_tokens or (args.spec_steps + 1)
    if (
        args.spec_algo != "NGRAM"
        and (draft_path := MODEL_MAP[args.model].draft_path) is not None
    ):
        if isinstance(draft_path, dict):
            algo = args.spec_algo
            if algo not in draft_path:
                raise ValueError(
                    f"Speculative draft model for algorithm {algo} is not defined."
                )
            draft_path = draft_path[algo]

        spec_args = ["--speculative-draft-model", str(draft_path)]

    spec_args += ["--speculative-algorithm", args.spec_algo]
    spec_args += ["--speculative-num-steps", str(args.spec_steps)]
    spec_args += ["--speculative-eagle-topk", str(args.spec_topk)]
    spec_args += ["--speculative-num-draft-tokens", str(spec_tokens)]

    return spec_args


def extend_parallelism_args(args):
    basic_args = []
    model_config = MODEL_MAP[args.model]

    # Resolve DP
    if args.dp is not None:
        basic_args += [
            "--enable-dp-attention",
            "--enable-dp-lm-head",
            "--dp",
            str(args.dp),
            "--tp",
            str(args.dp),
        ]
        return basic_args

    # Resolve TP
    if args.tp is not None:
        basic_args += ["--tp", str(args.tp)]
    elif model_config.tp is not None:
        basic_args += ["--tp", str(model_config.tp)]

    return basic_args


def extend_backend_args(args):
    if args.attn is not None:
        return ["--attention-backend", args.attn]

    return []


def extend_reasoning_args(args):
    reasoning_args = []
    model_config = MODEL_MAP[args.model]
    if model_config.reasoning_parser is not None:
        reasoning_args += ["--reasoning-parser", model_config.reasoning_parser]
    return reasoning_args


def extend_disagg_args(args):
    disagg_args = []
    if args.prefill:
        disagg_args += ["--disaggregation-mode=prefill"]
    elif args.decode:
        disagg_args += ["--disaggregation-mode=decode"]
    return disagg_args


def get_router_args(args):
    router_args = ["python3", "-m", "sglang_router.launch_router"]
    router_args += ["--pd-disaggregation", "--mini-lb"]
    assert args.ip_prefill is not None and args.ip_decode is not None
    prefill_port = args.port_prefill or DEFAULT_PREFILL_PORT
    decode_port = args.port_decode or DEFAULT_DECODE_PORT
    router_args += ["--prefill", f"http://{args.ip_prefill}:{prefill_port}"]
    router_args += ["--decode", f"http://{args.ip_decode}:{decode_port}"]

    if args.host is not None:
        router_args += ["--host", args.host]

    port = args.port or DEFAULT_PORT
    router_args += ["--port", str(port)]

    return router_args


def extend_other_args(args):
    other_args = []
    if args.page_size is not None:
        other_args = ["--page-size", str(args.page_size)]

    if args.max_reqs is not None:
        other_args += ["--max-running-requests", str(args.max_reqs)]

    if args.mem_frac is not None:
        other_args += ["--mem-fraction-static", str(args.mem_frac)]

    if args.dtype is not None:
        other_args += ["--dtype", args.dtype]
    elif args.model in ["llama3"] and (args.spec or args.spec_v2):  # FIXME
        other_args += ["--dtype", "float16"]

    if args.no_cuda_graph:
        other_args += ["--disable-cuda-graph"]

    if args.no_radix:
        other_args += ["--disable-radix-cache"]

    if args.log_interval is not None:
        other_args += ["--decode-log-interval", str(args.log_interval)]

    if args.log_requests:
        other_args += ["--log-requests", "--log-requests-level", "3"]

    return other_args


def main():
    parser = argparse.ArgumentParser()
    # Basic args
    parser.add_argument("--model", type=str, choices=MODEL_MAP.keys(), default="llama3")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--spec", action="store_true")
    parser.add_argument("--spec-v2", action="store_true")  # Syntax sugar
    parser.add_argument("--chunk", type=int, default=None)

    # Spec args
    parser.add_argument("--spec-algo", type=str, default="EAGLE")
    parser.add_argument("--spec-steps", type=int, default=5)
    parser.add_argument("--spec-topk", type=int, default=1)
    parser.add_argument("--spec-tokens", type=int, default=None)

    # Parallelism args
    parser.add_argument("--tp", type=int, default=None)
    parser.add_argument("--dp", type=int, default=None)

    # Backend args
    parser.add_argument("--attn", type=str, default=None)

    disagg_group = parser.add_mutually_exclusive_group()
    disagg_group.add_argument("--prefill", action="store_true")
    disagg_group.add_argument("--decode", action="store_true")
    disagg_group.add_argument("--router", action="store_true")
    parser.add_argument("--ip-prefill", type=str, default=None)
    parser.add_argument("--ip-decode", type=str, default=None)
    parser.add_argument("--port-prefill", type=int, default=None)
    parser.add_argument("--port-decode", type=int, default=None)

    # Other args
    parser.add_argument("--page-size", type=int, default=None)
    parser.add_argument("--max-reqs", type=int, default=None)
    parser.add_argument("--mem-frac", type=float, default=None)
    parser.add_argument("--dtype", type=str, default=None)
    parser.add_argument("--no-cuda-graph", action="store_true")
    parser.add_argument("--no-radix", action="store_true")
    parser.add_argument("--log-interval", type=int, default=None)
    parser.add_argument("--log-requests", action="store_true")

    args = parser.parse_args()

    if not args.router:
        launch_cmd = ["python3", "-m", "sglang.launch_server"]
        launch_cmd += extend_basic_args(args)
        launch_cmd += extend_spec_args(args)
        launch_cmd += extend_parallelism_args(args)
        launch_cmd += extend_backend_args(args)
        launch_cmd += extend_reasoning_args(args)
        launch_cmd += extend_disagg_args(args)
        launch_cmd += extend_other_args(args)
    else:
        launch_cmd = get_router_args(args)

    command = " ".join(launch_cmd)

    set_default_envs()
    print_launch_envs()
    print(f"command={command}")

    os.execvp("python3", launch_cmd)


if __name__ == "__main__":
    main()
