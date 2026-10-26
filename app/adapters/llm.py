"""Model provider selection for all agents, and a log line + trace record for every model call.

Provider order:
1. CAREOPS_MODEL, if set, e.g. meterless/auto or nvidia:nvidia/nemotron-3.5-lightning-30b-a3b
2. Meterless Nightshift (the primary provider) when METERLESS_* settings exist and the runtime is online,
   using METERLESS_MODEL (default meterless/auto)
3. NVIDIA's hosted API directly (Nemotron), e.g. when Nightshift is configured but unreachable or not
   available for this OS. The fallback and its reason are logged.
"""

import logging
import os

from langchain_core.callbacks import BaseCallbackHandler
from langchain.chat_models import init_chat_model

from app import progress
from app.adapters import meterless  # also loads CareOps/.env

log = logging.getLogger("careops.llm")

DEFAULT_MODEL = "nvidia:nvidia/nemotron-3.5-lightning-30b-a3b"

# The investigator's final answer (several findings with evidence) plus its reasoning
# overran 4096 tokens, which comes back as an empty reply with finish_reason=length.
NVIDIA_MAX_TOKENS = 16384
NVIDIA_TIMEOUT = 240.0  # per model request; the hosted API sometimes queues, and reviewing many orders takes long


def get_model(spec: str):
        """Build a chat model from a provider spec.

        "meterless/<mode>"           -> Meterless Runtime (e.g. meterless/auto, meterless/fast)
        "<provider>:<model>"         -> a LangChain provider whose package is installed, e.g.
                                        nvidia:nvidia/nemotron-3.5-lightning-30b-a3b (NVIDIA hosted API)
        """
        if spec.startswith(f"{meterless.PROVIDER_ID}/"):
                return meterless.get_chat_model(spec)
        if spec.startswith("nvidia:"):
                # Nemotron reasoning models think before they answer or call a tool; a reply that runs
                # out of tokens comes back EMPTY. The connector default (1024) is far too low.
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


_resolved: tuple[str, str] | None = None


def resolve_model_spec() -> tuple[str, str]:
        """(spec, reason) for the model every agent uses. Decided once per process."""
        global _resolved
        if _resolved:
                return _resolved
        if os.environ.get("CAREOPS_MODEL"):
                _resolved = (os.environ["CAREOPS_MODEL"], "set by CAREOPS_MODEL")
        elif meterless.is_configured():
                spec = os.environ.get("METERLESS_MODEL") or "meterless/auto"
                status = meterless.check_status(meterless.configured_base_url(), meterless.read_token())
                if status.state == "online":
                        _resolved = (spec, f"Meterless Nightshift online at {status.base_url} (primary provider)")
                else:
                        _resolved = (DEFAULT_MODEL, f"Meterless Nightshift {status.state} at {status.base_url} "
                                                    f"({status.detail or 'no detail'}); falling back to NVIDIA hosted API")
        else:
                _resolved = (DEFAULT_MODEL, "no Meterless Nightshift configured; using NVIDIA hosted API")
        (log.warning if "falling back" in _resolved[1] else log.info)("model: %s (%s)", *_resolved)
        return _resolved


class ModelCallLogger(BaseCallbackHandler):
        """Logs which model actually answered each call, so a trace shows where Nemotron did the work."""

        def __init__(self, agent: str):
                self.agent = agent

        def on_llm_end(self, response, **kwargs) -> None:
                generation = response.generations[0][0] if response.generations and response.generations[0] else None
                message = getattr(generation, "message", None)
                metadata = getattr(message, "response_metadata", None) or {}
                model = metadata.get("model_name") or metadata.get("model") or (response.llm_output or {}).get("model_name")
                completion = describe_completion(message)
                log.info("%s model call: %s, %s", self.agent, model, completion)
                progress.emit("llm_call", agent=self.agent, model=model, finish=metadata.get("finish_reason"),
                              completion=completion)


def default_model(agent: str = "agent"):
        """The model for one agent: the resolved provider, with per-call model logging attached."""
        model = get_model(resolve_model_spec()[0])
        model.callbacks = [ModelCallLogger(agent)]
        return model
