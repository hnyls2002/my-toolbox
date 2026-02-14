#!/usr/bin/env python3
import argparse
import os
from typing import List

from my_toolbox.sgl_run.environ import print_launch_envs, set_default_envs
from my_toolbox.sgl_run.models import MODEL_MAP, ModelInfo
from my_toolbox.utils.list_ib_devices import get_active_ib_devices

DEFAULT_PORT = 23333
DEFAULT_PREFILL_PORT = 25000
DEFAULT_DECODE_PORT = 27000


# --- Basic args ---


def add_basic_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("basic")
    group.add_argument("--model", type=str, choices=MODEL_MAP.keys(), default="llama3")
    group.add_argument("--host", type=str, default="0.0.0.0")
    group.add_argument("--port", type=int, default=None)
    group.add_argument("--chunk", type=int, default=None)


def build_basic_args(args: argparse.Namespace, model_config: ModelInfo) -> List[str]:
    result = ["--model", model_config.model_path]
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


# --- Speculative decoding args ---


def add_spec_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("speculative decoding")
    group.add_argument("--spec", action="store_true")
    group.add_argument("--spec-v2", action="store_true")
    group.add_argument("--spec-algo", type=str, default="EAGLE")
    group.add_argument("--spec-steps", type=int, default=5)
    group.add_argument("--spec-topk", type=int, default=1)
    group.add_argument("--spec-tokens", type=int, default=None)


def build_spec_args(args: argparse.Namespace, model_config: ModelInfo) -> List[str]:
    if args.spec_v2:
        os.environ["SGLANG_ENABLE_SPEC_V2"] = "1"
        args.spec = True

    if not args.spec:
        return []

    result = []
    spec_tokens = args.spec_tokens or (args.spec_steps + 1)

    if args.spec_algo != "NGRAM" and model_config.draft_path is not None:
        draft_path = model_config.draft_path
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


# --- Parallelism args ---


def add_parallelism_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("parallelism")
    group.add_argument("--tp", type=int, default=None)
    group.add_argument("--dp", type=int, default=None)


def build_parallelism_args(
    args: argparse.Namespace, model_config: ModelInfo
) -> List[str]:
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
    elif model_config.tp is not None:
        return ["--tp", str(model_config.tp)]

    return []


# --- Disaggregation args ---


def add_disagg_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("disaggregation")

    mode_group = group.add_mutually_exclusive_group()
    mode_group.add_argument("--prefill", action="store_true")
    mode_group.add_argument("--decode", action="store_true")
    mode_group.add_argument("--router", action="store_true")

    group.add_argument("--base-gpu", type=int, default=None)
    group.add_argument("--transfer", type=str, default="mooncake")
    group.add_argument("--ib", type=str, default=None)


def build_disagg_args(args: argparse.Namespace, model_config: ModelInfo) -> List[str]:
    if not args.prefill and not args.decode:
        return []

    mode = "prefill" if args.prefill else "decode"
    result = [f"--disaggregation-mode={mode}"]

    if args.base_gpu is not None:
        result += ["--base-gpu-id", str(args.base_gpu)]

    result += ["--disaggregation-transfer-backend", args.transfer]

    # IB device: explicit value > auto-detect
    if args.ib is None:
        devices = get_active_ib_devices()
        if devices:
            ib_device = devices[0]
            result += ["--disaggregation-ib-device", ib_device]

    return result


# --- Router args ---


def add_router_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("router")
    group.add_argument("--ip-prefill", type=str, default="127.0.0.1")
    group.add_argument("--ip-decode", type=str, default="127.0.0.1")
    group.add_argument("--port-prefill", type=int, default=DEFAULT_PREFILL_PORT)
    group.add_argument("--port-decode", type=int, default=DEFAULT_DECODE_PORT)


def build_router_args(args: argparse.Namespace) -> List[str]:
    cmd = ["python3", "-m", "sglang_router.launch_router"]
    cmd += ["--pd-disaggregation", "--mini-lb"]

    cmd += ["--host", args.host]
    cmd += ["--port", str(args.port or DEFAULT_PORT)]

    cmd += ["--prefill", f"http://{args.ip_prefill}:{args.port_prefill}"]
    cmd += ["--decode", f"http://{args.ip_decode}:{args.port_decode}"]

    return cmd


# --- Other args ---


def add_other_args(parser: argparse.ArgumentParser):
    group = parser.add_argument_group("other")
    group.add_argument("--attn", type=str, default=None)
    group.add_argument("--page-size", type=int, default=None)
    group.add_argument("--max-reqs", type=int, default=None)
    group.add_argument("--mem-frac", type=float, default=None)
    group.add_argument("--dtype", type=str, default=None)
    group.add_argument("--no-cuda-graph", action="store_true")
    group.add_argument("--no-radix", action="store_true")
    group.add_argument("--log-interval", type=int, default=None)
    group.add_argument("--log-requests", action="store_true")


def build_other_args(args: argparse.Namespace, model_config: ModelInfo) -> List[str]:
    result = []

    if args.attn is not None:
        result += ["--attention-backend", args.attn]

    if model_config.reasoning_parser is not None:
        result += ["--reasoning-parser", model_config.reasoning_parser]

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


# --- Argument parser ---


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch SGLang server or router")
    add_basic_args(parser)
    add_spec_args(parser)
    add_parallelism_args(parser)
    add_disagg_args(parser)
    add_router_args(parser)
    add_other_args(parser)
    return parser.parse_args()


# --- Launchers ---


class ServerLauncher:
    """Build launch command for sglang server (normal / prefill / decode)."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.model_config = MODEL_MAP[args.model]

    def build_cmd(self) -> List[str]:
        a, m = self.args, self.model_config
        cmd = ["python3", "-m", "sglang.launch_server"]
        cmd += build_basic_args(a, m)
        cmd += build_spec_args(a, m)
        cmd += build_parallelism_args(a, m)
        cmd += build_disagg_args(a, m)
        cmd += build_other_args(a, m)
        return cmd


class RouterLauncher:
    """Build launch command for sglang router."""

    def __init__(self, args: argparse.Namespace):
        self.args = args

    def build_cmd(self) -> List[str]:
        return build_router_args(self.args)


# --- Entry point ---


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
