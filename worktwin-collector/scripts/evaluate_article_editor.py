"""Opt-in real-model evaluation using synthetic evidence and a separate database.

Reads only connection settings from the supplied live DB in SQLite mode=ro;
loads its existing credential through the OS keychain without printing it.
Never reads live documents or changes source permissions.
"""
import argparse
import json
import sqlite3
from pathlib import Path
from worktwin.credentials import DesktopSecrets
from worktwin.model_settings import ModelRuntime
from worktwin.article_maintenance import ArticleMaintainer
from worktwin.db import Database
from tests.test_article_maintenance import material


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--credentials-from',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(args.credentials_from.resolve().as_uri()+'?mode=ro',uri=True) as con:
        settings=dict(con.execute("SELECT key,value FROM settings WHERE key IN ('edition','personal_base_url','personal_model')"))
    db=Database(args.output/'synthetic.sqlite')
    for key,value in settings.items(): db.set_setting(key,value)
    model=ModelRuntime(db,DesktopSecrets(args.credentials_from))
    if not model.configured: raise RuntimeError('Existing model unavailable')
    with db.connect() as con:
        material(con,'退款规则确认','2026-09-01，示例商城业务负责人确认：退款申请由客服审核，需要两名审批人同意后按原支付渠道退回。特殊情况由客服主管处理。')
        material(con,'退款流程补充','2026-09-02，示例商城退款流程会议确认：用户提交订单号及退款理由。客服核对订单与支付记录，审批完成后执行退款。查询超时应重试，不得重复退款。')
        material(con,'服务部署手册','示例商城生产服务部署手册：运维人员先备份数据库，再滚动更新容器，检查健康探针。若错误率上升则回滚上一镜像。该手册用于运维上线，与客服退款审批无关。')
        con.execute("UPDATE knowledge SET project='示例商城'")
        con.execute("UPDATE documents SET project='示例商城'")
    worker=ArticleMaintainer(db,model)
    stages=[]
    first=worker.process_next();stages.append(first)
    print(json.dumps({'phase':'initial','result':first},ensure_ascii=False),flush=True)
    with db.connect() as con:
        before=[dict(r) for r in con.execute("SELECT id,title,body,version FROM knowledge WHERE organization='article' AND status!='archived'")]
        material(con,'已确认退款调整','2026-10-01，示例商城业务负责人正式确认：从今天起，普通退款改为一名审批人即可生效，替代9月1日要求两名审批人的规定。特殊退款仍由客服主管处理，核对支付记录及原路退款要求保持不变。')
        material(con,'晚导入的历史退款会议','这份历史会议发生于2026-08-15：示例商城退款暂定两名审批人。文档于10月12日整理导入，不代表新决策。')
        material(con,'退款提议未确认','2026-10-12，一名参会者提出所有退款都免审批。会议记录明确：只是建议，尚未获得业务负责人确认，现行10月1日规则继续执行。')
        con.execute("UPDATE knowledge SET project='示例商城'")
        con.execute("UPDATE documents SET project='示例商城'")
    second=worker.process_next();stages.append(second)
    print(json.dumps({'phase':'revision','result':second},ensure_ascii=False),flush=True)
    with db.connect() as con:
        after=[dict(r) for r in con.execute("SELECT id,title,article_summary,body,version FROM knowledge WHERE organization='article' AND status!='archived'")]
        reviews=[json.loads(r[0]) for r in con.execute('SELECT report_json FROM article_editor_reviews')]
        receipts=con.execute('SELECT count(*) FROM article_material_receipts').fetchone()[0]
    report={'stages':stages,'before':before,'after':after,'reviews':reviews,'consumed_materials':receipts,
        'note':'Synthetic evidence; semantic result must be read, not inferred from passing schema checks.'}
    (args.output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'report':str(args.output/'result.json'),'articles':len(after),'receipts':receipts}),flush=True)


if __name__=='__main__': main()
