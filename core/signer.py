"""
EIP-712 order signing for Polymarket CLOB.
Adapted from discountry/polymarket-trading-bot/src/signer.py
"""

from eth_account import Account
from eth_account.messages import encode_typed_data

# Polymarket CTF Exchange contract on Polygon
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
COLLATERAL_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon

# EIP-712 domain
DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": 137,
    "verifyingContract": CTF_EXCHANGE_ADDRESS,
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
        "domain": DOMAIN,
        "primaryType": "Order",
        "message": order,
    }
    signed = Account.sign_typed_data(private_key, full_message=structured_data)
    return signed.signature.hex()


def get_address(private_key: str) -> str:
    """Get the Ethereum address for a private key."""
    account = Account.from_key(private_key)
    return account.address
