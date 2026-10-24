"""Public typed decision contract; fixtures are synthetic, never ML results."""

import pytest
from pydantic import ValidationError

from plugboard import api_contract


def test_decisions_publish_authenticated_routes_and_the_runtime_request_schema():
    document = api_contract.build_openapi()
    for method, path in [("get", "/v1/decisions/models"), ("post", "/v1/decisions")]:
        assert path in document["paths"], f"Missing decision contract path {path}"
        operation = document["paths"][path][method]
        assert operation["security"] == [{"bearer": []}]
    schema = document["components"]["schemas"]["DecisionRequest"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["questions"]["type"] == "object"
    assert set(schema["required"]) == {"model", "state", "questions"}


def test_public_contract_has_only_supported_decision_ids():
    schemas = api_contract.build_openapi()["components"]["schemas"]
    for schema_name, field in [
        ("DecisionRequest", "model"),
        ("DecisionResponse", "model"),
        ("DecisionModelEntry", "id"),
    ]:
        assert set(schemas[schema_name]["properties"][field]["enum"]) == {
            "jev",
            "laya",
            "laya-multilingual",
        }


def test_response_contract_describes_each_primitive_and_estimated_billing():
    response_model = api_contract.RESPONSE_MODELS.get(("POST", "/v1/decisions"))
    assert response_model is not None, "Decision response must be documented"
    body = {
        "model": "jev",
        "answers": {
            "route": {
                "type": "choice",
                "choice": "yes",
                "confidence": 0.8,
                "probabilities": {"yes": 0.8, "no": 0.2},
            },
            "certain": {"type": "noul", "noul": 0.7},
            "rating": {
                "type": "score",
                "score": 0.4,
                "confidence": 0.2,
                "probabilities": {"0": 0.6, "1": 0.4},
                "legend": {"0": "bad", "1": "good"},
            },
        },
        "billing": {"status": "estimated", "cost_usd": 0.002688},
    }
    assert response_model.model_validate(body).model_dump() == body
    body["answers"]["certain"]["noul"] = 1.1
    with pytest.raises(ValidationError):
        response_model.model_validate(body)
