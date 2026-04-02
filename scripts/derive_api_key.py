"""
Derive Polymarket L2 API credentials from the configured wallet key.

Usage:
    python -m scripts.derive_api_key
"""

import asyncio
import sys

from core.bot import TradingBot
import config


async def _run():
    if not config.POLYGON_WALLET_PRIVATE_KEY:
        raise ValueError(
            "POLYGON_WALLET_PRIVATE_KEY is required in .env to derive L2 credentials."
        )

    bot = TradingBot()
    try:
        creds = await bot.derive_api_credentials()
        output_path = config.BASE_DIR / ".derived_poly_api.env"
        output_path.write_text(
            "\n".join(
                [
                    "# Temporary Polymarket L2 credentials",
                    f"POLY_API_KEY={creds.api_key}",
                    f"POLY_API_SECRET={creds.secret}",
                    f"POLY_API_PASSPHRASE={creds.passphrase}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        print(
            f"Derived credentials written to {output_path.resolve()}",
            file=sys.stderr,
        )
        print(
            "Move them into .env and delete the temporary file when done.",
            file=sys.stderr,
        )
    finally:
        await bot.close()


def main():
    try:
        asyncio.run(_run())
    except Exception as e:
        print(f"Failed to derive credentials: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
