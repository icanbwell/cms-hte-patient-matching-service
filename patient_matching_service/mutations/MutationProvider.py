from typing import Optional


class MutationProvider:
    def interacted(
        self,
        *,  # force keyword only args from this point forward
        result_id: Optional[str],
    ) -> bool:
        return True
