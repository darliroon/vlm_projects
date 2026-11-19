import json, requests, time, sys
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,'.')
from mscall import BASE, KEY
H={"Authorization":"Bearer "+KEY}

# /models 里这几个模型的条目
r=requests.get(BASE+"/models",headers=H,timeout=30).json()["data"]
for m in r:
    if m["id"] in ("gpt-6-astra","claude-fable-5-2","h:gpt-6-astra"):
        print("MODEL ENTRY:",json.dumps(m,ensure_ascii=False)[:400])

Q="一个数列 a1=3, a(n+1)=2*a(n)-1。求 a10，并验证 a10 除以 7 的余数。只给最终数字和一句话。"
VARIANTS={
 "gpt-6-astra":{
   "base":{},
   "reasoning_effort=high":{"reasoning_effort":"high"},
   "reasoning_effort=xhigh":{"reasoning_effort":"xhigh"},
   "reasoning={effort:high}":{"reasoning":{"effort":"high"}},
 },
 "claude-fable-5-2":{
   "base":{},
   "thinking=enabled/8000":{"thinking":{"type":"enabled","budget_tokens":8000}},
   "thinking=adaptive":{"thinking":{"type":"adaptive"}},
   "reasoning_effort=high":{"reasoning_effort":"high"},
   "output_config.effort=high":{"output_config":{"effort":"high"}},
 },
}
def go(job):
    model,name,extra=job
    p={"model":model,"messages":[{"role":"user","content":Q}],"max_tokens":12000}
    p.update(extra)
    t=time.time()
    try:
        x=requests.post(BASE+"/chat/completions",json=p,headers=H,timeout=240)
    except Exception as e:
        return model,name,f"EXC {e}"
    dt=round(time.time()-t,1)
    if x.status_code!=200: return model,name,f"HTTP {x.status_code} {x.text[:200]}"
    j=x.json(); msg=j["choices"][0]["message"]
    u=j.get("usage",{})
    rk=[k for k in msg if k not in("role","content") and msg[k]]
    return model,name,f"{dt}s completion_tokens={u.get('completion_tokens')} details={u.get('completion_tokens_details')} extra_msg_keys={rk} content={ (msg.get('content') or '')[:80]!r}"
jobs=[(m,n,e) for m,v in VARIANTS.items() for n,e in v.items()]
with ThreadPoolExecutor(9) as ex:
    for model,name,res in ex.map(go,jobs): print(f"[{model}] {name}: {res}")
