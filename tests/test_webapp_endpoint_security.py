"""Regression tests for request-controlled LLM endpoints and credentials."""
from __future__ import annotations

import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
import dataset_recommender.llm.llm_client as llm_client  # noqa: E402
from dataset_recommender.llm.llm_client import load_llm_config  # noqa: E402


client = TestClient(webapp.app, base_url="http://127.0.0.1")


def _server_openai_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BIODATA_LLM_ENV_FILE",
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "OPENAI_BASE_URL",
        "ZHIPUAI_BASE_URL",
        "BIGMODEL_BASE_URL",
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "ZAI_API_KEY",
        "ZHIPUAI_API_KEY",
        "ZHIPUAI_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "server-secret")
    monkeypatch.setenv("LLM_BASE_URL", "https://trusted.example/v1")


def _capture_diagnose(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    captured: list[object] = []

    def fake_health(config):
        captured.append(config)
        return {"Connection": "success"}

    monkeypatch.setattr(webapp, "healthcheck", fake_health)
    monkeypatch.setattr(webapp, "diagnose_network", lambda _config: {"Network": "skipped"})
    return captured


def _diagnose_json(**overrides) -> dict:
    payload = {
        "provider": "openai-compatible",
        "use_llm": True,
        "mock_llm": False,
        "base_url": "https://trusted.example/v1",
        "model": "test-model",
    }
    payload.update(overrides)
    return payload


def test_diagnose_is_post_only(monkeypatch: pytest.MonkeyPatch):
    captured = _capture_diagnose(monkeypatch)
    response = client.get("/api/diagnose")
    assert response.status_code == 405
    assert captured == []


def test_diagnose_post_accepts_absent_and_matching_origin(monkeypatch: pytest.MonkeyPatch):
    _server_openai_config(monkeypatch)
    captured = _capture_diagnose(monkeypatch)

    without_origin = client.post("/api/diagnose", json=_diagnose_json())
    matching_origin = client.post(
        "/api/diagnose",
        json=_diagnose_json(),
        headers={"Origin": "http://127.0.0.1"},
    )

    assert without_origin.status_code == 200
    assert matching_origin.status_code == 200
    assert len(captured) == 2
    assert all(config.api_key == "server-secret" for config in captured)


def test_diagnose_rejects_cross_origin_before_network(monkeypatch: pytest.MonkeyPatch):
    _server_openai_config(monkeypatch)
    captured = _capture_diagnose(monkeypatch)
    response = client.post(
        "/api/diagnose",
        json=_diagnose_json(),
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert captured == []


def test_diagnose_network_calls_happen_outside_env_lock(monkeypatch: pytest.MonkeyPatch):
    """触发点审计 F1：healthcheck / diagnose_network 的网络 I/O 绝不发生在 ENV_LOCK 内——
    调用发生的那一刻，锁必须能被其他请求立即获取（诊断期间全进程 LLM 端点不排队）。"""
    _server_openai_config(monkeypatch)
    lock_free_at_call: list[bool] = []

    def _lock_is_free() -> bool:
        acquired = webapp.ENV_LOCK.acquire(blocking=False)
        if acquired:
            webapp.ENV_LOCK.release()
        return acquired

    def fake_health(_config):
        lock_free_at_call.append(_lock_is_free())
        return {"Connection": "success"}

    def fake_network(_config):
        lock_free_at_call.append(_lock_is_free())
        return {"Network": "skipped"}

    monkeypatch.setattr(webapp, "healthcheck", fake_health)
    monkeypatch.setattr(webapp, "diagnose_network", fake_network)

    response = client.post("/api/diagnose", json=_diagnose_json())
    assert response.status_code == 200, response.text
    assert lock_free_at_call == [True, True]


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///etc/passwd",
        "http://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?target=other",
        "https://api.example.com/v1#fragment",
        "https://localhost/v1",
        "https://192.168.1.9/v1",
        "https://169.254.10.20/v1",
        "https://224.0.0.1/v1",
        "https://0.0.0.0/v1",
        "https://127.1/v1",
        "https://0x7f.0x0.0x0.0x1/v1",
        "https://service.internal/v1",
    ],
)
def test_dangerous_endpoint_is_rejected_before_network(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
):
    _server_openai_config(monkeypatch)
    captured = _capture_diagnose(monkeypatch)
    response = client.post("/api/diagnose", json=_diagnose_json(base_url=base_url))
    assert response.status_code == 400, response.text
    assert captured == []


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
        "http://[::1]:11434/v1",
    ],
)
def test_loopback_http_is_allowed_with_request_key(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
):
    _server_openai_config(monkeypatch)
    captured = _capture_diagnose(monkeypatch)
    response = client.post(
        "/api/diagnose",
        json=_diagnose_json(base_url=base_url, api_key="request-secret"),
    )
    assert response.status_code == 200, response.text
    assert captured[0].base_url == base_url
    assert captured[0].api_key == "request-secret"


