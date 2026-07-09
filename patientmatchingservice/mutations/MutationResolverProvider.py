from typing import Any, Optional

from graphql import GraphQLResolveInfo

from patientmatchingservice.mutations.MutationProvider import MutationProvider


class MutationResolverProvider:
    def __init__(self, results_provider: MutationProvider):
        self.results_provider = results_provider

    def resolve_interacted(
        self,
        obj: Any,
        info: GraphQLResolveInfo,
        *,
        result_id: Optional[str] = None,
    ) -> bool:
        return self.results_provider.interacted(result_id=result_id)
