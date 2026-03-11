import bittensor as bt


def get_dynamic_info(subtensor: bt.Subtensor, netuid: int) -> dict:
    dynamic_info = subtensor.query_runtime_api(
        "SubnetInfoRuntimeApi", "get_dynamic_info", [netuid]
    )
    return {
        "last_step_block": dynamic_info["last_step"],
        "tempo": dynamic_info["tempo"],
    }


def get_synchroized_sleep_time(
    last_step_block: int, current_block: int, tempo: int
) -> int:
    if last_step_block + (tempo // 2) >= current_block:
        return (last_step_block + (tempo // 2) - current_block) * 12
    else:
        return (last_step_block + tempo - current_block) * 12
