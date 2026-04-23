from .engine import TraderCopilot
from .core.structures import Candle, TradeSignal, Direction, BiasType
from .config.pairs import PAIR_CONFIGS

__version__ = "1.0.0"
__all__ = ["TraderCopilot", "Candle", "TradeSignal", "Direction", "BiasType", "PAIR_CONFIGS"]
