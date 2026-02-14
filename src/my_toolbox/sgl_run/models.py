"""Model registry for SGLang launches."""

import dataclasses
from typing import Dict, Optional, Union


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
