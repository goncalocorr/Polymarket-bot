"""
EIP-712 order signing and CLOB authentication for Polymarket.
Adapted from discountry/polymarket-trading-bot/src/signer.py
"""

import time
from typing import Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

# Polymarket CTF Exchange contract on Polygon
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
COLLATERAL_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon

# EIP-712 domain for order signing
ORDER_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,
    "verifyingContract": CTF_EXCHANGE_ADDRESS,
}

# EIP-712 domain for auth message signing (L1 → L2 credential derivation)
AUTH_DOMAIN = {
    "name": "ClobAuthDomain",
    "version": "1",
    "chainId": 137,
}

ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ],
}

AUTH_TYPES = {
    "ClobAuth": [
        {"name": "address", "type": "address"},
        {"name": "timestamp", "type": "string"},
        {"name": "nonce", "type": "uint256"},
        {"name": "message", "type": "string"},
    ],
}


def sign_order(order: dict, private_key: str) -> str:
    """
    Sign an order using EIP-712 typed data signing.

    Args:
        order: Order dict with all required fields.
        private_key: Hex-encoded private key.

    Returns:
        Hex-encoded signature string.
    """
    structured_data = {
        "types": ORDER_TYPES,
        "domain": ORDER_DOMAIN,
        "primaryType": "Order",
        "message": order,
    }
    signed = Account.sign_typed_data(private_key, full_message=structured_data)
    return signed.signature.hex()


def sign_auth_message(
    private_key: str,
    timestamp: Optional[str] = None,
    nonce: int = 0,
) -> str:
    """
    Sign an EIP-712 authentication message for L2 API credential derivation.

    The Polymarket CLOB requires L2 API credentials (api_key, secret, passphrase)
    for authenticated endpoints. These are obtained by signing an auth message
    with your private key (L1) and calling /auth/derive-api-key.

    Args:
        private_key: Hex-encoded private key.
        timestamp: Unix timestamp string (defaults to current time).
        nonce: Auth nonce (usually 0).

    Returns:
        Hex-encoded signature string with 0x prefix.
    """
    if timestamp is None:
        timestamp = str(int(time.time()))

    address = get_address(private_key)

    message_data = {
        "address": address,
        "timestamp": timestamp,
        "nonce": nonce,
        "message": "This message attests that I control the given wallet",
    }

    signable = encode_typed_data(
        domain_data=AUTH_DOMAIN,
        message_types=AUTH_TYPES,
        message_data=message_data,
    )

    account = Account.from_key(private_key)
    signed = account.sign_message(signable)
    return "0x" + signed.signature.hex()


def get_address(private_key: str) -> str:
    """Get the Ethereum address for a private key."""
    account = Account.from_key(private_key)
    return account.address
