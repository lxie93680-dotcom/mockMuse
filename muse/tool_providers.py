"""Definition discovery adapters. Discovery never executes a tool or installs code."""
import asyncio
import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_DEFINITION_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ToolDefinition:
    key: str
    name: str
    description: str
    parameters: dict


class ToolProvider(Protocol):
    source_id: str
    source_kind: str

    async def discover_tools(self) -> list[ToolDefinition]: ...


def mcp_source_id(server):
    known = {'Filesystem':'mcp:filesystem', 'Muse Browser':'mcp:muse-browser'}
    if server.name in known:
        return known[server.name]
    label = re.sub(r'[^A-Za-z0-9._-]+', '-', server.name).strip('-')[:80] or 'server'
    digest = hashlib.sha256(server.name.encode()).hexdigest()[:12]
    return f'mcp:{label}:{digest}'


class MCPProvider:
    source_kind = 'mcp'

    def __init__(self, server, *, server_id=None):
        self.server = server
        self.server_id = server_id
        self.source_id = mcp_source_id(server)

    def bind_access(self, checker):
        original=getattr(self.server,'call_tool',None)
        if original is None:return lambda:None
        async def checked_call(tool_name, arguments, meta=None):
            checker(tool_name)
            result=await original(tool_name,arguments,meta)
            if getattr(result,'is_error',getattr(result,'isError',False)):
                from .tool_failures import ToolBusinessError
                raise ToolBusinessError('MCP工具返回业务错误')
            return result
        self.server.call_tool=checked_call
        def unsubscribe():
            if self.server.call_tool is checked_call:self.server.call_tool=original
        return unsubscribe

    def invalidate(self):
        invalidate=getattr(self.server,'invalidate_tools_cache',None)
        if invalidate:invalidate()

    def subscribe(self, callback):
        subscribe=getattr(self.server,'add_catalog_listener',None)
        return subscribe(callback) if subscribe else lambda:None

    async def discover_tools(self):
        tools = await self.server.list_tools()
        result = []
        for tool in tools:
            schema = getattr(tool, 'input_schema', None)
            if schema is None:
                schema = tool.inputSchema
            result.append(ToolDefinition(tool.name, tool.name, tool.description or '', schema))
        return result


def manifest_definitions(document):
    if not isinstance(document, dict) or not isinstance(document.get('tools'), list):
        raise ValueError('Definition document must contain a tools array')
    tools = []
    for item in document['tools']:
        if not isinstance(item, dict):
            raise ValueError('Each tool definition must be an object')
        name = item.get('name')
        parameters = item.get('inputSchema', item.get('parameters'))
        tools.append(ToolDefinition(item.get('key', name), name, item.get('description', ''), parameters))
    return tools


class ManifestProvider:
    source_kind = 'plugin'

    def __init__(self, source_id, path):
        self.source_id = source_id
        self.path = Path(path)
        self.revision = None

    async def discover_tools(self):
        def read():
            with self.path.open('rb') as stream:
                data = stream.read(MAX_DEFINITION_BYTES + 1)
            if len(data) > MAX_DEFINITION_BYTES:
                raise ValueError('Tool manifest exceeds 2 MiB')
            tools=manifest_definitions(json.loads(data))
            self.revision=hashlib.sha256(data).hexdigest()
            return tools
        return await asyncio.to_thread(read)

    async def watch_revision(self):
        def read_revision():
            try:
                with self.path.open('rb') as stream:
                    data=stream.read(MAX_DEFINITION_BYTES+1)
                return hashlib.sha256(data).hexdigest()
            except OSError as error:
                return type(error).__name__
        return await asyncio.to_thread(read_revision)


