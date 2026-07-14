"""Validator package exports.

This package initializer intentionally avoids eager imports.

Reason:
- utility modules such as ``tensorusd.utils.core`` import specific modules from
  ``tensorusd.validator``
- eager imports here pull in ``forward``
- ``forward`` imports ``neurons.validator``
- ``neurons.validator`` imports ``neurons.validator.agent`` which imports
  ``tensorusd.utils.core`` again, creating a circular import during validator
  startup

Import concrete validator submodules directly where needed, e.g.:

    from tensorusd.validator.delayed_evaluation import DelayedEvaluator
    from tensorusd.validator.reward import get_auction_rewards_from_db
"""

__all__ = [
    "forward",
    "get_auction_rewards_from_db",
    "ValidatorEventListener",
    "forward_mech1",
]
