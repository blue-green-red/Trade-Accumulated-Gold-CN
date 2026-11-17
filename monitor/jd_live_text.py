"""Reject incomplete generated explanations without inventing endings or extra model calls."""
import re


def incomplete_fields(value):
    reassessment=value.get('reassessment') or {};path=value.get('position_path') or {}
    fields=(('价位依据',reassessment.get('price_basis')),('后续计划',path.get('continuation')))
    result=[]
    for name,text in fields:
        if text is None:continue
        text=text.strip()
        if not text or text[-1] not in '。！？.!?；;）)】]”"' or re.search(r'\d\.$',text):result.append(name)
    return result
