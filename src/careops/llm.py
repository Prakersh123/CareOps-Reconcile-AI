import os

from langchain.chat_models import init_chat_model

from careops import meterless  # also loads CareOps/.env

# Used when CAREOPS_MODEL is not set in .env.
DEFAULT_MODEL = "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b"

# The investigator's final answer (several findings with evidence) plus its reasoning
# overran 4096 tokens, which comes back as an empty reply with finish_reason=length.
NVIDIA_MAX_TOKENS = 16384
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
                # Nemotron reasoning models think before they answer or call a tool; a reply that runs
                # out of tokens comes back EMPTY. The connector default (1024) is far too low.
                # The hosted API can also queue for over a minute, past the 60 s default timeout.
                return init_chat_model(spec, max_completion_tokens=NVIDIA_MAX_TOKENS, timeout=NVIDIA_TIMEOUT)
        return init_chat_model(spec)


def describe_completion(message) -> str:
        """Why a model reply ended and what it cost, for logs: 'finish=length, output 16384 tokens (reasoning 15900)'."""
        metadata = getattr(message, "response_metadata", None) or {}
        usage = metadata.get("token_usage") or {}
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        output = usage.get("completion_tokens")
        text = f"finish={metadata.get('finish_reason', '?')}"
        if output is not None:
                text += f", output {output} tokens"
                if reasoning is not None:
                        text += f" (reasoning {reasoning})"
        return text


def default_model():
        """The model all agents use, chosen by CAREOPS_MODEL in .env (one place to switch provider)."""
        return get_model(os.environ.get("CAREOPS_MODEL") or DEFAULT_MODEL)
