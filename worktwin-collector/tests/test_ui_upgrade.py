"""An installed upgrade must not reuse the previous dashboard resources."""
import re
import shutil

from fastapi.testclient import TestClient

from worktwin import api


def test_upgrade_changes_asset_urls_and_forbids_stale_cache(tmp_path, monkeypatch):
    static = tmp_path / 'static'
    shutil.copytree(api.STATIC, static)
    monkeypatch.setattr(api, 'STATIC', static)
    # An old process only substitutes the session token into the new template.
    # Its HTML must still bypass unversioned scripts already cached in-browser.
    legacy_server_html = (static / 'index.html').read_text().replace('__LOCAL_TOKEN_VALUE__', 'old-session')
    legacy_urls = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', legacy_server_html)
    assert all('?v=' in url for url in legacy_urls)

    def resources(client):
        response = client.get('/')
        assert response.headers['cache-control'] == 'no-store'
        return re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)

    with TestClient(api.create_app(tmp_path / 'old.sqlite', start_worker=False)) as old:
        old_urls = resources(old)
        assert len(old_urls) == 2
        for url in old_urls:
            resource = old.get(url)
            assert resource.status_code == 200
            assert resource.headers['cache-control'] == 'no-store'
        # Even a previously stored unversioned URL must request revalidation.
        assert old.get('/assets/app.js').headers['cache-control'] == 'no-store'

    script = static / 'app.js'
    script.write_text(script.read_text() + '\n// Upgraded application code\n')
    with TestClient(api.create_app(tmp_path / 'new.sqlite', start_worker=False)) as new:
        new_urls = resources(new)
        assert set(old_urls).isdisjoint(new_urls)
        for url in new_urls:
            assert new.get(url).status_code == 200


def test_runtime_dashboard_and_package_versions_are_identical():
    import tomllib
    from pathlib import Path
    from worktwin import __version__

    root=Path(__file__).resolve().parents[1]
    package=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))
    html=(root/'worktwin/static/index.html').read_text(encoding='utf-8')
    assert __version__==package['project']['version']
    assert f'window.__WORKTWIN_VERSION__="{__version__}"' in html
    assert f'/assets/app.js?v={__version__}' in html
    assert f'/assets/styles.css?v={__version__}' in html


def test_stale_editor_cannot_overwrite_newer_knowledge(tmp_path):
    with TestClient(api.create_app(tmp_path/'conflict.sqlite',start_worker=False)) as client:
        token=re.search(r'window.__WORKTWIN_TOKEN__="(.*?)";',client.get('/').text).group(1)
        headers={'X-Worktwin-Token':token}
        body={'title':'操作说明','body':'第一版正文','kind':'process','status':'confirmed'}
        created=client.post('/api/knowledge',json=body,headers=headers)
        assert created.status_code==200,created.text
        kid=created.json()['id']
        version=client.get(f'/api/knowledge-item/{kid}',headers=headers).json()['version']
        newer=dict(body,body='后台已更新正文',expected_version=version)
        assert client.put(f'/api/knowledge/{kid}',json=newer,headers=headers).status_code==200
        stale=client.put(f'/api/knowledge/{kid}',json=dict(body,expected_version=version),headers=headers)
        assert stale.status_code==409
        assert client.get(f'/api/knowledge-item/{kid}',headers=headers).json()['body']=='后台已更新正文'
        assert len(client.get(f'/api/knowledge/{kid}/history',headers=headers).json())==1