def test_custom_diagnose_endpoint_cannot_inherit_server_key(monkeypatch: pytest.MonkeyPatch):
    _server_openai_config(monkeypatch)
    captured = _capture_diagnose(monkeypatch)
    response = client.post(
        "/api/diagnose",
        json=_diagnose_json(base_url="https://custom.example/v1", api_key=None),
    )
    assert response.status_code == 200
    assert captured[0].base_url == "https://custom.example/v1"
    assert captured[0].api_key is None


def test_request_key_overrides_high_priority_server_key(monkeypatch: pytest.MonkeyPatch):
    _server_openai_config(monkeypatch)
    captured = _capture_diagnose(monkeypatch)
    response = client.post(
        "/api/diagnose",
        json=_diagnose_json(base_url="https://custom.example/v1", api_key="request-secret"),
    )
    assert response.status_code == 200
    assert captured[0].api_key == "request-secret"


def _empty_meta() -> SimpleNamespace:
    return SimpleNamespace(
        answer="",
        pipeline="rules",
        llm_attempted=False,
        llm_succeeded=False,
        llm_response_used=False,
        llm_provider=None,
        llm_mode=None,
        prompt_name=None,
        fallback=False,
        fallback_reason=None,
        retrieved_data=[],
        relaxation_options=[],
    )


def test_recommend_uses_the_same_key_isolation(monkeypatch: pytest.MonkeyPatch):
    _server_openai_config(monkeypatch)
    captured = []

    class FakeWorkflow:
        def __init__(self):
            captured.append(load_llm_config(project_root=webapp.PROJECT_ROOT))

        def run_with_meta(self, *_args, **_kwargs):
            return _empty_meta()

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", FakeWorkflow)

    no_request_key = client.post(
        "/api/recommend",
        json={
            "query": "human data",
            "provider": "openai-compatible",
            "use_llm": True,
            "base_url": "https://custom.example/v1",
        },
    )
    with_request_key = client.post(
        "/api/recommend",
        json={
            "query": "human data",
            "provider": "openai-compatible",
            "use_llm": True,
            "base_url": "https://custom.example/v1",
            "api_key": "request-secret",
        },
    )

    assert no_request_key.status_code == 200, no_request_key.text
    assert with_request_key.status_code == 200, with_request_key.text
    assert captured[0].base_url == "https://custom.example/v1"
    assert captured[0].api_key is None
    assert captured[1].api_key == "request-secret"


def test_recommend_accepts_absent_and_matching_origin(monkeypatch: pytest.MonkeyPatch):
    constructed = []

    class FakeWorkflow:
        def __init__(self):
            constructed.append(True)

        def run_with_meta(self, *_args, **_kwargs):
            return _empty_meta()

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", FakeWorkflow)
    payload = {"query": "human data", "use_llm": False}
    without_origin = client.post("/api/recommend", json=payload)
    matching_origin = client.post(
        "/api/recommend",
        json=payload,
        headers={"Origin": "http://127.0.0.1"},
    )

    assert without_origin.status_code == 200
    assert matching_origin.status_code == 200
    assert len(constructed) == 2


