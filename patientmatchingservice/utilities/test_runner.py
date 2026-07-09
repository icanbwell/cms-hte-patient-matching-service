import glob
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from fastapi.testclient import TestClient

from deepdiff import DeepDiff  # For Deep Difference of 2 objects


def get_json_schema(
    obj: Dict[str, Any] | List[Dict[str, Any]]
) -> Dict[str, Any] | List[Dict[str, Any]] | str:
    """Recursively extracts the schema (structure and data types) from a JSON object."""
    if isinstance(obj, dict):
        return {key: get_json_schema(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [get_json_schema(obj[0])] if obj else []  # type: ignore[list-item]
    else:
        return type(obj).__name__


class CheckOutputProtocol(Protocol):
    def __call__(self, data: Dict[str, Any]) -> Optional[str]: ...


async def run_test_runner_async(
    data_dir: Path,
    graphql_client: TestClient,
    test_name: str,
    run_only_test: Optional[str] = None,
    validate_order_in_json: bool = True,
    validate_json_schema_only: Optional[bool] = False,
    headers: Optional[Dict[str, str]] = None,
    fn_check_output: Optional[CheckOutputProtocol] = None,
) -> None:
    print("")
    print(f"Running {test_name}")

    # read the graphql query
    graphql_folder = data_dir.joinpath("graphql")
    graphql_files: List[str] = sorted(glob.glob(str(graphql_folder.joinpath("*.gql"))))
    graphql_file: str
    graphql_file_name: str
    found_file: bool = False
    for graphql_file in graphql_files:
        graphql_file_name_with_ext = os.path.basename(graphql_file)
        graphql_file_name = os.path.splitext(graphql_file_name_with_ext)[0]
        if run_only_test and graphql_file_name != run_only_test:
            continue
        found_file = True
        with open(graphql_file) as file:
            graphql_query = file.read()

        response = graphql_client.post(
            "/graphql", json={"query": graphql_query}, headers=headers
        )
        assert response.status_code == 200
        # compare to expected json
        expected_file: Path = data_dir.joinpath("expected").joinpath(
            f"{graphql_file_name}.json"
        )
        print(f"Loading {graphql_file_name}")
        try:
            with open(expected_file) as file:
                expected_json: Dict[str, Any] = json.loads(file.read())
        except FileNotFoundError:
            # if no file matching the query file name found then look for expected.json
            expected_file = data_dir.joinpath("expected").joinpath("expected.json")
            try:
                with open(expected_file) as file:
                    expected_json = json.loads(file.read())
            except FileNotFoundError:
                # the expected file cannot be found
                raise ValueError(
                    f"No search results file found in 'expected' directory for graphql query {graphql_file_name}."
                    f"Please add the missing results file {expected_file}"
                )
        # assert
        print(f"Ran {graphql_file_name}")
        response_json = response.json()
        print(json.dumps(response_json))
        assert response_json
        assert "error" not in response_json

        if validate_json_schema_only:
            # Compare structure only by ignoring values
            response_schema = get_json_schema(response_json)
            expected_schema = get_json_schema(expected_json)
            assert (
                response_schema == expected_schema
            ), f"{graphql_file_name}: \n{response_schema}\n!=\n{expected_schema}"
            if fn_check_output is not None:
                check_output = fn_check_output(response_json)
                assert (
                    check_output is None
                ), f"{graphql_file_name} failed output check function: {check_output}"
        else:
            # assert sorted(response.json.items()) == sorted(expected_json.items())
            differences = DeepDiff(
                response_json, expected_json, ignore_order=not validate_order_in_json
            )
            if len(differences) > 0:
                print("=========== DIFFERENCES =================")
                print(f"{differences}")
                print(f"response={response_json}")
                print(f"expected={expected_json}")
                assert len(differences) == 0, f"{graphql_file_name}: {differences}"
    assert found_file, "No test file was found"
