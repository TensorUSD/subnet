"""
TensorUSD CLI — Submit your agent file to the TensorUSD subnet.

For submitting agent file:
    - python tensorusd.py submit --agent "<file location>"
"""

import os
import sys

import bittensor as bt
import click
import requests

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

    with click.progressbar(length=1, label="  [1/4] Unlocking wallet   ") as bar:
        wallet = _load_wallet(wallet_name, hotkey_name, password)
        hotkey_ss58 = wallet.hotkey.ss58_address
        coldkey_ss58 = wallet.coldkey.ss58_address
        bar.update(1)

    click.echo(f"        {click.style('✓', fg='green')} hotkey  {hotkey_ss58}")
    click.echo(f"        {click.style('✓', fg='green')} coldkey {coldkey_ss58}")

    with click.progressbar(length=1, label="  [2/4] Fetching nonce     ") as bar:
        nonce_data = _request_nonce(backend_url, hotkey_ss58)
        nonce = nonce_data["nonce"]
        expires_at = nonce_data["expires_at"]
        message_to_sign = nonce_data["message_to_sign"]
        bar.update(1)

    click.echo(f"        {click.style('✓', fg='green')} expires at {expires_at}")

    with click.progressbar(length=1, label="  [3/4] Signing            ") as bar:
        signature_hex = wallet.hotkey.sign(message_to_sign.encode()).hex()
        bar.update(1)

    click.echo(f"        {click.style('✓', fg='green')} {signature_hex[:32]}…")

    click.echo("  [4/4] Uploading to backend…")

    try:
        with open(agent_file, "rb") as fh:
            response = requests.post(
                f"{backend_url}/v1/submissions/submit",
                data={
                    "hotkey": hotkey_ss58,
                    "coldkey": coldkey_ss58,  # stored in users table
                    "nonce": nonce,
                    "signature": signature_hex,
                },
                files={
                    "file": (os.path.basename(agent_file), fh, "text/x-python"),
                },
                timeout=60,
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