def test_recommend_rejects_cross_origin_before_workflow(monkeypatch: pytest.MonkeyPatch):
    constructed = []

    class ShouldNotRun:
        def __init__(self):
            constructed.append(True)

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", ShouldNotRun)
    response = client.post(
        "/api/recommend",
        json={"query": "human data", "use_llm": True},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert constructed == []


def test_other_browser_posts_share_the_same_origin_contract():
    interpret = client.post(
        "/api/interpret",
        json={"query": "human data"},
        headers={"Origin": "https://evil.example"},
    )
    upload = client.post(
        "/api/upload",
        files={"file": ("private.json", b"[]", "application/json")},
        headers={"Origin": "https://evil.example"},
    )
    board_plan = client.post(
        "/api/board/plan",
        json={"query": "human data", "utterance": "换成小鼠"},
        headers={"Origin": "https://evil.example"},
    )
    assert interpret.status_code == 403
    assert upload.status_code == 403
    assert board_plan.status_code == 403


def test_every_post_route_checks_the_origin():
    """结构性断言，取代逐条枚举。

    枚举挡不住「新加了一个端点但忘了那一行」——`/api/reuse-pack` 就这样漏了很久：
    它是当时 21 个路由里唯一没调这一行的。改成扫描全部 POST 处理函数，
    加新端点时忘了写就会在这里红，而不是等某天被人发现。
    """
    import ast
    from pathlib import Path

    source = Path(webapp.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    missing = []
    checked = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_post = False
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if isinstance(func, ast.Attribute) and func.attr in ("post", "put", "patch", "delete"):
                is_post = True
            if isinstance(func, ast.Attribute) and func.attr == "api_route":
                for keyword in decorator.keywords:
                    if keyword.arg == "methods" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                        values = {getattr(e, "value", None) for e in keyword.value.elts}
                        if values & {"POST", "PUT", "PATCH", "DELETE"}:
                            is_post = True
        if not is_post:
            continue
        checked.append(node.name)
        calls = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        if "_require_same_origin" not in calls:
            missing.append(node.name)

    assert len(checked) >= 8, f"只扫到 {len(checked)} 个写入端点，扫描逻辑可能失效了"
    assert not missing, f"这些写入端点没有同源检查：{missing}"


def test_dns_rebinding_host_is_rejected_before_workflow_network_or_write(
    monkeypatch: pytest.MonkeyPatch,
):
    touched = []

    class ShouldNotRun:
        def __init__(self):
            touched.append("workflow")

    def forbidden(*_args, **_kwargs):
        touched.append("network-or-write")
        raise AssertionError("rebound request reached a protected side effect")

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", ShouldNotRun)
    monkeypatch.setattr(webapp, "healthcheck", forbidden)
    monkeypatch.setattr(webapp, "diagnose_network", forbidden)
    monkeypatch.setattr(webapp, "_new_upload_name", forbidden)
    headers = {"Host": "evil.example", "Origin": "http://evil.example"}

    responses = [
        client.post("/api/interpret", json={"query": "human data"}, headers=headers),
        client.post(
            "/api/recommend",
            json={"query": "human data", "use_llm": True},
            headers=headers,
        ),
        client.post("/api/diagnose", json=_diagnose_json(), headers=headers),
        client.post(
            "/api/upload",
            files={"file": ("private.json", b"[]", "application/json")},
            headers=headers,
        ),
        client.post(
            "/api/recommend",
            json={"query": "human data", "use_llm": False},
            headers={"Host": "evil.example"},
        ),
    ]
    assert [response.status_code for response in responses] == [403, 403, 403, 403, 403]
    assert touched == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/health",
        "/api/datasets",
        "/api/files?dataset_id=missing",
        "/api/introduction?dataset_id=missing",
    ],
)
def test_dns_rebinding_host_is_rejected_for_read_routes(path: str):
    response = client.get(path, headers={"Host": "evil.example"})
    assert response.status_code == 403


