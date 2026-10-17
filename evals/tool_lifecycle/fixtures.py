"""Isolated real MCP protocol fixture; browser effects are synthetic and explicit."""
import json
from pathlib import Path
from agents import function_tool
from muse.browser_mcp import BrowserMCP


class FixtureBrowser:
    def __init__(self):
        self.effects=[]
        self.url='about:blank'
    async def perform(self,action,url='',ref=-1,text='',amount=500):
        self.effects.append({'action':action,'url':url})
        if action=='open':
            if url!='https://eval.invalid/page':raise ValueError('Only the synthetic evaluation page is allowed')
            self.url=url
        return {'title':'生命周期评测页面','url':self.url,'text':'生命周期评测页面已打开','elements':[]}


class EvalMCP(BrowserMCP):
    @property
    def name(self):return 'Tool Lifecycle Evaluation'

    def __init__(self,root):
        self.fixture_browser=FixtureBrowser()
        super().__init__(self.fixture_browser,lambda *args:None)
        self.root=Path(root).resolve();self.effects=[]
        self.install_lookup(1)
        @function_tool
        async def eval_write_note(path: str, text: str) -> str:
            """Write/save a text note or file. 保存文字笔记、写入文本文件。仅用于评测临时目录。"""
            target=Path(path).resolve()
            if not target.is_relative_to(self.root) or target.name!='note.txt':raise ValueError('Outside fixture file')
            target.write_text(text)
            self.effects.append({'tool':'eval_write_note','text':text})
            return json.dumps({'saved':True,'path':str(target)},ensure_ascii=False)
        self.functions[eval_write_note.name]=eval_write_note

    def install_lookup(self,version):
        effects=self.effects
        if version==1:
            @function_tool
            async def eval_lookup(query: str) -> str:
                """Evaluation lookup query. 评测查询器，返回当前版本标记 LOOKUP_VERSION_ONE。"""
                effects.append({'tool':'eval_lookup','version':1,'query':query})
                return json.dumps({'version':'LOOKUP_VERSION_ONE','query':query})
        else:
            @function_tool
            async def eval_lookup(query: str, revision: int) -> str:
                """Evaluation lookup query. 评测查询器新版 LOOKUP_VERSION_TWO；revision必须为2。"""
                if revision!=2:raise ValueError('revision must be 2')
                effects.append({'tool':'eval_lookup','version':2,'query':query,'revision':revision})
                return json.dumps({'version':'LOOKUP_VERSION_TWO','query':query,'revision':revision})
        self.functions['eval_lookup']=eval_lookup

    def install_probe(self):
        effects=self.effects
        @function_tool
        async def eval_temp_probe() -> str:
            """临时温度探针 temporary temperature probe，仅用于目录删除评测。"""
            effects.append({'tool':'eval_temp_probe'})
            return 'probe'
        self.functions['eval_temp_probe']=eval_temp_probe
