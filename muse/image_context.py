"""Bound tool-image history without altering archived originals or user uploads."""
import copy
import json

IMAGE_TYPES={'input_image','image_url','image'}


def project_tool_images(items, max_images=2):
    projected=copy.deepcopy(items)
    last_user=max((i for i,item in enumerate(projected) if item.get('role')=='user'),default=-1)
    outputs=[]
    for i,item in enumerate(projected):
        if item.get('type')!='function_call_output':continue
        output=item.get('output')
        if isinstance(output,str):
            try:output=json.loads(output)
            except (ValueError,TypeError):continue
        if isinstance(output,list):outputs.append((i,item,output))
    # Only the newest image-bearing tool return in this user turn remains visual.
    latest=next((i for i,item,parts in reversed(outputs) if i>last_user and any(isinstance(p,dict) and p.get('type') in IMAGE_TYPES for p in parts)),None)
    kept=0
    for i,item,parts in reversed(outputs):
        replacement=[]
        for part in parts:
            if isinstance(part,dict) and part.get('type') in IMAGE_TYPES:
                if i==latest and kept<max_images:
                    replacement.append(part);kept+=1
                else:
                    replacement.append({'type':'input_text','text':f"[历史工具图片已从模型上下文移除；原图保留在工具原文记录。tool_call_id={item.get('call_id','未知')}。此引用不描述图片内容，也不能用于定位当前页面。]"})
            else:replacement.append(part)
        item['output']=replacement
    return projected
