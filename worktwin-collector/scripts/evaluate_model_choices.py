import json, sqlite3,time
from pathlib import Path
from worktwin.db import Database
from worktwin.credentials import DesktopSecrets
from worktwin.model_settings import ModelRuntime
from worktwin.update_filter import plan
import argparse
ap=argparse.ArgumentParser(description='Opt-in isolated evaluation; no production documents are uploaded.')
ap.add_argument('--credentials-from',type=Path,required=True)
ap.add_argument('--output',type=Path,default=Path('release/model-choices'))
args=ap.parse_args()
live=args.credentials_from.resolve()
out=args.output;out.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(live.as_uri()+'?mode=ro',uri=True) as con:
 settings=dict(con.execute("SELECT key,value FROM settings WHERE key IN ('edition','personal_base_url','personal_model')"))
cases=[('重复','退款需一人审批。','退款需一人审批。','same'),('换行','正文\r\n','正文\n','same'),('同义','审批人为一位主管。','由一名主管审批。','same'),('金额','上限100元。','上限200元。','update'),('日期','10月1日生效。','11月1日生效。','update'),('否定','允许分享。','不允许分享。','update'),('撤回','已决定上线。','负责人撤回上线决定。','update'),('删除','需审批并核对付款。','需核对付款。','update'),('移动','甲负责设计。乙负责开发。','乙负责开发。甲负责设计。','same'),('新增','支持导入。','支持导入和导出。','update'),('未确认','已确认一人审批。','有人建议免审批，未确认。','uncertain'),('旧材料','10月确认一人审批。','8月会议两人审批，11月导入。','same'),('AI声称','尚未验收。','AI说测试已完成，没有证据。','uncertain'),('用户确认','AI建议使用SQLite。','用户明确同意上一条SQLite方案。','update'),('模糊确认','AI给出两个互斥方案。','用户说可以。','uncertain'),('对象一致','Orion技术架构。','Orion商业评估。','same_object'),('对象不同','Orion流程引擎。','博洋流程引擎。','different_object'),('链接','[文档](https://a.test)','[文档](https://b.test)','update'),('状态','任务处理中。','任务已取消。','update'),('单位','限额100元。','限额100美元。','update')]
for label,modelname in [('paid',settings['personal_model']),('free','nvidia/nemotron-3-super-120b-a12b:free')]:
 db=Database(out/(label+'.sqlite'))
 for k,v in settings.items():db.set_setting(k,v)
 db.set_setting('personal_model',modelname)
 model=ModelRuntime(db,DesktopSecrets(live));results=[]
 for name,old,new,expected in cases:
  started=time.monotonic()
  try:
   raw=model.chat([{'role':'system','content':'按 task 指定任务判断。task=object 时只比较研究对象身份，同一产品的不同章节仍为同一对象；只能用same_object或different_object。task=update 时判断是否需要进一步处理知识更新，不判断链接所指网页的内容；只能用same/update/uncertain。金额、日期、否定、删除条款、链接目标变化必须update；建议从未确认变为用户明确确认也属于update；仅换行、同义改写和段落移动为same。比较业务事件时间而非导入时间，较早历史记录不覆盖已确认的新规则。输出JSON {"decision":"same|update|uncertain|same_object|different_object","reason":"简短依据"}。重复和语义等价用same；已确认实质变化用update；未确认建议或缺乏证据用uncertain；旧材料不能覆盖现行规则，用same；只比较对象身份的条目用same_object或different_object。'}, {'role':'user','content':json.dumps({'stage':'benchmark','task':'object' if expected.endswith('_object') else 'update','current':old,'incoming':new},ensure_ascii=False)}],max_tokens=1200)
   parsed=json.loads(raw);result={'name':name,'expected':expected,'actual':parsed,'passed':parsed.get('decision')==expected}
  except Exception as e: result={'name':name,'expected':expected,'error':type(e).__name__,'passed':False}
  result['seconds']=round(time.monotonic()-started,2);results.append(result)
  (out/(label+'.json')).write_text(json.dumps({'model':modelname,'results':results},ensure_ascii=False,indent=2))
  print(label,name,result['passed'],flush=True)
