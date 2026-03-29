"""
Historical data storage using Parquet format.
Adapted from Jon-Becker/prediction-market-analysis.
"""

from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

import config


class ParquetStore:
    """Read/write historical market data in Parquet format."""

    def __init__(self, data_dir: Path = config.DATA_DIR):
        self.data_dir = data_dir
        self.data_dir.mkdir(exist_ok=True)

    def save_trades(self, trades: pd.DataFrame, filename: str = "trades.parquet"):
        path = self.data_dir / filename
        trades.to_parquet(path, index=False, engine="pyarrow")
        logger.debug(f"Saved {len(trades)} trades to {path}")

    def load_trades(self, filename: str = "trades.parquet") -> Optional[pd.DataFrame]:
        path = self.data_dir / filename
        if not path.exists():
            return None
        return pd.read_parquet(path, engine="pyarrow")

    def save_markets(self, markets: pd.DataFrame, filename: str = "markets.parquet"):
        path = self.data_dir / filename
        markets.to_parquet(path, index=False, engine="pyarrow")

    def load_markets(self, filename: str = "markets.parquet") -> Optional[pd.DataFrame]:
        path = self.data_dir / filename
        if not path.exists():
            return None
        return pd.read_parquet(path, engine="pyarrow")

    def load_external_dataset(self, dataset_path: Path) -> Optional[pd.DataFrame]:
        """Load data from Jon-Becker's pre-collected dataset."""
        if not dataset_path.exists():
            logger.warning(f"Dataset not found: {dataset_path}")
            return None
        try:
            return pd.read_parquet(dataset_path, engine="pyarrow")
        except Exception as e:
            logger.error(f"Failed to load dataset {dataset_path}: {e}")
            return None
