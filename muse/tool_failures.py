"""Typed failures; never infer recovery from untrusted exception messages."""
import errno
import anyio
import httpx2 as httpx
from mcp.shared.exceptions import MCPError
from mcp.types import REQUEST_TIMEOUT, CONNECTION_CLOSED, METHOD_NOT_FOUND


class ToolBusinessError(RuntimeError):
    """An MCP tool ran and returned isError; this is not a discovery failure."""


class ToolDefinitionError(ValueError):
    pass


class ToolCallFailure(RuntimeError):
    def __init__(self, category, row, stage, discovery):
        self.details={'category':category,'source_id':row['source_id'],'server_id':row.get('server_id'),'tool_id':row['tool_id'],
                      'stage':stage,'rediscovery':discovery,
                      'call_may_have_executed':stage=='execute','business_call_retried':False}
        outcome='调用可能已执行，请先核实结果，禁止盲目重试。' if stage=='execute' else '业务调用尚未发出。'
        super().__init__('外部工具调用失败（'+category+'）；目录重新发现：'+discovery+'。'+outcome)


def causes(error):
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error));yield error
        error=error.__cause__ or (None if error.__suppress_context__ else error.__context__)


def classify(error):
    chain=list(causes(error))
    # Business failures and explicit policy/argument errors never trigger discovery.
    if any(isinstance(e,ToolBusinessError) for e in chain):return 'business'
    if any(isinstance(e,ToolDefinitionError) for e in chain):return 'definition'
    network_errno={errno.ECONNRESET,errno.ECONNREFUSED,errno.EPIPE,errno.ENOTCONN,errno.ETIMEDOUT,errno.ENETUNREACH,errno.EHOSTUNREACH}
    for e in chain:
        if isinstance(e,(TimeoutError,httpx.TimeoutException)):return 'timeout'
        if isinstance(e,(ConnectionError,httpx.TransportError,anyio.EndOfStream,anyio.BrokenResourceError,anyio.ClosedResourceError)):
            return 'connection'
        if isinstance(e,OSError) and e.errno in network_errno:return 'connection'
        if isinstance(e,MCPError):
            if e.error.code==REQUEST_TIMEOUT:return 'timeout'
            if e.error.code==CONNECTION_CLOSED:return 'connection'
            if e.error.code==METHOD_NOT_FOUND:return 'definition'
    if any(isinstance(e,PermissionError) for e in chain):return 'permission'
    if any(isinstance(e,ValueError) for e in chain):return 'arguments'
    return 'execution'
