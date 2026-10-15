"""Execution phase telemetry, separate from overall task status."""
class Phases:
    VALID = {'preparing_context', 'thinking', 'replying', 'executing_tool', 'checking_result'}

    def __init__(self, notify=None):
        self.notify = notify or (lambda *args: None)
        self.active_tools = {}
        self.phase = None

    def set(self, phase, detail=''):
        if phase not in self.VALID:
            raise ValueError('Invalid execution phase')
        # Parallel tools keep execution visible until all have returned.
        if self.active_tools:
            phase = 'executing_tool'
            detail = '、'.join(self.active_tools.values())
        self.phase = phase
        self.notify(phase, detail, list(self.active_tools.values()))

    def tool_started(self, call_id, name):
        self.active_tools[call_id] = name
        self.set('executing_tool', name)

    def tool_finished(self, call_id):
        self.active_tools.pop(call_id, None)
        self.set('checking_result', '检查工具返回、错误与重复调用')