def test_loopback_host_is_accepted_for_read_routes():
    response = client.get("/api/health", headers={"Host": "127.0.0.1"})
    assert response.status_code == 200
    assert response.json()["version"] == webapp.WEB_API_VERSION


def test_recommend_rejects_unsafe_endpoint_before_workflow(monkeypatch: pytest.MonkeyPatch):
    _server_openai_config(monkeypatch)
    constructed = []

    class ShouldNotRun:
        def __init__(self):
            constructed.append(True)

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", ShouldNotRun)
    response = client.post(
        "/api/recommend",
        json={
            "query": "human data",
            "provider": "openai-compatible",
            "use_llm": True,
            "base_url": "https://10.0.0.8/v1",
        },
    )
    assert response.status_code == 400
    assert constructed == []


def test_equivalent_trusted_endpoint_keeps_server_key(monkeypatch: pytest.MonkeyPatch):
    _server_openai_config(monkeypatch)
    overrides = webapp._build_request_overrides(
        provider="openai-compatible",
        use_llm=True,
        mock_llm=False,
        api_key=None,
        base_url="https://TRUSTED.example:443/v1/",
        server_provider="openai-compatible",
        server_base_url="https://trusted.example/v1",
    )
    assert not any(key in overrides for key in webapp._LLM_SECRET_ENV_KEYS)
    with webapp._temporary_env(overrides):
        config = load_llm_config(project_root=webapp.PROJECT_ROOT)
        assert config.api_key == "server-secret"


def test_override_builder_shadows_dotenv_secrets_for_untrusted_endpoint(monkeypatch: pytest.MonkeyPatch):
    _server_openai_config(monkeypatch)
    overrides = webapp._build_request_overrides(
        provider="openai-compatible",
        use_llm=True,
        mock_llm=False,
        api_key=None,
        base_url="https://custom.example/v1",
        server_provider="openai-compatible",
        server_base_url="https://trusted.example/v1",
    )
    assert all(overrides[key] == "" for key in webapp._LLM_SECRET_ENV_KEYS)
    with webapp._temporary_env(overrides):
        config = load_llm_config(project_root=webapp.PROJECT_ROOT)
        assert config.api_key is None