def openapi_definitions(document):
    """OpenAPI 3 JSON definitions: local refs, parameters, JSON request bodies.

    This maps metadata only, not HTTP execution. Unsupported references/media
    fail the whole source instead of registering an incomplete parameter schema.
    """
    if not isinstance(document, dict) or not str(document.get('openapi', '')).startswith('3.'):
        raise ValueError('Expected an OpenAPI 3 document')
    paths = document.get('paths')
    if not isinstance(paths, dict):
        raise ValueError('OpenAPI paths must be an object')

    def resolve(value, chain=(), depth=0):
        if depth > 40:
            raise ValueError('OpenAPI reference depth exceeded')
        if isinstance(value, list):
            return [resolve(v, chain, depth+1) for v in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            ref = value['$ref']
            if not isinstance(ref, str) or not ref.startswith('#/') or ref in chain:
                raise ValueError('Only nonrecursive local OpenAPI refs are supported')
            target = document
            for segment in ref[2:].split('/'):
                target = target[segment.replace('~1','/').replace('~0','~')]
            if not isinstance(target, dict):
                raise ValueError('OpenAPI ref must resolve to an object')
            resolved = resolve(target, (*chain,ref), depth+1)
            siblings = resolve({k:v for k,v in value.items() if k!='$ref'}, chain, depth+1)
            return {**resolved, **siblings}
        return {k:resolve(v,chain,depth+1) for k,v in value.items()}

    tools = []
    for path, raw_path_item in paths.items():
        if not isinstance(path, str) or not path.startswith('/'):
            raise ValueError('Invalid OpenAPI path')
        path_item = raw_path_item
        if isinstance(path_item,dict) and '$ref' in path_item:
            # Path-item refs are uncommon; resolve them without walking response
            # schemas for ordinary operations (responses are not tool inputs).
            path_item = resolve(path_item)
        if not isinstance(path_item, dict):
            raise ValueError('Invalid OpenAPI path item')
        for method in ('get','put','post','delete','options','head','patch','trace'):
            if method not in path_item:
                continue
            operation = path_item[method]
            if not isinstance(operation, dict):
                raise ValueError('Invalid OpenAPI operation')
            key = method.upper() + ':' + path
            name = operation.get('operationId') or re.sub(r'[^A-Za-z0-9_]', '_', method+'_'+path)
            parameters = {'type':'object','properties':{},'additionalProperties':False}
            required = []
            combined = {}
            for parameter in path_item.get('parameters', []) + operation.get('parameters', []):
                parameter = resolve(parameter)
                if not isinstance(parameter, dict):
                    raise ValueError('Invalid OpenAPI parameter')
                combined[(parameter['in'],parameter['name'])] = parameter
            for (location, parameter_name), parameter in combined.items():
                group = {'path':'path','query':'query','header':'headers','cookie':'cookies'}.get(location)
                if group is None or not isinstance(parameter.get('schema'), dict):
                    raise ValueError('Only schema-based OpenAPI parameters are supported')
                container = parameters['properties'].setdefault(group, {'type':'object','properties':{},'additionalProperties':False})
                schema = copy.deepcopy(parameter['schema'])
                if parameter.get('description'):
                    schema.setdefault('description',parameter['description'])
                container['properties'][parameter_name] = schema
                if parameter.get('required') or location=='path':
                    container.setdefault('required',[]).append(parameter_name)
                    if group not in required:
                        required.append(group)
            if 'requestBody' in operation:
                body = resolve(operation['requestBody'])
                media = body.get('content', {}).get('application/json')
                if not isinstance(media, dict) or not isinstance(media.get('schema'), dict):
                    raise ValueError('Only application/json request bodies are supported')
                parameters['properties']['body'] = media['schema']
                if body.get('required'):
                    required.append('body')
            if required:
                parameters['required'] = required
            description = operation.get('description') or operation.get('summary') or ''
            tools.append(ToolDefinition(key,name,description,parameters))
    return tools


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Schema URL redirects are not followed')


class HTTPToolsProvider:
    source_kind = 'http'

    def __init__(self, source_id, url, headers=None, timeout_seconds=10):
        parsed = urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Expected an HTTP(S) URL without embedded credentials')
        if not 0 < timeout_seconds <= 30:
            raise ValueError('Schema timeout must be 0–30 seconds')
        self.source_id = source_id
        self.url = url
        self.headers = dict(headers or {})
        self.timeout_seconds = timeout_seconds

    async def discover_tools(self):
        def fetch():
            request = Request(self.url, headers=self.headers)
            with build_opener(_NoRedirects()).open(request, timeout=self.timeout_seconds) as response:
                data = response.read(MAX_DEFINITION_BYTES + 1)
            if len(data) > MAX_DEFINITION_BYTES:
                raise ValueError('HTTP tool definition exceeds 2 MiB')
            document = json.loads(data)
            if isinstance(document, dict) and 'openapi' in document:
                return openapi_definitions(document)
            return manifest_definitions(document)
        return await asyncio.to_thread(fetch)


def load_configured_providers(config_path):
    """Load restart-persistent plugin/HTTP source configuration; credentials stay in env."""
    import os
    path=Path(config_path)
    with path.open('rb') as stream:
        data=stream.read(MAX_DEFINITION_BYTES+1)
    if len(data)>MAX_DEFINITION_BYTES:
        raise ValueError('Tool source config exceeds 2 MiB')
    document=json.loads(data)
    if not isinstance(document,dict) or not isinstance(document.get('sources'),list):
        raise ValueError('Expected a sources array')
    providers=[]
    for source in document['sources']:
        if source['kind']=='plugin':
            manifest=Path(source['path']).expanduser()
            if not manifest.is_absolute():manifest=path.parent/manifest
            providers.append(ManifestProvider(source['id'],manifest))
        elif source['kind']=='http':
            headers={name:os.environ[variable] for name,variable in source.get('headers_env',{}).items()}
            providers.append(HTTPToolsProvider(source['id'],source['url'],headers,source.get('timeout_seconds',10)))
        else:
            raise ValueError('Only plugin and HTTP config sources are supported')
    return providers
