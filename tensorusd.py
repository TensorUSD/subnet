"""
TensorUSD CLI — Submit your agent file to the TensorUSD subnet.

For submitting agent file:
    - python tensorusd.py submit --agent "<file location>"
"""

import os
import sys
from decimal import Decimal

import bittensor as bt
import click
import requests
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.styles import Style

from tensorusd.auth.config import settings
from tensorusd.utils.pricing import (
    dollars_to_tao,
    estimate_openai_cost_usd_from_output_ceiling,
    fetch_tao_price_usd_coingecko,
    get_known_model_pricing,
    get_model_pricing,
)

DEFAULT_BACKEND = os.environ.get("TENSORUSD_SN_BACKEND_URL", "http://localhost:8000")


# CLI group
@click.group()
@click.option(
    "--backend-url",
    default=DEFAULT_BACKEND,
    show_default=True,
    envvar="TENSORUSD_SN_BACKEND_URL",
    help="Base URL of the TensorUSD backend.",
)
@click.pass_context
def cli(ctx: click.Context, backend_url: str) -> None:
    """TensorUSD CLI — manage your TensorUSD subnet submissions."""
    ctx.ensure_object(dict)
    ctx.obj["backend_url"] = backend_url.rstrip("/")


# Helpers
def _load_wallet(wallet_name: str, hotkey_name: str, password: str) -> bt.Wallet:
    """
    Load and unlock the bittensor wallet.
    """
    try:
        wallet = bt.Wallet(name=wallet_name, hotkey=hotkey_name)
        # Decrypt the coldkey with the password — raises KeyFileError if wrong
        wallet.coldkey_file.decrypt(password)
        return wallet
    except bt.errors.KeyFileError:
        raise click.ClickException(  # noqa: B904
            f"Wrong password or corrupted keyfile for wallet '{wallet_name}'."
        )
    except Exception as exc:
        raise click.ClickException(  # noqa: B904
            f"Could not load wallet '{wallet_name}' / hotkey '{hotkey_name}':\n  {exc}\n\n"
            f"  Make sure the wallet exists:\n"
            f"    btcli wallet new_hotkey "
            f"--wallet.name {wallet_name} --wallet.hotkey {hotkey_name}"
        )