def _server_zhipu_generic_key_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server configured for zhipuai via a *generic* ``LLM_API_KEY`` and no
    pinned ``LLM_BASE_URL`` — the one config under which the pre-fix
    provider-relative scoping would leak the server key to another vendor's
    default endpoint."""
    for key in (
        "BIODATA_LLM_ENV_FILE",
        "OPENAI_API_KEY",
        "ZAI_API_KEY",
        "ZHIPUAI_API_KEY",
        "ZHIPUAI_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    # Empty-string sentinels keep the machine's local .env (loaded via
    # setdefault / load_dotenv(override=False)) from re-pinning a base_url, so
    # each provider resolves to its own default endpoint.  ``_env_first`` treats
    # "" as unset, so the effective config is "no fixed base_url".
    for key in ("LLM_BASE_URL", "OPENAI_BASE_URL", "ZHIPUAI_BASE_URL", "BIGMODEL_BASE_URL"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("LLM_PROVIDER", "zhipuai")
    monkeypatch.setenv("LLM_API_KEY", "server-secret")
    # Deliberately NO LLM_BASE_URL value and NO provider-specific key.


def test_provider_switch_without_base_url_does_not_leak_generic_server_key(
    monkeypatch: pytest.MonkeyPatch,
):
    """Regression: request switches provider (zhipuai server → openai-compatible)
    with NO base_url and NO request key.  The generic server key must not be
    forwarded to the openai default endpoint (a vendor the server never
    configured)."""
    _server_zhipu_generic_key_config(monkeypatch)
    captured = _capture_diagnose(monkeypatch)
    response = client.post(
        "/api/diagnose",
        json={
            "provider": "openai-compatible",
            "use_llm": True,
            "mock_llm": False,
            # No base_url, no api_key.
        },
    )
    assert response.status_code == 200, response.text
    # The server key is masked, so nothing is sent to the switched-to vendor.
    assert captured[0].api_key is None
    assert captured[0].provider == "openai-compatible"
    assert captured[0].base_url == llm_client.OPENAI_DEFAULT_BASE_URL


def test_provider_switch_masks_generic_server_key_at_builder_level(
    monkeypatch: pytest.MonkeyPatch,
):
    """Builder-level pin of the same contract: provider switch + empty base_url +
    no request key → all server secrets shadowed."""
    _server_zhipu_generic_key_config(monkeypatch)
    overrides = webapp._build_request_overrides(
        provider="openai-compatible",
        use_llm=True,
        mock_llm=False,
        api_key=None,
        base_url="",
        server_provider="zhipuai",
        server_base_url=llm_client.ZHIPU_DEFAULT_BASE_URL,
    )
    assert all(overrides[key] == "" for key in webapp._LLM_SECRET_ENV_KEYS)
    with webapp._temporary_env(overrides):
        config = load_llm_config(project_root=webapp.PROJECT_ROOT)
        assert config.api_key is None


def test_matching_provider_without_base_url_keeps_server_key(
    monkeypatch: pytest.MonkeyPatch,
):
    """Guardrail against over-masking: when the request provider matches the
    server and carries no base_url, the server key still flows to the server's
    own configured endpoint (including a pinned LLM_BASE_URL)."""
    _server_openai_config(monkeypatch)  # openai-compatible + LLM_BASE_URL=trusted.example
    overrides = webapp._build_request_overrides(
        provider="openai-compatible",
        use_llm=True,
        mock_llm=False,
        api_key=None,
        base_url="",
        server_provider="openai-compatible",
        server_base_url="https://trusted.example/v1",
    )
    assert not any(key in overrides for key in webapp._LLM_SECRET_ENV_KEYS)
    with webapp._temporary_env(overrides):
        config = load_llm_config(project_root=webapp.PROJECT_ROOT)
        assert config.api_key == "server-secret"


def test_llm_http_client_installs_no_redirect_handler(monkeypatch: pytest.MonkeyPatch):
    captured = {}
    sentinel = object()

    class FakeOpener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return sentinel

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr(llm_client.urllib.request, "build_opener", fake_build_opener)
    request = llm_client.urllib.request.Request("https://api.example.com/v1")
    result = llm_client._open_request(request, timeout=3.0)

    assert result is sentinel
    assert captured["timeout"] == 3.0
    assert any(isinstance(handler, llm_client._NoRedirectHandler) for handler in captured["handlers"])
    redirect_handler = next(
        handler for handler in captured["handlers"] if isinstance(handler, llm_client._NoRedirectHandler)
    )
    assert redirect_handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1") is None


def test_mock_network_diagnose_is_fully_offline(monkeypatch: pytest.MonkeyPatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("mock diagnose attempted network access")

    monkeypatch.setattr(llm_client.socket, "gethostbyname", forbidden)
    monkeypatch.setattr(llm_client.socket, "create_connection", forbidden)
    monkeypatch.setattr(llm_client, "_open_request", forbidden)
    report = llm_client.diagnose_network(
        llm_client.LLMConfig(
            provider="mock", mock_llm=True, api_key="SERVER_SECRET_MUST_NOT_BE_USED"
        )
    )
    assert report["Provider"] == "mock"
    assert report["DNS"] == report["TCP 443"] == report["HTTPS"] == "skipped"
    assert report["API request"] == "skipped"
    assert "SERVER_SECRET_MUST_NOT_BE_USED" not in str(report)


def test_ci_offline_network_diagnose_is_a_zero_socket_contract(
    monkeypatch: pytest.MonkeyPatch,
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline quality mode attempted network access")

    monkeypatch.setenv("BIODATA_CI_OFFLINE", "1")
    monkeypatch.setattr(llm_client.socket, "gethostbyname", forbidden)
    monkeypatch.setattr(llm_client.socket, "create_connection", forbidden)
    monkeypatch.setattr(llm_client, "_open_request", forbidden)
    report = llm_client.diagnose_network(
        llm_client.LLMConfig(
            provider="zhipuai",
            api_key="SERVER_SECRET_MUST_NOT_BE_USED",
            base_url="https://open.bigmodel.cn/api/paas/v4/",
        )
    )
    assert report["Provider"] == "zhipuai"
    assert report["DNS"] == report["TCP 443"] == report["HTTPS"] == "skipped"
    assert report["API request"] == "skipped"
    assert "SERVER_SECRET_MUST_NOT_BE_USED" not in str(report)


def test_provider_error_redacts_secret_controls_and_excess_text(
    monkeypatch: pytest.MonkeyPatch,
):
    secret = "request-secret-7419"
    body = (
        f'Authorization: Bearer {secret}\r\n{{"api_key":"{secret}"}}\x00' + "x" * 9000
    ).encode("utf-8")

    def raise_http_error(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", hdrs=None, fp=BytesIO(body)
        )

    monkeypatch.setattr(llm_client, "_open_request", raise_http_error)
    result = llm_client.call_openai_compatible(
        "test",
        llm_client.LLMConfig(
            provider="openai-compatible",
            api_key=secret,
            base_url="https://api.example.com/v1",
            model="test-model",
        ),
    )
    assert result.succeeded is False
    assert secret not in (result.error or "")
    assert "Bearer [REDACTED]" in (result.error or "")
    assert "\x00" not in (result.error or "")
    assert "[truncated]" in (result.error or "")
    assert len(result.error or "") < 2200


def test_success_response_is_bounded_and_oversize_body_is_not_retained(
    monkeypatch: pytest.MonkeyPatch,
):
    observed = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size=-1):
            observed["size"] = size
            return b"x" * size

    monkeypatch.setattr(llm_client, "MAX_PROVIDER_RESPONSE_BYTES", 32)
    monkeypatch.setattr(llm_client, "_open_request", lambda *_args, **_kwargs: FakeResponse())
    result = llm_client.call_openai_compatible(
        "test",
        llm_client.LLMConfig(
            provider="openai-compatible",
            api_key="request-secret",
            base_url="https://api.example.com/v1",
            model="test-model",
        ),
    )

    assert observed["size"] == 33
    assert result.succeeded is False
    assert "safety limit" in (result.error or "")
    assert result.raw_response is None


def test_non_json_success_body_is_not_echoed_back(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size=-1):
            return b"provider internal debug page"

    monkeypatch.setattr(llm_client, "_open_request", lambda *_args, **_kwargs: FakeResponse())
    result = llm_client.call_openai_compatible(
        "test",
        llm_client.LLMConfig(
            provider="openai-compatible",
            api_key="request-secret",
            base_url="https://api.example.com/v1",
            model="test-model",
        ),
    )
    assert result.succeeded is False
    assert "non-JSON" in (result.error or "")
    assert result.raw_response is None


def test_trial_ignores_request_base_url(monkeypatch: pytest.MonkeyPatch):
    """2026-08-31 裁决钉：trial 是锁定通道（地址由服务端托管），
    请求级 base_url 必须被丢弃——否则试用密钥可被引向攻击者端点窃取。"""
    _server_openai_config(monkeypatch)
    captured = _capture_diagnose(monkeypatch)
    response = client.post(
        "/api/diagnose",
        json=_diagnose_json(provider="trial", base_url="https://attacker.example/v1"),
    )
    assert response.status_code == 200, response.text
    assert captured, "diagnose 必须真的发起探测"
    assert captured[0].base_url != "https://attacker.example/v1"
