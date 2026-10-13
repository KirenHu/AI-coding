import json,sqlite3
from pathlib import Path
from worktwin.db import Database
from worktwin.model_settings import ModelRuntime
from worktwin.credentials import DesktopSecrets
from worktwin.inference import extract_knowledge
from worktwin.update_filter import plan
import argparse
ap=argparse.ArgumentParser(description='Opt-in isolated evaluation; no production documents are uploaded.')
ap.add_argument('--credentials-from',type=Path,required=True)
ap.add_argument('--output',type=Path,default=Path('release/document-delta'))
args=ap.parse_args()
live=args.credentials_from.resolve()
root=args.output;root.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(live.as_uri()+'?mode=ro',uri=True) as con:settings=dict(con.execute("SELECT key,value FROM settings WHERE key IN ('edition','personal_base_url','personal_model')"))
old='# WorkTwin 项目要求\n维护用户可阅读的知识文章。\n\n'
old+='## 知识整理\n'+('采集时记录有价值的信息及其原文上下文；周期性维护完整事项文章，不把每个信息点作为独立文章。\n'*20)
old+='## 交互要求\n'+('一级菜单为知识库、数字分身、知识来源。知识详情以标题和正文为主要内容，历史通过日志入口查看。\n'*20)
old+='## 当前约束\n效果优先，不以减少 Token 为首要目标。\n'
new=old+'\n## 浏览器白名单\n允许用户手动添加网站。用户开启采集后持续记录该网站，直到用户主动关闭。流程平台也可以开启任务采集，但任务结束不得关闭用户手动开启的采集。\n'
selection=plan(old,new)
results={}
for mode,text in [('full',new),('incremental',selection['content'])]:
 db=Database(root/(mode+'.sqlite'))
 for k,v in settings.items():db.set_setting(k,v)
 model=ModelRuntime(db,DesktopSecrets(live))
 items=extract_knowledge(text,transcript=False,client=model)
 with db.connect() as c:usage=[dict(r) for r in c.execute('SELECT stage,count(*) calls,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(cost_usd) cost_usd FROM model_call_metrics GROUP BY stage')]
 results[mode]={'items':items,'usage':usage,'chars':len(text)}
 (root/'result.json').write_text(json.dumps({'selection':selection['reason'],'results':results},ensure_ascii=False,indent=2))
 print(mode,len(items),usage,flush=True)
