from src.execution.base import ExecutionClient, OrderRequest, OrderResult
from src.execution.paper import PaperExecutionClient
from src.paper_trading import PaperTradingEngine

EXECUTION_MODES = ("paper",)


def build_execution_client(mode: str | None, book: PaperTradingEngine) -> ExecutionClient:
    """Only paper execution exists. Any other mode is refused rather than
    silently simulated, so nobody can believe live orders are being sent."""
    mode = (mode or "paper").lower()
    if mode == "paper":
        return PaperExecutionClient(book)
    raise ValueError(
        f"execution_mode {mode!r} is not available. Only 'paper' (simulation) is "
        "implemented; live broker execution has not been built."
    )


__all__ = [
    "EXECUTION_MODES",
    "ExecutionClient",
    "OrderRequest",
    "OrderResult",
    "PaperExecutionClient",
    "build_execution_client",
]
