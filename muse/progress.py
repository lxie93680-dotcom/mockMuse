"""Independent progress for delegated work and context compaction."""
import asyncio
import time
import uuid

notify=lambda event:None

class Activity:
    def __init__(self,title,purpose,parent_task_id=None):
        self.event={'task_id':'task_'+uuid.uuid4().hex,'parent_task_id':parent_task_id,
            'purpose':purpose,'title':title[:160],'status':'running','phase':'preparing_context',
            'detail':'准备上下文','active_tools':[],'started_at':time.time(),'ended_at':None}
        self.emit()
    def emit(self):
        try:notify(dict(self.event,active_tools=list(self.event['active_tools'])))
        except Exception:
            # UI telemetry must not turn a successful child into a tool error.
            import logging
            logging.getLogger(__name__).debug('Progress notification failed',exc_info=True)
    def phase(self,phase,detail='',active_tools=None):
        if self.event['status']!='running':return
        self.event.update(phase=phase,detail=detail,active_tools=active_tools or []);self.emit()
    def finish(self,error=None):
        if self.event['status']!='running':return
        status='completed' if error is None else ('interrupted' if isinstance(error,(asyncio.CancelledError,TimeoutError)) else 'error')
        self.event.update(status=status,phase=None,active_tools=[],ended_at=time.time(),detail='' if error is None else type(error).__name__);self.emit()
