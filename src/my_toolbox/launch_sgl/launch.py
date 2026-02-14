#!/usr/bin/env python3
import argparse
import dataclasses
import os
from typing import Dict, List, Optional, Union

import tabulate

from my_toolbox.utils.list_ib_devices import get_active_ib_devices

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Default ports
# ---------------------------------------------------------------------------

DEFAULT_PORT = 23333
DEFAULT_PREFILL_PORT = 25000
DEFAULT_DECODE_PORT = 27000


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _add_basic_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("basic")
    group.add_argument("--model", type=str, choices=MODEL_MAP.keys(), default="llama3")
    group.add_argument("--host", type=str, default="0.0.0.0")
    group.add_argument("--port", type=int, default=None)
    group.add_argument("--chunk", type=int, default=None)


def _add_spec_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("speculative decoding")
    group.add_argument("--spec", action="store_true")
    group.add_argument("--spec-v2", action="store_true")
    group.add_argument("--spec-algo", type=str, default="EAGLE")
    group.add_argument("--spec-steps", type=int, default=5)
    group.add_argument("--spec-topk", type=int, default=1)
    group.add_argument("--spec-tokens", type=int, default=None)


def _add_parallelism_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("parallelism")
    group.add_argument("--tp", type=int, default=None)
    group.add_argument("--dp", type=int, default=None)


def _add_backend_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("backend")
    group.add_argument("--attn", type=str, default=None)


def _add_disagg_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("disaggregation")

    mode_group = group.add_mutually_exclusive_group()
    mode_group.add_argument("--prefill", action="store_true")
    mode_group.add_argument("--decode", action="store_true")
    mode_group.add_argument("--router", action="store_true")

    group.add_argument("--base-gpu-id", type=int, default=None)
    group.add_argument("--transfer-backend", type=str, default="nixl")
    group.add_argument("--ib-device", type=str, default=None)


def _add_router_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("router")
    group.add_argument("--ip-prefill", type=str, default="127.0.0.1")
    group.add_argument("--ip-decode", type=str, default="127.0.0.1")
    group.add_argument("--port-prefill", type=int, default=DEFAULT_PREFILL_PORT)
    group.add_argument("--port-decode", type=int, default=DEFAULT_DECODE_PORT)


