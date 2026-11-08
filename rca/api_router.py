import json
import os
import time
from typing import Any, Dict, Optional

_DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")
_MODEL_KEY = "gemini3pro"

def _load_config() -> Dict[str, Any]:
    config_path = os.environ.get("API_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.path.dirname(__file__), "..", config_path)
    
    with open(config_path, "r") as f:
        meta_config = json.load(f)
    
    model_config = meta_config.get(_MODEL_KEY, {})
    
    return {
        "MODEL": model_config.get("model", _MODEL_KEY),
        "API_KEY": model_config.get("api_key"),
        "API_BASE": model_config.get("base_url"),
        "TEMPERATURE": model_config.get("temperature", 0.0),
    }

configs = _load_config()

# Anthropic models need explicit cache breakpoints; OpenAI-compatible gateways
# such as DigitalOcean Gradient pass `cache_control` through on content blocks.
_CACHE_BREAKPOINTS = 2


def _supports_prompt_cache(model: str) -> bool:
    return "anthropic" in model.lower() or "claude" in model.lower()


def _with_cache_breakpoints(messages):
    """Return a copy of `messages` with cache breakpoints on the last two messages.

    The agent resends its whole history on every call, and both the controller and
    the executor append a temporary `note` message that is not kept in the history.
    Marking only the last message would cache a prefix ending in that note, which
    the next call never repeats. The second-to-last breakpoint caches the history
    itself, which the next call does start with.

    The input is copied, never mutated: callers reuse and extend these lists, and
    accumulated markers would exceed Anthropic's limit of 4 breakpoints per request.
    """
    marked = list(messages)
    for i in range(max(0, len(marked) - _CACHE_BREAKPOINTS), len(marked)):
        message = marked[i]
        content = message.get("content")
        if isinstance(content, str):
            if not content:
                continue  # empty text blocks are rejected
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list) and content and isinstance(content[-1], dict):
            blocks = [dict(block) for block in content]
        else:
            continue
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        marked[i] = {**message, "content": blocks}
    return marked


def get_chat_completion(messages, temperature: Optional[float] = None, tools=None, parallel_tool_calls: bool = False):
    from openai import OpenAI

    if temperature is None:
        temperature = configs["TEMPERATURE"]

    client_args = {"api_key": configs["API_KEY"]}
    if configs["API_BASE"]:
        client_args["base_url"] = configs["API_BASE"]
    client = OpenAI(**client_args)

    if _supports_prompt_cache(configs["MODEL"]):
        messages = _with_cache_breakpoints(messages)

    request_args: Dict[str, Any] = {
        "model": configs["MODEL"],
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        request_args["tools"] = tools
        request_args["parallel_tool_calls"] = parallel_tool_calls

    for _ in range(3):
        try:
            return client.chat.completions.create(**request_args).choices[0].message.content
        except Exception as e:
            print(e)
            if "429" in str(e):
                print("Rate limit exceeded. Waiting for 1 second.")
                time.sleep(1)
                continue
            raise e

if __name__ == "__main__":
    print(f"Model: {configs['MODEL']}")
    response = get_chat_completion([{"role": "user", "content": "123+321=?"}])
    print(response)