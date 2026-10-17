"""Muse command line: help locally, manage the running service through its API."""
import argparse
import http.cookiejar
import ipaddress
import json
import os
import sys
from urllib.error import HTTPError,URLError
from urllib.parse import quote,urlsplit
from urllib.request import Request,build_opener,HTTPCookieProcessor,HTTPRedirectHandler,ProxyHandler
from .skills import BuiltinSkills, Skills
from .tool_diagnostics import safe_text

class CLIError(Exception):pass
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

class Client:
    def __init__(self,server,timeout=45):
        p=urlsplit(server)
        try:local=p.hostname=='localhost' or ipaddress.ip_address(p.hostname).is_loopback
        except (ValueError,TypeError):local=False
        if (p.scheme not in ('http','https') or not p.hostname or p.username or p.password
                or p.query or p.fragment or p.path not in ('','/') or (p.scheme=='http' and not local)):
            raise CLIError('服务地址须为本机HTTP或HTTPS地址，不得包含凭据、路径或查询参数')
        self.base=server.rstrip('/');self.timeout=timeout
        handlers=[NoRedirect(),HTTPCookieProcessor(http.cookiejar.CookieJar())]
        if local:handlers.append(ProxyHandler({}))
        self.opener=build_opener(*handlers)
    def request(self,path,body=None,retry_auth=True,*,method=None):
        data=json.dumps(body).encode() if body is not None else None
        req=Request(self.base+'/api/'+path,data=data,headers={'Accept':'application/json',**({'Content-Type':'application/json','Origin':self.base} if data is not None or method=='DELETE' else {})},method=method or ('POST' if data is not None else 'GET'))
        try:
            with self.opener.open(req,timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code==401 and retry_auth and os.environ.get('MUSE_ACCESS_TOKEN'):
                self.request('login',{'token':os.environ['MUSE_ACCESS_TOKEN']},False)
                return self.request(path,body,False,method=method)
            try:detail=json.loads(error.read(65536)).get('detail')
            except (ValueError,UnicodeError):detail=None
            if error.code==401:raise CLIError('Muse要求认证；在本地使用，或通过环境变量MUSE_ACCESS_TOKEN配置访问口令，不要把口令写入命令') from None
            raise CLIError('Muse HTTP '+str(error.code)+'：'+safe_text(detail or '请求失败')) from None
        except (URLError,TimeoutError,OSError):raise CLIError('无法连接Muse或请求超时；请确认服务已启动、地址正确。CLI未直接修改配置文件') from None
        except (ValueError,UnicodeError):raise CLIError('Muse返回了无法解析的响应；请检查服务地址及版本') from None

def parser():
    p=argparse.ArgumentParser(prog='muse',description='Muse CLI：管理运行中的Muse；操作说明见 muse docs')
    p.add_argument('--server',default=os.getenv('MUSE_URL') or 'http://127.0.0.1:'+os.getenv('MUSE_PORT','8000'),help='Muse地址，默认本机8000；也可设置MUSE_URL')
    p.add_argument('--timeout',type=float,default=45,help='请求超时秒数，默认45')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('docs',help='读取内置muse-usage Skill，无需服务在线')
    skills=sub.add_parser('skills',help='查看内置和安装Skill');ss=skills.add_subparsers(dest='action',required=True)
    ss.add_parser('list');sr=ss.add_parser('read');sr.add_argument('name');sr.add_argument('--path',default='SKILL.md')
    sub.add_parser('status',help='检查服务、浏览器和MCP状态，不输出会话内容')
    mcp=sub.add_parser('mcp',help='远程MCP配置、连接与注册');ms=mcp.add_subparsers(dest='action',required=True)
    ms.add_parser('list',help='列出服务及连接状态')
    add=ms.add_parser('add',help='保存配置并连接，工具目录实时更新');add.add_argument('name');add.add_argument('--url',required=True);add.add_argument('--token-env',default='',help='服务端环境变量名，不能传密钥值')
    remove=ms.add_parser('remove',aliases=['uninstall'],help='卸载远程MCP，停止重连并移除可调用工具');remove.add_argument('name')
    connect=ms.add_parser('connect',help='重建已有服务连接，不重放业务调用');connect.add_argument('name')
    tools=sub.add_parser('tools',help='工具搜索、详情、管理');ts=tools.add_subparsers(dest='action',required=True)
    ts.add_parser('list',help='管理列表，包含禁用/缺失工具；Agent发现应使用search').add_argument('--server-id')
    search=ts.add_parser('search',help='按启停缓存过滤后搜索可用候选');search.add_argument('query');search.add_argument('--limit',type=int,default=10);search.add_argument('--server-id')
    for name in ('describe','enable','disable'):
        cmd=ts.add_parser(name);cmd.add_argument('tool_id')
    refresh=ts.add_parser('refresh',help='重新发现工具，更新指纹和索引');refresh.add_argument('--source')
    return p

def execute(args,client):
    if args.command=='status':
        state=client.request('state');remote=client.request('mcp')
        return {'reachable':True,'task_status':state.get('status'),'browser_connected':state.get('browser_connected'),
                'environment':state.get('environment'),
                'workspace_recovery':state.get('workspace_recovery'),
                'local_mcp':{name:{key:value.get(key) for key in ('enabled','ready','error')} for name,value in state.get('mcp',{}).items()},'remote_mcp':remote['servers']},0
    if args.command=='mcp':
        if args.action=='list':return client.request('mcp'),0
        if args.action in ('remove','uninstall'):
            return client.request('mcp/'+quote(args.name,safe=''),method='DELETE'),0
        if args.action=='add':
            result=client.request('mcp',{'id':args.name,'url':args.url,'token_env':args.token_env})
        else:result=client.request('mcp/'+quote(args.name,safe='')+'/connect',{})
        return {**result,'saved':True},0 if result.get('ready') else 2
    if args.action=='list':return client.request('tools'+('?server_id='+quote(args.server_id,safe='') if args.server_id else '')),0
    if args.action=='search':return client.request('tools/search?query='+quote(args.query,safe='')+'&limit='+str(args.limit)+('&server_id='+quote(args.server_id,safe='') if args.server_id else '')),0
    if args.action=='describe':return client.request('tools/'+quote(args.tool_id,safe='')+'/detail'),0
    if args.action in ('enable','disable'):return client.request('tools/'+quote(args.tool_id,safe='')+'/enabled',{'enabled':args.action=='enable'}),0
    result=client.request('tools/refresh',{'source_id':args.source})
    return result,2 if result.get('errors') else 0

def main(argv=None):
    p=parser();args=p.parse_args(argv)
    try:
        if args.command=='docs':print(BuiltinSkills().read('muse-usage'));return 0
        if args.command=='skills':
            reader=Skills()
            if args.action=='read':print(reader.read(args.name,args.path))
            else:print(json.dumps({'skills':reader.catalog()},ensure_ascii=False,indent=2))
            return 0
        if not 0<args.timeout<=120:raise CLIError('timeout须为大于0且不超过120的秒数')
        if args.command=='tools' and args.action=='search' and not 1<=args.limit<=50:raise CLIError('limit须为1至50')
        result,code=execute(args,Client(args.server,args.timeout))
        print(json.dumps(result,ensure_ascii=False,indent=2));return code
    except (CLIError,ValueError,OSError) as error:
        print(json.dumps({'error':safe_text(error)},ensure_ascii=False),file=sys.stderr);return 1
    except KeyboardInterrupt:return 130

if __name__=='__main__':raise SystemExit(main())
