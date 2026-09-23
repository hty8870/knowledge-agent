from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_model_control_is_fully_wired():
    core = (ROOT / "web/static/js/core/core.js").read_text(encoding="utf-8")
    shell = (ROOT / "web/static/js/core/shell.js").read_text(encoding="utf-8")
    boot = (ROOT / "web/static/js/core/boot.js").read_text(encoding="utf-8")
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    for endpoint in ("/api/local-model/status", "/api/local-model/install", "/api/local-model/cancel"):
        assert endpoint in core
    for marker in ("API.localModelStatus", "API.localModelInstall", "API.localModelCancel"):
        assert marker in shell
    assert "export function initLocalModelControl" in shell
    assert "initLocalModelControl();" in boot
    for node in ("modelInstallRow", "modelInstallBtn", "modelCancelBtn", "modelInstallProgress"):
        assert f'id="{node}"' in html
    assert ".local-model-install" in css and "@keyframes localModelProgress" in css
    assert "prefers-reduced-motion: reduce" in css


def test_model_install_copy_is_explicit_optional_and_honest():
    shell = (ROOT / "web/static/js/core/shell.js").read_text(encoding="utf-8")
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    for text in ("约下载 3 GB", "安装后约占 5 GB", "不装也能正常检索"):
        assert text in html or text in shell
    assert "下载失败不影响基础检索" in shell
    assert "window.confirm" in shell
    assert "can_cancel" in shell
