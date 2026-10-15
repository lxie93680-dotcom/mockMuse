"""Durable run checkpoints and safe continuation after interrupted tools."""
import copy
import json


def close_pending_tools(items):
    items = copy.deepcopy(items)
    completed = {item.get('call_id') for item in items if item.get('type') == 'function_call_output'}
    result = []
    for item in items:
        result.append(item)
        if item.get('type') == 'function_call' and item.get('call_id') not in completed:
            result.append({'type': 'function_call_output', 'call_id': item['call_id'],
                           'output': '执行已中断，未记录到工具返回。操作是否生效未知；继续前先检查实际状态，避免重复操作。'})
    return result


class RunCheckpoint:
    def __init__(self, items, save=None):
        self.items = copy.deepcopy(items)
        self.save = save or (lambda items: None)

    def flush(self):
        self.save(copy.deepcopy(self.items))

    def model_input(self, items):
        self.items = copy.deepcopy(items)
        self.flush()

    def model_output(self, response):
        self.items.extend(response.to_input_items())
        self.flush()

    def tool_output(self, call_id, output):
        if not any(item.get('type') == 'function_call' and item.get('call_id') == call_id for item in self.items):
            return
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False, default=lambda value: value.model_dump(mode='json') if hasattr(value, 'model_dump') else str(value))
        self.items.append({'type': 'function_call_output', 'call_id': call_id, 'output': output})
        self.flush()

    def close(self):
        self.items = close_pending_tools(self.items)
        self.flush()
