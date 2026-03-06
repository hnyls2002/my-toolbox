"""Environment variable management for SGLang launches."""

import os

import tabulate

SGLANG_DEFAULT_ENVS: dict[str, str] = {
    "SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN": "1",
    "SGLANG_TORCH_PROFILER_DIR": "./",
    "SGLANG_ENABLE_JIT_DEEPGEMM": "0",
}

SGLANG_DISPLAY_ENVS: list[tuple[str, str]] = [
    ("SGLANG_ENABLE_SPEC_V2", "0"),
    ("CUDA_LAUNCH_BLOCKING", "0"),
    ("SGLANG_TORCH_PROFILER_DIR", "./"),
    ("SGLANG_ENABLE_JIT_DEEPGEMM", "0"),
    ("SGLANG_ENABLE_STRICT_MEM_CHECK_DURING_BUSY", "0"),
]


def set_default_envs():
    for key, value in SGLANG_DEFAULT_ENVS.items():
        os.environ.setdefault(key, value)


def print_launch_envs():
    env_status = [
        (name, os.getenv(name, default)) for name, default in SGLANG_DISPLAY_ENVS
    ]
    env_msg = tabulate.tabulate(
        env_status, headers=["Variable", "Value"], tablefmt="fancy_grid"
    )
    print(env_msg)
