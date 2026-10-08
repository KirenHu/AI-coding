from worktwin.rowboat_markdown import extract_field,extract_list,extract_title


def test_rowboat_markdown_format_and_empty_field_boundaries():
    body='# 审批节点\n\n**Aliases:** [[流程审批|审批流]], 业务审批\n**Email:**\n**Role:** 产品经理\n'
    assert extract_title(body)=='审批节点'
    assert extract_field(body,'Aliases')=='审批流'
    assert extract_list('**Keywords:** 工作流, 审批, 路由\n','Keywords')==['工作流','审批','路由']
    assert extract_field(body,'Email') is None
    assert extract_field(body,'Role')=='产品经理'
