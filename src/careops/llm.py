import os

from langchain.chat_models import init_chat_model

from careops import meterless  # also loads CareOps/.env

# Used when CAREOPS_MODEL is not set in .env.
DEFAULT_MODEL = "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b"

NVIDIA_MAX_TOKENS = 4096
NVIDIA_TIMEOUT = 180.0


def get_model(spec: str):
        """Build a chat model from a provider spec.

        "meterless/<mode>"           -> Meterless Runtime (e.g. meterless/auto, meterless/fast)
        "<provider>:<model>"         -> LangChain's own providers, e.g.
                                        nvidia:nvidia/nemotron-3.5-lightning-30b-a3b (NVIDIA hosted API)
                                        groq:openai/gpt-oss-20b
        """
        if spec.startswith(f"{meterless.PROVIDER_ID}/"):
                return meterless.get_chat_model(spec)
        if spec.startswith("nvidia:"):
                # Nemotron reasoning models spend ~800-1000 tokens thinking before they answer or call a tool,
                # so the connector's default max_tokens=1024 cuts replies off (empty answer, no tool call).
                # The hosted API can also queue for over a minute, past the 60 s default timeout.
                return init_chat_model(spec, max_tokens=NVIDIA_MAX_TOKENS, timeout=NVIDIA_TIMEOUT)
        return init_chat_model(spec)


def default_model():
        """The model all agents use, chosen by CAREOPS_MODEL in .env (one place to switch provider)."""
        return get_model(os.environ.get("CAREOPS_MODEL") or DEFAULT_MODEL)
