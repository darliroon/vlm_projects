"""卫片车道增减标注小工具（仅用标准库）。

用法:  python annotate_tool.py [--port 8765]
浏览器打开 http://127.0.0.1:8765 （VS Code 远程开发时，在"端口"面板转发该端口即可）

标注保存在 annotations.json:
  {"0102": {"status": "event"|"none"|"unsure", "boxes": [[x0,y0,x1,y1], ...], "note": "..."}}
坐标为归一化 0~1，(0,0) 在左上角，与模型输出的 bbox 同一格式。
"""
import argparse
import json
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = "/home/liufeng/data/lane-tiles"
ANN = os.path.join(HERE, "annotations.json")
LOCK = threading.Lock()
IDX = re.compile(r"^\d{4}$")
STATUS = {"event", "none", "unsure"}
MAX_BODY = 64 * 1024

# 用户口述的第一条标注：0102 的 E6(F7)-G9，即 x 0.4~0.7, y 0.5~0.9
SEED = {"0102": {"status": "event", "boxes": [[0.4, 0.5, 0.7, 0.9]],
                 "note": "用户口述 E6(F7)-G9 车道增减；格子换算为框，如有偏差请直接改"}}


def load_ann():
    if not os.path.exists(ANN):
        save_ann(SEED)
        return dict(SEED)
    with open(ANN, encoding="utf-8") as f:
        return json.load(f)


def save_ann(data):
    fd, tmp = tempfile.mkstemp(dir=HERE, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, ANN)  # 原子替换，避免写一半崩溃损坏标注


def tile_list():
    meta = {}
    try:
        for t in json.load(open(os.path.join(ROOT, "manifest.json")))["tiles"]:
            meta[f"{t['index']:04d}"] = (t.get("highway", ""), t.get("ref", ""))
    except (OSError, KeyError, ValueError):
        pass
    names = sorted(n[:-4] for n in os.listdir(os.path.join(ROOT, "z19")) if IDX.match(n[:-4]))
    return [{"i": n, "hw": meta.get(n, ("", ""))[0], "ref": meta.get(n, ("", ""))[1]} for n in names]


def clean_ann(obj):
    """校验并规整前端提交的单张标注，非法输入抛 ValueError。"""
    if obj.get("status") not in STATUS:
        raise ValueError("bad status")
    boxes = []
    for b in obj.get("boxes", []):
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in b)
        x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        if x1 - x0 > 0.005 and y1 - y0 > 0.005:
            boxes.append([round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)])
    if obj["status"] != "event":
        boxes = []
    return {"status": obj["status"], "boxes": boxes, "note": str(obj.get("note", ""))[:2000]}


