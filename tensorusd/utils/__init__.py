"""Utility package for TensorUSD.

This module intentionally avoids eager submodule imports.

Reason:
- sandboxed agent code may import a narrow helper such as
  ``tensorusd.utils.openai_runtime``
- eager imports here would also load unrelated modules like ``config``
  that bring in bittensor/substrate dependencies
- those dependencies are not required for sandbox inference and can trigger
  avoidable import-time conflicts inside the sandbox image

Callers should import required submodules directly, e.g.:

    from tensorusd.utils import config
    from tensorusd.utils.openai_runtime import build_agent_openai_client
"""

__all__ = ["config", "misc", "uids"]
