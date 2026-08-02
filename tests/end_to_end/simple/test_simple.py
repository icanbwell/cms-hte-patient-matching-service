from pathlib import Path

from fastapi.testclient import TestClient

from patient_matching_service.utilities.test_runner import run_test_runner_async


async def test_simple(graphql_client: TestClient) -> None:
    # Arrange
    data_dir: Path = Path(__file__).parent.joinpath("./")

    await run_test_runner_async(
        data_dir=data_dir,
        graphql_client=graphql_client,
        test_name="test_simple",
    )
