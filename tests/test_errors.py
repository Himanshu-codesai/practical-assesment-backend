"""Invalid input is reported in the same error shape as every other failure."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)  # not used as a context manager, so start-up (database, Qdrant) is skipped


def test_invalid_input_uses_the_common_error_shape():
    response = client.post("/videos", json={})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "bad_request"
    assert "source_url" in error["message"]
    assert error["request_id"] == response.headers["X-Request-ID"]
