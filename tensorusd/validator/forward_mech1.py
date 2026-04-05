from neurons.validator import Validator
from tensorusd.utils.subnet import get_dynamic_info, get_synchroized_sleep_time


async def forward_mech1(self: Validator):
    dynamic_info = get_dynamic_info(self.subtensor, self.config.netuid)
    if self.is_first_run:
        self.is_first_run = False
        sleep_time = get_synchroized_sleep_time(
            dynamic_info["last_step_block"], self.block, dynamic_info["tempo"]
        )
    else:
        sleep_time = (self.tempo // 2) * 12

        # TODO: waiting for contract update
