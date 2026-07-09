from typing import Any, Dict, Optional


class ResultsProvider:
    async def get_results_async(
        self,
        *,
        query_id: str,
        query: str,  # force keyword only args from this point forward
        client: Optional[str] = None,
        test: bool = False,
    ) -> Dict[str, Any]:
        raise NotImplementedError
