"""部署注入页脚（备案合规）契约：

- env 未配置 → 占位符清空、页脚留空（CSS :empty 隐藏），页面无模板痕迹；
- env 配置 → 管理侧 HTML 原样注入（保留 <a>，不转义）；
- 运行时注入不改写源文件——源 index.html 永远只含占位符，真实备案号不进 git。
"""

from __future__ import annotations


def test_deploy_footer_absent_when_env_unset(monkeypatch) -> None:
    from dataset_recommender.app import webapp

    monkeypatch.delenv("BIODATA_SITE_FOOTER_HTML", raising=False)
    html = webapp._index_html()
    assert "<!--deploy-footer-->" not in html, "未配置时占位符必须清空，不留模板痕迹"
    assert '<footer class="deploy-footer"></footer>' in html


def test_deploy_footer_injected_raw_when_env_set(monkeypatch) -> None:
    from dataset_recommender.app import webapp

    snippet = ('<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">'
               '某ICP备00000000号-1</a>')
    monkeypatch.setenv("BIODATA_SITE_FOOTER_HTML", snippet)
    html = webapp._index_html()
    assert snippet in html, "管理侧 HTML 必须原样注入（不转义，保留 <a> 标记）"
    assert "<!--deploy-footer-->" not in html


def test_deploy_footer_placeholder_stays_in_source(monkeypatch) -> None:
    from dataset_recommender.app import webapp

    monkeypatch.setenv("BIODATA_SITE_FOOTER_HTML", "<a>x</a>")
    webapp._index_html()
    source = (webapp.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "<!--deploy-footer-->" in source, "运行时注入不得改写源 HTML"