def model_preds(idx):
    out = []
    base = os.path.join(HERE, "ret", "v2", "results")
    if not os.path.isdir(base):
        return out
    for model in sorted(os.listdir(base)):
        for mode in sorted(os.listdir(os.path.join(base, model))):
            fp = os.path.join(base, model, mode, f"{idx}.json")
            if os.path.exists(fp):
                regs = (json.load(open(fp, encoding="utf-8")).get("parsed") or {}).get("regions", [])
                out.append({"model": model, "mode": mode,
                            "boxes": [r["bbox"] for r in regs if len(r.get("bbox", [])) == 4]})
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 保持终端安静
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/tiles":
            return self._send(200, tile_list())
        if path == "/api/ann":
            with LOCK:
                return self._send(200, load_ann())
        m = re.match(r"^/api/preds/(\d{4})$", path)
        if m:
            return self._send(200, model_preds(m.group(1)))
        m = re.match(r"^/img/(z19|z20)/(\d{4})$", path)  # 正则限定字符，杜绝路径穿越
        if m:
            fp = os.path.join(ROOT, m.group(1), m.group(2) + ".png")
            if os.path.exists(fp):
                return self._send(200, open(fp, "rb").read(), "image/png")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        m = re.match(r"^/api/ann/(\d{4})$", self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_BODY:
            return self._send(413, {"error": "too large"})
        try:
            obj = clean_ann(json.loads(self.rfile.read(n)))
        except (ValueError, TypeError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        with LOCK:
            data = load_ann()
            data[m.group(1)] = obj
            save_ann(data)
        self._send(200, {"ok": True, "saved": len(data)})

    def do_DELETE(self):
        m = re.match(r"^/api/ann/(\d{4})$", self.path)
        if not m:
            return self._send(404, {"error": "not found"})
        with LOCK:
            data = load_ann()
            data.pop(m.group(1), None)
            save_ann(data)
        self._send(200, {"ok": True})


PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>车道增减标注</title>
<style>
body{margin:0;font:14px sans-serif;display:flex;height:100vh;background:#222;color:#ddd}
#side{width:230px;display:flex;flex-direction:column;border-right:1px solid #444}
#side input,#side select{margin:6px;padding:4px}
#list{flex:1;overflow:auto}
#list div{padding:3px 8px;cursor:pointer;display:flex;justify-content:space-between}
#list div:hover{background:#333}#list div.cur{background:#246}
.st-event{color:#4f4}.st-none{color:#999}.st-unsure{color:#fa4}
#main{flex:1;display:flex;flex-direction:column;align-items:center;padding:8px;overflow:auto}
#bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
button{padding:5px 10px;cursor:pointer}button.on{background:#38c;color:#fff}
#wrap{position:relative;line-height:0}
canvas{border:1px solid #666;cursor:crosshair}
#note{width:min(90vmin,800px);margin-top:6px}
.legend span{margin-right:10px}
</style></head><body>
<div id="side">
 <select id="filter"><option value="all">全部</option><option value="todo">未标注</option>
  <option value="event">有事件</option><option value="none">无事件</option><option value="unsure">不确定</option></select>
 <input id="jump" placeholder="跳转编号，如 0256，回车">
 <div id="list"></div>
 <div id="stat" style="padding:6px;border-top:1px solid #444"></div>
</div>
<div id="main">
 <div id="bar">
  <b id="title"></b>
  <button id="bEvent">有事件 (E)</button><button id="bNone">无事件 (N)</button><button id="bUnsure">不确定 (U)</button>
  <button id="bUndo">撤销上一框 (Z)</button><button id="bClear">清空框 (C)</button>
  <button id="bGrid">网格 (G)</button><button id="bPred">显示模型预测 (M)</button>
  <button id="bZ20">z20 图 (Y)</button>
  <button id="bPrev">← 上一张 (A)</button><button id="bNext">下一张 (D) →</button>
 </div>
 <div class="legend"><span style="color:#0f0">■ 你的标注</span><span style="color:#f55">■ gpt</span><span style="color:#0cf">■ claude</span>
  <span>在图上按住鼠标拖框，可画多个；改状态/画框会自动保存</span></div>
 <div id="wrap"><canvas id="cv" width="800" height="800"></canvas></div>
 <textarea id="note" rows="2" placeholder="备注（可选）：例如 右侧深色车道由上到下逐渐变宽，是新增的辅助车道"></textarea>
</div>
<script>
const S=800, cv=document.getElementById('cv'), cx=cv.getContext('2d');
let tiles=[], ann={}, cur=null, boxes=[], preds=[], img=new Image(), showGrid=false, showPred=false, useZ20=false, drag=null, loadSeq=0;
const $=id=>document.getElementById(id);
const stOf=i=>(ann[i]||{}).status||'';
async function api(path,opt){const r=await fetch(path,opt);if(!r.ok)throw new Error(path+' '+r.status);return r.json();}
function renderList(){
  const f=$('filter').value, el=$('list'); el.innerHTML='';
  tiles.filter(t=>f==='all'||(f==='todo'?!stOf(t.i):stOf(t.i)===f)).forEach(t=>{
    const d=document.createElement('div'); if(t.i===cur) d.className='cur';
    const lab={event:'有',none:'无',unsure:'?'}[stOf(t.i)]||'';
    d.innerHTML=`<span>${t.i} <small>${t.hw==='motorway'?'A':t.hw==='trunk'?'T':t.hw==='primary'?'P':''}</small></span><span class="st-${stOf(t.i)}">${lab}</span>`;
    d.onclick=()=>open(t.i); el.appendChild(d);});
  const c={event:0,none:0,unsure:0}; Object.values(ann).forEach(a=>c[a.status]++);
  $('stat').textContent=`有事件 ${c.event} · 无事件 ${c.none} · 不确定 ${c.unsure} · 共 ${tiles.length}`;
}
function draw(){
  cx.clearRect(0,0,S,S); if(img.complete&&img.naturalWidth) cx.drawImage(img,0,0,S,S);
  if(showGrid){cx.strokeStyle='rgba(255,255,0,.45)';cx.fillStyle='rgba(255,255,0,.9)';cx.font='11px sans-serif';
    for(let k=0;k<10;k++){cx.beginPath();cx.moveTo(k*80,0);cx.lineTo(k*80,S);cx.moveTo(0,k*80);cx.lineTo(S,k*80);cx.stroke();
      for(let r=0;r<10;r++) cx.fillText('ABCDEFGHIJ'[k]+(r+1),k*80+3,r*80+12);}}
  if(showPred) preds.forEach(p=>{cx.strokeStyle=p.model.startsWith('gpt')?'#f55':'#0cf';cx.lineWidth=2;
    cx.setLineDash(p.mode==='max'?[]:[6,4]);p.boxes.forEach(b=>cx.strokeRect(b[0]*S,b[1]*S,(b[2]-b[0])*S,(b[3]-b[1])*S));});
  cx.setLineDash([]);cx.lineWidth=3;cx.strokeStyle='#0f0';
  boxes.forEach(b=>cx.strokeRect(b[0]*S,b[1]*S,(b[2]-b[0])*S,(b[3]-b[1])*S));
  if(drag){cx.strokeStyle='#ff0';cx.strokeRect(drag.x0,drag.y0,drag.x1-drag.x0,drag.y1-drag.y0);}
  ['bEvent','bNone','bUnsure'].forEach((id,k)=>$(id).classList.toggle('on',stOf(cur)===['event','none','unsure'][k]));
  $('bGrid').classList.toggle('on',showGrid);$('bPred').classList.toggle('on',showPred);$('bZ20').classList.toggle('on',useZ20);
}
async function save(status){
  if(!cur) return; const s=status||stOf(cur)||'event';
  ann[cur]={status:s,boxes:s==='event'?boxes:[],note:$('note').value};
  if(s!=='event') boxes=[];
  await api('/api/ann/'+cur,{method:'POST',body:JSON.stringify(ann[cur])}); renderList(); draw();
}
function loadImg(){const seq=++loadSeq; img=new Image(); img.onload=()=>{if(seq===loadSeq)draw();}; img.src=`/img/${useZ20?'z20':'z19'}/${cur}`;}
async function open(i){
  cur=i; const a=ann[i]||{}; boxes=(a.boxes||[]).map(b=>b.slice()); $('note').value=a.note||'';
  $('title').textContent=i; loadImg(); preds=await api('/api/preds/'+i); renderList(); draw();
}
function step(d){const k=tiles.findIndex(t=>t.i===cur);const n=tiles[Math.min(tiles.length-1,Math.max(0,k+d))];if(n)open(n.i);}
function pos(e){const r=cv.getBoundingClientRect();return{x:Math.min(S,Math.max(0,(e.clientX-r.left)*S/r.width)),y:Math.min(S,Math.max(0,(e.clientY-r.top)*S/r.height))};}
cv.onmousedown=e=>{const p=pos(e);drag={x0:p.x,y0:p.y,x1:p.x,y1:p.y};};
window.onmousemove=e=>{if(!drag)return;const p=pos(e);drag.x1=p.x;drag.y1=p.y;draw();};
window.onmouseup=async()=>{if(!drag)return;const d=drag;drag=null;
  const b=[Math.min(d.x0,d.x1)/S,Math.min(d.y0,d.y1)/S,Math.max(d.x0,d.x1)/S,Math.max(d.y0,d.y1)/S];
  if(b[2]-b[0]>0.01&&b[3]-b[1]>0.01){boxes.push(b);await save('event');}else draw();};
$('bEvent').onclick=()=>save('event');$('bNone').onclick=()=>save('none');$('bUnsure').onclick=()=>save('unsure');
$('bUndo').onclick=()=>{boxes.pop();save();};$('bClear').onclick=()=>{boxes=[];save();};
$('bGrid').onclick=()=>{showGrid=!showGrid;draw();};$('bPred').onclick=()=>{showPred=!showPred;draw();};
$('bZ20').onclick=()=>{useZ20=!useZ20;loadImg();draw();};
$('bPrev').onclick=()=>step(-1);$('bNext').onclick=()=>step(1);
$('note').onchange=()=>{if(ann[cur]||boxes.length)save();};
$('filter').onchange=renderList;
$('jump').onkeydown=e=>{if(e.key==='Enter'){const v=e.target.value.padStart(4,'0');if(tiles.some(t=>t.i===v))open(v);}};
window.onkeydown=e=>{if(['TEXTAREA','INPUT','SELECT'].includes(document.activeElement.tagName))return;
  const k=e.key.toLowerCase();
  if(k==='e')save('event');else if(k==='n')save('none');else if(k==='u')save('unsure');
  else if(k==='z'){boxes.pop();save();}else if(k==='c'){boxes=[];save();}
  else if(k==='g'){showGrid=!showGrid;draw();}else if(k==='m'){showPred=!showPred;draw();}
  else if(k==='y'){useZ20=!useZ20;loadImg();draw();}
  else if(k==='a'||e.key==='ArrowLeft')step(-1);else if(k==='d'||e.key==='ArrowRight')step(1);};
(async()=>{tiles=await api('/api/tiles');ann=await api('/api/ann');
  const first=tiles.find(t=>!stOf(t.i))||tiles[0];await open(first.i);})();
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    load_ann()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)  # 只监听本机
    print(f"标注工具: http://127.0.0.1:{args.port}   标注文件: {ANN}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
