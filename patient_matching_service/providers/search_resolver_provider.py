from typing import Any, Dict, Optional

from graphql import GraphQLResolveInfo

from patient_matching_service.providers.results_provider import ResultsProvider


class SearchResolverProvider:
    def __init__(self, results_provider: ResultsProvider):
        self.results_provider = results_provider

    async def resolve_async(
        self,
        obj: Any,
        info: GraphQLResolveInfo,
        *,  # force keyword only args from this point forward
        query_id: str,
        query: str,
        client: Optional[str] = None,
        test: bool = False,
    ) -> Dict[str, Any]:
        """
        query_id: str,
        query: str,
        client: Optional[str] = None
        """
        return await self.results_provider.get_results_async(
            query_id=query_id, query=query, client=client, test=test
        )