def _add_other_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("other")
    group.add_argument("--page-size", type=int, default=None)
    group.add_argument("--max-reqs", type=int, default=None)
    group.add_argument("--mem-frac", type=float, default=None)
    group.add_argument("--dtype", type=str, default=None)
    group.add_argument("--no-cuda-graph", action="store_true")
    group.add_argument("--no-radix", action="store_true")
    group.add_argument("--log-interval", type=int, default=None)
    group.add_argument("--log-requests", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch SGLang server or router")
    _add_basic_args(parser)
    _add_spec_args(parser)
    _add_parallelism_args(parser)
    _add_backend_args(parser)
    _add_disagg_args(parser)
    _add_router_args(parser)
    _add_other_args(parser)
    return parser.parse_args()


# ---------------------------------------------------------------------------
# ServerLauncher
# ---------------------------------------------------------------------------


class ServerLauncher:
    """Build launch command for sglang server (normal / prefill / decode)."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.model_config = MODEL_MAP[args.model]

    def build_cmd(self) -> List[str]:
        cmd = ["python3", "-m", "sglang.launch_server"]
        cmd += self._basic_args()
        cmd += self._spec_args()
        cmd += self._parallelism_args()
        cmd += self._backend_args()
        cmd += self._reasoning_args()
        cmd += self._disagg_args()
        cmd += self._other_args()
        return cmd

    # -- private helpers ----------------------------------------------------

    def _basic_args(self) -> List[str]:
        args = self.args
        result = ["--model", self.model_config.model_path]
        result += ["--host", args.host]

        if args.port is not None:
            port = args.port
        elif args.prefill:
            port = DEFAULT_PREFILL_PORT
        elif args.decode:
            port = DEFAULT_DECODE_PORT
        else:
            port = DEFAULT_PORT
        result += ["--port", str(port)]

        if args.chunk is not None:
            result += ["--chunk", str(args.chunk)]

        return result

    def _spec_args(self) -> List[str]:
        args = self.args

        if args.spec_v2:
            os.environ["SGLANG_ENABLE_SPEC_V2"] = "1"
            args.spec = True

        if not args.spec:
            return []

        result = []
        spec_tokens = args.spec_tokens or (args.spec_steps + 1)

        # Resolve draft model path
        if args.spec_algo != "NGRAM" and self.model_config.draft_path is not None:
            draft_path = self.model_config.draft_path
            if isinstance(draft_path, dict):
                if args.spec_algo not in draft_path:
                    raise ValueError(
                        f"Speculative draft model for algorithm "
                        f"{args.spec_algo} is not defined."
                    )
                draft_path = draft_path[args.spec_algo]
            result += ["--speculative-draft-model", str(draft_path)]

        result += ["--speculative-algorithm", args.spec_algo]
        result += ["--speculative-num-steps", str(args.spec_steps)]
        result += ["--speculative-eagle-topk", str(args.spec_topk)]
        result += ["--speculative-num-draft-tokens", str(spec_tokens)]

        return result

    def _parallelism_args(self) -> List[str]:
        args = self.args

        if args.dp is not None:
            return [
                "--enable-dp-attention",
                "--enable-dp-lm-head",
                "--dp",
                str(args.dp),
                "--tp",
                str(args.dp),
            ]

        if args.tp is not None:
            return ["--tp", str(args.tp)]
        elif self.model_config.tp is not None:
            return ["--tp", str(self.model_config.tp)]

        return []

    def _backend_args(self) -> List[str]:
        if self.args.attn is not None:
            return ["--attention-backend", self.args.attn]
        return []

    def _reasoning_args(self) -> List[str]:
        if self.model_config.reasoning_parser is not None:
            return ["--reasoning-parser", self.model_config.reasoning_parser]
        return []

    def _disagg_args(self) -> List[str]:
        args = self.args
        if not args.prefill and not args.decode:
            return []

        mode = "prefill" if args.prefill else "decode"
        result = [f"--disaggregation-mode={mode}"]

        if args.base_gpu_id is not None:
            result += ["--base-gpu-id", str(args.base_gpu_id)]

        result += ["--disaggregation-transfer-backend", args.transfer_backend]

        # IB device: explicit value > auto-detect
        ib_device = args.ib_device
        if ib_device is None:
            devices = get_active_ib_devices()
            if devices:
                ib_device = ",".join(devices)
        if ib_device:
            result += ["--disaggregation-ib-device", ib_device]

        return result

    def _other_args(self) -> List[str]:
        args = self.args
        result = []

        if args.page_size is not None:
            result += ["--page-size", str(args.page_size)]

        if args.max_reqs is not None:
            result += ["--max-running-requests", str(args.max_reqs)]

        if args.mem_frac is not None:
            result += ["--mem-fraction-static", str(args.mem_frac)]

        if args.dtype is not None:
            result += ["--dtype", args.dtype]
        elif args.model in ["llama3"] and (args.spec or args.spec_v2):  # FIXME
            result += ["--dtype", "float16"]

        if args.no_cuda_graph:
            result += ["--disable-cuda-graph"]

        if args.no_radix:
            result += ["--disable-radix-cache"]

        if args.log_interval is not None:
            result += ["--decode-log-interval", str(args.log_interval)]

        if args.log_requests:
            result += ["--log-requests", "--log-requests-level", "3"]

        return result


# ---------------------------------------------------------------------------
# RouterLauncher
# ---------------------------------------------------------------------------


class RouterLauncher:
    """Build launch command for sglang router."""

    def __init__(self, args: argparse.Namespace):
        self.args = args

    def build_cmd(self) -> List[str]:
        args = self.args
        cmd = ["python3", "-m", "sglang_router.launch_router"]
        cmd += ["--pd-disaggregation", "--mini-lb"]

        cmd += ["--host", args.host]
        cmd += ["--port", str(args.port or DEFAULT_PORT)]

        cmd += ["--prefill", f"http://{args.ip_prefill}:{args.port_prefill}"]
        cmd += ["--decode", f"http://{args.ip_decode}:{args.port_decode}"]

        return cmd


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    args = parse_args()

    set_default_envs()
    print_launch_envs()

    launcher = RouterLauncher(args) if args.router else ServerLauncher(args)
    cmd = launcher.build_cmd()

    print(f"command={' '.join(cmd)}")
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
