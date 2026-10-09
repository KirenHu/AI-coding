"""Project identity is a verified relationship, never a shared directory name."""
import re

from fastapi.testclient import TestClient

from worktwin.api import create_app
from worktwin.db import Database
from worktwin.reconcile import existing_for_project
from worktwin.scope import document_scope


def token(client):
    return {"X-Worktwin-Token": re.search(
        r'window\.__WORKTWIN_TOKEN__="(.*?)";', client.get("/").text
    ).group(1)}


def source(con, name):
    return con.execute(
        "INSERT INTO sources(name,kind,root,allow_ai) VALUES(?,'folder',?,1)",
        (name, "/tmp/test-project-" + name)
    ).lastrowid


def document(con, source_id, name, *, project_key="", scope="unknown", verified=0):
    return con.execute("""INSERT INTO documents
        (source_id,path,relative_path,title,file_type,project,content,sha256,
         size_bytes,mtime_ns,project_key,scope,project_verified)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source_id, "/tmp/document-" + str(source_id) + "-" + name, name,
         name, ".txt", name, "真实资料中的已确认工作要求", "hash-" + name,
         100, 1, project_key, scope, verified)
    ).lastrowid


def test_unverified_folders_always_have_isolated_scope():
    one = dict(id=100,source_id=1,project="销售平台",file_type=".md",
               content="项目销售平台设计说明")
    two = dict(id=101,source_id=1,project="销售平台",file_type=".md",
               content="项目销售平台其他工作")
    assert document_scope(one) == {"project_key":"session:100","scope":"session","project":"销售平台"}
    assert document_scope(two)["project_key"] == "session:101"
    verified = dict(one, project_verified=1, project_key="manual:explicit",scope="project")
    assert document_scope(verified)["project_key"] == "manual:explicit"


def test_project_names_never_silently_merge_and_owner_can_explicitly_link(tmp_path):
    app = create_app(tmp_path / "db.sqlite", start_worker=False)
    with app.state.db.connect() as con:
        s1 = source(con, "source1")
        s2 = source(con, "source2")
        d1 = document(con, s1, "work-a")
        d2 = document(con, s2, "work-b")
        d3 = document(con, s1, "work-c")
    with TestClient(app) as client:
        headers = token(client)
        def assign(doc_id, body):
            return client.put(f"/api/documents/{doc_id}/scope", headers=headers, json=body)
        a = assign(d1, {"project":"同名业务"})
        b = assign(d2, {"project":"同名业务"})
        assert a.status_code == 200 and b.status_code == 200
        key1, key2 = a.json()["project_key"], b.json()["project_key"]
        assert key1 != key2 and key1.startswith("manual:") and key2.startswith("manual:")
        assert assign(d3, {"project":"同名业务","existing_project_key":key1}).json()["project_key"] == key1
        assert assign(d1, {"project":"同名业务"}).json() == {
            "project":"同名业务","project_key":key1,"queued":False}
        assert assign(d2, {"project":"同名业务","existing_project_key":"manual:unknown"}).status_code == 400
        assert assign(d2, {"project":"不同名称","existing_project_key":key1}).status_code == 400
        groups = client.get("/api/projects/verified", headers=headers).json()
        assert len(groups) == 2
        assert sorted(x["document_count"] for x in groups) == [1,2]


def test_verified_business_projects_may_combine_distinct_sources(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as con:
        s1 = source(con, "folderA")
        s2 = source(con, "folderB")
        d1 = document(con, s1, "first", project_key="manual:verified", scope="project", verified=1)
        d2 = document(con, s2, "second", project_key="manual:verified", scope="project", verified=1)
        d3 = document(con, s2, "third", project_key="manual:separate", scope="project", verified=1)
        kid = con.execute("""INSERT INTO knowledge
            (kind,title,body,status,created_by,source_bound,project,project_key,
             scope,topic,scope_detail,quality)
            VALUES('decision','采购规范','仅在此项目实行采购规范','confirmed',
                   'enterprise_ai',1,'同名业务','manual:verified','project',
                   '采购规范','该业务采购','useful')""").lastrowid
        con.execute("""INSERT INTO knowledge_evidence(knowledge_id,document_id,quote)
            VALUES(?,?,'真实资料中的已确认工作要求')""",(kid,d1))
        same = existing_for_project(con,d2)
        assert [r["id"] for r in same] == [kid]
        assert existing_for_project(con,d3) == []
        con.execute("UPDATE sources SET allow_ai=0 WHERE id=?",(s1,))
        assert existing_for_project(con,d2) == []


def test_legacy_unverified_folder_groups_are_quarantined_on_upgrade(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as con:
        s = source(con, "legacy")
        d = document(con,s,"legacy-note",project_key="source:1:legacy",scope="project",verified=0)
        kid = con.execute("""INSERT INTO knowledge
            (kind,title,body,status,created_by,source_bound,project,project_key,
             scope,topic,scope_detail,quality)
            VALUES('fact','旧归属','旧知识草稿','confirmed','enterprise_ai',1,
                   'legacy','source:1:legacy','project','旧归属','仅来源资料','useful')""").lastrowid
        con.execute("INSERT INTO knowledge_evidence(knowledge_id,document_id,quote) VALUES(?,?,?)",
                    (kid,d,"真实资料中的已确认工作要求"))
        con.execute("DELETE FROM settings WHERE key='project_identity_v2'")
    Database(db.path)
    with db.connect() as con:
        doc = con.execute("SELECT * FROM documents WHERE id=?",(d,)).fetchone()
        know = con.execute("SELECT * FROM knowledge WHERE id=?",(kid,)).fetchone()
        assert doc["scope"] == "session" and doc["project_key"] == f"session:{d}"
        assert know["body"] == "旧知识草稿"
        assert know["review_hold"] == 1 and know["needs_review"] == 1
    # Running the migration again must not silently reactivate frozen notes.
    Database(db.path)
    with db.connect() as con:
        assert con.execute("SELECT review_hold FROM knowledge WHERE id=?",(kid,)).fetchone()[0] == 1
