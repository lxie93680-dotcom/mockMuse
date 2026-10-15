"""Pause automation for visible human verification; never solve the challenge."""
from .loop_guard import LoopStopped

class HumanInterventionRequired(LoopStopped):
    pass

def human_reason(snapshot):
    text=str(snapshot).lower()
    if any(p in text for p in ('滑动下方滑块','拖动滑块完成验证','请完成安全验证','verify you are human','确认您是真人')):
        return '网页出现人机验证，请在浏览器画面中手动完成验证。'
    return None
