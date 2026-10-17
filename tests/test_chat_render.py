"""Run real Markdown code in a bounded JS VM, including typewriter prefixes."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

SCRIPT=Path(__file__).resolve().parents[1]/'muse/static/chat-render.js'
NODE=shutil.which('node')
HARNESS=r'''
const vm=require('node:vm'),fs=require('node:fs');
const renderer=fs.readFileSync(process.argv[1],'utf8');
class Element {
 constructor(tag,text=''){this.tag=tag;this.textContent=text;this.childNodes=[];}
 append(...children){this.childNodes.push(...children);}
 setAttribute(){}
}
const context={window:{},document:{createElement:tag=>new Element(tag),createTextNode:text=>new Element('#text',text)}};
vm.createContext(context);vm.runInContext(renderer,context,{timeout:500});
const inputs=JSON.parse(process.argv[2]);
for(const text of inputs){
 context.input=text;
 vm.runInContext('result=window.renderMuseMessage({role:"assistant",text:input})',context,{timeout:250});
}
console.log(JSON.stringify({rendered:inputs.length}));
'''

@unittest.skipUnless(NODE,'Node.js unavailable')
class ChatRendererTests(unittest.TestCase):
    def render(self,inputs):
        result=subprocess.run([NODE,'-e',HARNESS,str(SCRIPT),json.dumps(inputs)],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['rendered'],len(inputs))
    def test_incomplete_empty_list_items_never_stall(self):
        self.render(['- ','+ ','* ','1. ','12. ','   - ','- first\n- ','1. first\n2. ','- first\n1. '])
    def test_every_typewriter_prefix_terminates(self):
        samples=['说明\n- 第一项\n- 第二项\n\n1. 步骤一\n2. 步骤二',
                 '# 标题\n> 引用\n\n| A | B |\n| --- | --- |\n| a | b |\n\n```python\nprint(1)\n```']
        self.render([sample[:end] for sample in samples for end in range(len(sample)+1)])
