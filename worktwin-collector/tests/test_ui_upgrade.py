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
