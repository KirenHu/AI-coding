"""Opt-in synthetic evaluation: no live documents are read or changed."""
import argparse
import json
import sqlite3
from pathlib import Path
from worktwin.credentials import DesktopSecrets
from worktwin.model_settings import ModelRuntime
from worktwin.article_maintenance import ArticleMaintainer
from worktwin.library_organization import LibraryOrganizer
from worktwin.db import Database
from tests.test_article_maintenance import material, article


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-from',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(args.credentials_from.resolve().as_uri()+'?mode=ro',uri=True) as con:
        settings=dict(con.execute("SELECT key,value FROM settings WHERE key IN ('edition','personal_base_url','personal_model')"))
    class Settings:
        def setting(self,key,default=''):return settings.get(key,default)
    model=ModelRuntime(Settings(),DesktopSecrets(args.credentials_from))
    if not model.configured:raise RuntimeError('Model not configured')
    db=Database(args.output/'synthetic.sqlite')
    sources=[
        ('辰星引擎架构','辰星（Stellar）流程引擎通过 Java 服务执行流程，PostgreSQL 保存流程状态，可作为独立服务部署。它是本次流程平台选型的研究对象。'),
        ('Stellar 对外集成','Stellar 是辰星流程引擎的英文名。其 REST 接口接收 JSON，供业务系统发起流程、查询任务并完成任务，执行状态由引擎统一保存。'),
        ('辰星商业选型','辰星商业评估：供应商报价一年 8 万元，包含技术支持；我们尚未采购。技术实现不是本篇主题，是否采购取决于最终预算。'),
        ('沧海建模能力','沧海建模平台是另一个研究对象，通过可视化建模配置表单和业务规则，与辰星是不同产品。'),
        ('沧海部署条件','沧海平台的应用按租户隔离，支持私有化部署；各应用沿用平台的可视化业务模型。'),
    ]
    with db.connect() as con:
        for name,body in sources:
            _,kid=material(con,name,body);article(con,kid,body)
        con.execute("UPDATE knowledge SET project='流程平台选型'")
        con.execute("UPDATE documents SET project='流程平台选型'")
    worker=LibraryOrganizer(db,model,ArticleMaintainer(db,model));results=[]
    for turn in range(5):
        result=worker.process_next();results.append(result)
        print(json.dumps({'turn':turn,**result},ensure_ascii=False),flush=True)
        if result['state'] in ('idle','error','not_configured'):break
    with db.connect() as con:
        rows=[dict(r) for r in con.execute('SELECT id,title,body,group_name,status,version FROM knowledge ORDER BY id')]
        reviews=[json.loads(r[0]) for r in con.execute('SELECT report_json FROM article_editor_reviews')]
    report={'model':settings['personal_model'],'synthetic':True,'results':results,'articles':rows,'reviews':reviews}
    (args.output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('Report saved:',args.output/'result.json',flush=True)


if __name__=='__main__':main()