def _request_nonce(backend_url: str, hotkey: str) -> dict:
    """Call GET /v1/submissions/nonce and return the data payload."""
    try:
        r = requests.get(
            f"{backend_url}/v1/submissions/nonce",
            params={"hotkey": hotkey},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["data"]
    except requests.HTTPError as exc:
        raise click.ClickException(  # noqa: B904
            f"Failed to fetch nonce: {exc}\n{exc.response.text}"
        )
    except requests.ConnectionError:
        raise click.ClickException(  # noqa: B904
            f"Could not connect to backend at {backend_url}.\n"
            "Is the server running?  Set --backend-url or $TENSORUSD_SN_BACKEND_URL."
        )


def _request_payment_info(backend_url: str, hotkey: str) -> dict:
    """Call GET /v1/submissions/payment-info and return the data payload."""
    try:
        r = requests.get(
            f"{backend_url}/v1/submissions/payment-info",
            params={"hotkey": hotkey},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["data"]
    except requests.HTTPError as exc:
        raise click.ClickException(  # noqa: B904
            f"Failed to fetch payment info: {exc}\n{exc.response.text}"
        )
    except requests.ConnectionError:
        raise click.ClickException(  # noqa: B904
            f"Could not connect to backend at {backend_url}.\n"
            "Is the server running?  Set --backend-url or $TENSORUSD_SN_BACKEND_URL."
        )

def _check_agent_name(backend_url: str, hotkey: str, agent_name: str) -> bool:
    try:
        r = requests.get(
            f"{backend_url}/v1/submissions/agent-name/exists",
            params={"hotkey": hotkey, "agent_name": agent_name},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["data"]
    except requests.HTTPError as exc:
        raise click.ClickException( 
            f"Failed to info: {exc}\n{exc.response.text}"
        )
    except requests.ConnectionError:
        raise click.ClickException( 
            f"Could not connect to backend at {backend_url}.\n"
            "Is the server running?  Set --backend-url or $TENSORUSD_SN_BACKEND_URL."
        )
    

def _prompt_int(label: str, default: int | None = None, min_value: int = 1) -> int:
    """Prompt for integer with custom minimum value."""
    while True:
        value = click.prompt(
            click.style(label, fg="yellow"), 
            default=default, 
            type=int
        )
        if value < min_value:
            click.echo(
                click.style(
                    f"❌ {label} must be >= {min_value}", 
                    fg="red"
                )
            )
            continue
        return value


def _prompt_str(label: str) -> int:
    """Prompt for agent with custom minimum value."""
    while True:
        value = click.prompt(click.style(label, fg="yellow"), default=None, type=str)
        return value


def _prompt_model() -> str:
    available_models = [pricing.model for pricing in get_known_model_pricing()]
    selected = choice(
        message="TensorUSD Agent model selection",
        options=[(model, model) for model in available_models],
        default="salad",
            style=Style.from_dict(
                {
                    "selected-option": "bold green",
                }
            ),
    )
    if not selected:
        raise click.ClickException("No model selected.")
    return selected


# submit
@cli.command()
@click.option(
    "--agent",
    "agent_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to your agent.py file.",
)
@click.option("--wallet", "wallet_name", default=None, help="Bittensor wallet name.")
@click.option(
    "--hotkey", "hotkey_name", default=None, help="Hotkey name within the wallet."
)
@click.pass_context
def submit(
    ctx: click.Context,
    agent_file: str,
    wallet_name: str | None,
    hotkey_name: str | None,
) -> None:
    """Sign and submit an agent file to the TensorUSD subnet."""
    backend_url: str = ctx.obj["backend_url"]

    click.echo()
    click.echo(click.style("  TensorUSD — Agent Submission", fg="cyan", bold=True))
    click.echo(click.style("  " + "─" * 40, fg="cyan"))
    click.echo()

    agent_name = _prompt_str("  Enter the name of your agent (must be unique)")
        
    if not wallet_name:
        wallet_name = click.prompt(
            click.style("  Wallet name", fg="yellow"),
            default="default",
        )
    if not hotkey_name:
        hotkey_name = click.prompt(
            click.style("  Hotkey name", fg="yellow"),
            default="default",
        )

    password = click.prompt(
        click.style("  Wallet password", fg="yellow"),
        hide_input=True,
    )

    click.echo()

    with click.progressbar(length=1, label="  [1/5] Unlocking wallet   ") as bar:
        wallet = _load_wallet(wallet_name, hotkey_name, password)
        hotkey_ss58 = wallet.hotkey.ss58_address
        coldkey_ss58 = wallet.coldkey.ss58_address
        bar.update(1)

    click.echo(f"        {click.style('✓', fg='green')} hotkey  {hotkey_ss58}")
    click.echo(f"        {click.style('✓', fg='green')} coldkey {coldkey_ss58}")
    
    name_status = _check_agent_name(backend_url, hotkey_ss58, agent_name)
    
    if name_status:
        click.echo(f"   {click.style('❌', fg='red', bold = True)} Agent Name Already taken, Please enter another name!")
        sys.exit(1)
    

    with click.progressbar(length=1, label="  [2/5] Fetching nonce     ") as bar:
        nonce_data = _request_nonce(backend_url, hotkey_ss58)
        nonce = nonce_data["nonce"]
        expires_at = nonce_data["expires_at"]
        message_to_sign = nonce_data["message_to_sign"]
        bar.update(1)

    click.echo(f"        {click.style('✓', fg='green')} expires at {expires_at}")

    with click.progressbar(length=1, label="  [3/5] Signing            ") as bar:
        signature_hex = wallet.hotkey.sign(message_to_sign.encode()).hex()
        bar.update(1)

    click.echo(f"        {click.style('✓', fg='green')} {signature_hex[:32]}…")

    selected_model = _prompt_model()
    pricing = get_model_pricing(selected_model)
    estimated_input_tokens = _prompt_int("  Expected input tokens")
    estimated_output_tokens = _prompt_int("  Expected output tokens")

    estimated_cost_usd = estimate_openai_cost_usd_from_output_ceiling(
        input_tokens=estimated_input_tokens,
        output_tokens=estimated_output_tokens,
        pricing=pricing,
        eval_multiplier=3.0,
    )
    tao_price_usd = fetch_tao_price_usd_coingecko()
    estimated_cost_tao = dollars_to_tao(estimated_cost_usd, tao_price_usd)

    click.echo()
    click.echo(
        click.style(
            (
                f"  Selected model: {selected_model}\n"
                f"  Estimated evaluation API cost: ${estimated_cost_usd:.6f} "
                f"({estimated_cost_tao:.6f} TAO)"
            ),
            fg="cyan",
        )
    )
    click.echo(
        click.style(
            f"  {estimated_cost_usd:.6f} USD ({estimated_cost_tao:.6f} TAO) will be transferred to the evaluation wallet for API cost.",
            fg="yellow",
        )
    )

    payment_info = _request_payment_info(backend_url, hotkey_ss58)
    fee_target_coldkey = payment_info["fee_target_coldkey"]

    confirm = click.confirm("  Continue with TAO transfer and submission?", default=True)
    if not confirm:
        raise click.ClickException("Submission cancelled by user.")

    tx_id = ""

    with click.progressbar(length=1, label="  [4/5] Paying submission fee") as bar:
        subtensor = bt.Subtensor(network=settings.network)
        result = subtensor.transfer(
            wallet=wallet,
            destination_ss58=fee_target_coldkey,
            amount=bt.Balance.from_tao(amount=Decimal(str(estimated_cost_tao))),
        )
        tx_id = str(result.extrinsic_receipt.extrinsic_hash)
        block_hash = result.extrinsic_receipt.block_hash
        bar.update(1)

    if tx_id:
        click.echo(f"        {click.style('✓', fg='green')} tx {tx_id}")
    else:
        click.echo(f"        {click.style('✓', fg='green')} no fee required")

    click.echo("  [5/5] Uploading to backend…")

    try:
        with open(agent_file, "rb") as fh:
            response = requests.post(
                f"{backend_url}/v1/submissions/submit",
                data={
                    "agent_name": agent_name,
                    "hotkey": hotkey_ss58,
                    "coldkey": coldkey_ss58,  # stored in users table
                    "nonce": nonce,
                    "signature": signature_hex,
                    "est_input_tokens": estimated_input_tokens,
                    "est_output_tokens": estimated_output_tokens,
                    "submission_fees_tao":estimated_cost_tao,
                    "submission_fees_usd": estimated_cost_usd,
                    "model_id": selected_model,
                    "txn_hash": tx_id,
                    "block_hash": block_hash,
                },
                files={
                    "file": (os.path.basename(agent_file), fh, "text/x-python"),
                },
                timeout=120,
            )
    except requests.ConnectionError:
        raise click.ClickException(  # noqa: B904
            f"Lost connection to backend at {backend_url}."
        )

    click.echo()

    if response.status_code == 201:
        data = response.json().get("data", {})
        click.echo(click.style("  ✅  Submission accepted!", fg="green", bold=True))
        click.echo(
            f"      Submission ID : {click.style(str(data.get('submission_id')), fg='cyan')}"
        )
        click.echo(f"      Status        : {data.get('status')}")
        click.echo(f"      File          : {data.get('filename')}")
    else:
        try:
            error = response.json().get("error", {})
            detail = error.get("message") or response.text
        except Exception:
            detail = response.text

        click.echo(
            click.style(
                f"  ❌  Submission rejected  (HTTP {response.status_code})",
                fg="red",
                bold=True,
            )
        )
        click.echo(f"      Reason: {detail}")
        sys.exit(1)

    click.echo()


# Entrypoint
if __name__ == "__main__":
    cli()
