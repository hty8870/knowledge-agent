from __future__ import annotations

import ipaddress
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "web"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-web.yml"
TAG_RE_LITERAL = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_public_web_compose_is_tls_proxy_only_and_account_guarded():
    text = _text("deploy/web/docker-compose.web.yml")
    assert '"80:8510"' not in text
    assert "127.0.0.1:${BIODATA_PROXY_PORT:-8510}:8510" in text
    assert 'BIODATA_REQUIRE_ACCOUNT: "1"' in text
    assert 'BIODATA_COOKIE_SECURE: "1"' in text
    assert 'FORWARDED_ALLOW_IPS: "*"' in text


def test_deploy_script_validates_tag_and_has_no_nested_sudo_or_sed_interpolation():
    text = _text("deploy/web/deploy.sh")
    assert TAG_RE_LITERAL in text
    assert "EUID" in text and "必须以 root 运行" in text
    assert "sudo docker" not in text
    assert "sudo sed" not in text
    assert "sed -i" not in text
    assert 'mktemp "$BASE/.env.tmp.XXXXXX"' in text
    assert "awk -v tag=\"$tag\"" in text


def test_deploy_workflow_validates_all_shell_inputs_and_pins_health_scheme():
    """部署 workflow 合同：输入先进 env 再校验，健康检查 scheme 跟随 PUBLIC_HEALTH_URL。

    2026-09-09 合同变更（用户拍板，域名 ICP 备案过渡）：
    - PUBLIC_HEALTH_URL 接受 http/https 两种 scheme；https 分支仍钉死 TLS1.2+，
      http 分支仅过渡期使用，备案完成后只改 environment 变量即切回；
    - 部署不再经 root wrapper（deploy-release.sh 已退役删除），workflow 直接对齐
      服务器 sudoers 现实白名单：docker pull / docker tag / deploy.sh 三条。
    """
    if not WORKFLOW.exists():
        return  # public mirror intentionally has no production deployment workflow
    text = WORKFLOW.read_text(encoding="utf-8")
    validate_at = text.index("- name: Validate deployment inputs")
    ssh_at = text.index("- name: Deploy on production host via SSH")
    assert validate_at < ssh_at
    assert TAG_RE_LITERAL in text
    assert "PUBLIC_HEALTH_URL must be http(s)" in text
    assert "--proto '=https' --tlsv1.2" in text
    assert "--proto '=http'" in text
    assert "sudo -n /usr/bin/docker pull" in text
    assert "sudo -n /usr/bin/docker tag" in text
    assert "sudo -n /opt/biodata-web/deploy.sh '$TAG'" in text
    assert "deploy-release.sh" not in text
    # Expressions go into env first; untrusted values are not pasted into run scripts.
    assert 'printf \'%s\\n\' "$DEPLOY_HOST_KEY"' in text
    assert '"$DEPLOY_USER@$DEPLOY_HOST"' in text


def test_deploy_docs_and_workflow_contain_no_live_global_ip_literals():
    paths = [ROOT / "SECURITY.md", *DEPLOY.glob("*")]
    if WORKFLOW.exists():
        paths.append(WORKFLOW)
    ip_re = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
    violations: list[str] = []
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in {".md", ".yml", ".yaml", ".sh", ".conf", ".example"}:
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for raw in ip_re.findall(line):
                try:
                    ip = ipaddress.ip_address(raw)
                except ValueError:
                    continue
                documentation = any(
                    ip in ipaddress.ip_network(block)
                    for block in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
                )
                if ip.is_global and not documentation:
                    violations.append(f"{path.relative_to(ROOT).as_posix()}:{line_no}")
    assert violations == []
