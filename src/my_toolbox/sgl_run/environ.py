"""Environment variable management for SGLang launches."""

import os

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
