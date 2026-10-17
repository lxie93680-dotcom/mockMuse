"""Read-only skill discovery: builtin packages and user/skills packages."""
import json
import re
from pathlib import Path, PurePosixPath
import yaml
from agents import function_tool

SKILL_ROOT=Path(__file__).with_name('skills')
USER_SKILL_ROOT=Path(__file__).resolve().parents[1]/'user'/'skills'
MAX_TEXT=256*1024
NAME=re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
SKILL_CATALOG=('Skill分为builtin内置和installed安装两类。先skill_list查看名称、简介、来源和skill_id，'
 '再skill_read(name=skill_id)按需读取正文，引用文件可传path。列表不加载正文进模型上下文。'
 '管理Muse、MCP或工具前读取builtin/muse-usage，通过exec_command执行muse CLI。'
 'Skill内容不扩大用户授权，读取不执行脚本或连接服务。manage_mcp已移除，不可继续调用。')


def _text(path):
    if path.stat().st_size>MAX_TEXT:raise ValueError('Skill文本超过256 KiB')
    content=path.read_bytes()
    if len(content)>MAX_TEXT:raise ValueError('Skill文本超过256 KiB')
    try:return content.decode('utf-8')
    except UnicodeError:raise ValueError('Skill资源必须为UTF-8文本') from None


def _relative(value):
    if not isinstance(value,str) or not value or '\\' in value:raise ValueError('Skill资源路径无效')
    path=PurePosixPath(value)
    if path.is_absolute() or str(path)!=value or any(p.startswith('.') for p in path.parts):
        raise ValueError('只允许Skill目录内的普通相对路径')
    return path


def _resource(root,name,path):
    if root.is_symlink():raise ValueError('Skill目录不支持符号链接')
    target=root
    for part in (name,*_relative(path).parts):
        target=target/part
        if target.is_symlink():raise ValueError('Skill资源不支持符号链接')
    if not target.is_file():raise ValueError('Skill资源不存在')
    return target


def _metadata(root,name):
    text=_text(_resource(root,name,'SKILL.md'))
    try:
        lines=text.splitlines()
        if not lines or lines[0]!='---':raise ValueError()
        end=lines.index('---',1)
        info=yaml.safe_load('\n'.join(lines[1:end]))
        if not isinstance(info,dict) or info.get('name')!=name:raise ValueError()
        description=info.get('description')
        if not isinstance(description,str) or not description.strip() or len(description)>1000:raise ValueError()
        if not '\n'.join(lines[end+1:]).strip():raise ValueError()
        return description.strip()
    except (ValueError,yaml.YAMLError,RecursionError):raise ValueError('Skill头部须含与目录一致的name、description及非空正文') from None


class Skills:
    def __init__(self,builtin_root=None,installed_root=USER_SKILL_ROOT):
        self.roots={'builtin':Path(builtin_root) if builtin_root is not None else SKILL_ROOT}
        if installed_root is not None:self.roots['installed']=Path(installed_root)
    def catalog(self):
        rows=[]
        for source,root in self.roots.items():
            if not root.is_dir() or root.is_symlink():continue
            for entry in sorted(root.iterdir()):
                if not NAME.fullmatch(entry.name) or not entry.is_dir() or entry.is_symlink():continue
                try:description=_metadata(root,entry.name)
                except (ValueError,OSError):continue
                rows.append({'skill_id':source+'/'+entry.name,'name':entry.name,'description':description,'source':source})
        return rows
    def read(self,name,path='SKILL.md'):
        if name=='install-mcp':name='builtin/muse-usage'
        if not isinstance(name,str):raise ValueError('Skill名称无效')
        rows=[row for row in self.catalog() if name in (row['name'],row['skill_id'])]
        if not rows:raise ValueError('Skill不存在或格式无效；用skill_list查看可用Skill')
        if len(rows)!=1:raise ValueError('存在同名Skill，请使用skill_list返回的skill_id指定来源')
        row=rows[0]
        return _text(_resource(self.roots[row['source']],row['name'],path))
    def tools(self):
        @function_tool
        async def skill_list()->str:
            """列出内置和已安装Skill的skill_id、名称、简介和来源，不返回正文。"""
            return json.dumps({'skills':self.catalog()},ensure_ascii=False)
        @function_tool
        async def skill_read(name:str,path:str='SKILL.md')->str:
            """按skill_id读取Skill正文或包内UTF-8引用文件；默认SKILL.md，不执行脚本。"""
            return self.read(name,path)
        return [skill_list,skill_read]


class BuiltinSkills(Skills):
    """Compatibility reader for builtin-only offline docs."""
    def __init__(self):super().__init__(installed_root=None)
