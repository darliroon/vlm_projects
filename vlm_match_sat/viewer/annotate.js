/* 人工标注模块
 * ------------------------------------------------------------------
 * 目标：让使用者在查看器里直接指出"哪里错了、应该改成什么形状"，而不是只能用文字描述。
 *
 * 一条标注 = 一个"问题"（1 vs N）：
 *   ◎ 点问题  —— 点一下标出问题所在的区域（例如一个分合流口），写类型和整体说明。
 *   一个问题往往要改好几处，所以问题下面可以挂任意多条几何操作 ops[]：
 *   ⤳ 连/补线 —— 逐点点击，画出"这两处应该连起来"或"这里应该新增一条这样的线"。
 *                 连成的折线就是给模型的样例形状，不用再写"向南延伸 30 m 再并入主线"。
 *   ✕ 删线    —— 直接点一条线，说明它不该存在。
 *   还没有打开的问题时先画连线/删线，第一个点会自动当作问题位置。
 *
 * 数据：row = {kind:'issue', x, y, xy_global, pts:[锚点], type, fix, note,
 *              ops:[{kind:'link'|'del', pts:[{x,y,lane,idx}], xy_global_pts, lane}]}
 *       旧格式（单独的 kind:'link'/'del' 行、没有 ops 的 'point' 行）读入时自动转换。
 *
 * 吸附：鼠标靠近车道顶点（包括线要素中间的每一个点，不只是端点）会自动吸过去；
 *       靠近线段中间则投影到线上；按住 Alt 可强制自由取点。
 *
 * 产出两样东西：
 *   1. few-shot 示例 —— 直接喂进 prompt，让模型看到真实判例
 *   2. 真值（ground truth）—— 用来算 precision / recall
 *
 * 坐标：点击位置是查看器底图（2048 px）里的像素；保存时同时换算成整幅卫片的像素坐标
 *       xy_global。后者才是真正用的：模型看到的图（1024 px、旋转角可能不同）与查看器
 *       底图不是同一个坐标系，只有整幅卫片坐标对所有版本都成立。
 * 存储：每次保存/删除都自动 POST api/annots，服务端写到 human_annots.json（不用手动导出）；
 *       同时写 localStorage 作离线兜底。打开页面时以服务端文件为准。
 */
(function () {
  'use strict';

  /* 问题类型还没固化，所以除了下面这些内置项，标注时可以直接输入新的类型。
   * 自定义类型的 type 字段存的就是使用者输入的文字本身，下游对未知 type 一律按原文透传。 */
  const TYPES = [
    { k: 'missing_lane', label: '该续未续', color: '#ff4d4d' },
    { k: 'wrong_break', label: '连了但不该连', color: '#ff9f2e' },
    { k: 'wrong_branch', label: '分合流错', color: '#c14dff' },
    { k: 'wrong_count', label: '车道数不符', color: '#3d9bff' },
    { k: 'ok', label: '这里是对的', color: '#3fbf6a' },
  ];
  const CUSTOM = '__custom__';
  const LST = 'lane-annots-custom-types-v1';   // 用过哪些自定义类型，下次直接在下拉提示里选
  const CUSTOM_COLORS = ['#e05fa0', '#2fc4c4', '#a8b83c', '#d9822b', '#8b7bff', '#4db86a'];
  const LS = 'lane-annots-v1';
  const LS_UI = 'lane-annots-ui-v1';           // 面板宽度、字号等界面偏好的记忆
  /* 旧格式 -> 问题 + ops。单独的连线/删线行变成"只有一条操作的问题"，锚点取它的第一个点 */
  function normalize(a) {
    if (a.kind === 'issue') return Object.assign(a, { ops: a.ops || [] });
    if (a.kind === 'link' || a.kind === 'del') {
      const pts = (a.pts && a.pts.length) ? a.pts : [{ x: a.x, y: a.y, lane: null, idx: null }];
      const g = a.xy_global_pts && a.xy_global_pts.length === pts.length ? a.xy_global_pts : null;
      return Object.assign({}, a, {
        kind: 'issue', pts: [pts[0]], xy_global_pts: [g ? g[0] : a.xy_global], lane: null,
        ops: [{ kind: a.kind, pts, xy_global_pts: g || [a.xy_global], lane: a.lane != null ? a.lane : null }],
      });
    }
    return Object.assign(a, { kind: 'issue', ops: a.ops || [] });
  }
  let ANNOTS = {};
  try { ANNOTS = JSON.parse(localStorage.getItem(LS) || '{}') || {}; } catch (e) { ANNOTS = {}; }
  for (const k of Object.keys(ANNOTS)) ANNOTS[k] = (ANNOTS[k] || []).map(normalize);

  let UI = { fs: 13, w: 360 };
  try { UI = Object.assign(UI, JSON.parse(localStorage.getItem(LS_UI) || '{}') || {}); } catch (e) { /* 用默认 */ }
  function saveUI() {
    try { localStorage.setItem(LS_UI, JSON.stringify(UI)); } catch (e) { /* 内存模式 */ }
  }

  let annotMode = false;
  let tool = 'point';        // point | link | del
  let iss = null;            // 当前打开的问题 {anchor:{x,y,lane,idx}, ops:[...]}，未保存
  let op = null;             // 正在画、还没完成的连线 {kind:'link', pts:[...]}
  let editing = null;        // 正在编辑的已保存问题 id（新建时为 null）
  let hover = null;          // 鼠标当前吸附到的点 / 线
  let hovG = null;           // 悬浮提示所在的图层
  let SCALED = [];           // 需要随缩放保持屏幕尺寸的图元：[元素, 应用函数]

  const S = () => window.DATA.size;
  const V = () => window.VIEW;                       // {cur, tf}，由 index.html 提供
  const cand = () => window.DATA.cands[V().cur];
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs) => {
    const e = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };

  /* 查看器底图坐标 -> 整幅卫片像素坐标（render.py 里 rot() 的逆变换） */
  function toGlobal(c, x, y) {
    const a = -c.rotation_exact * Math.PI / 180, ca = Math.cos(a), sa = Math.sin(a);
    const u = x - S() / 2, v = y - S() / 2;
    return [+(u * ca + v * sa + c.center_xy[0]).toFixed(2),
            +(-u * sa + v * ca + c.center_xy[1]).toFixed(2)];
  }
  /* 这个点在不在模型当时看到的图幅里（图幅外的标注做不了 few-shot） */
  function inFov(c, x, y) {
    const q = c.annot_fov;
    if (!q) return true;
    let inside = false;
    for (let i = 0, j = q.length - 1; i < q.length; j = i++) {
      if ((q[i][1] > y) !== (q[j][1] > y) &&
          x < (q[j][0] - q[i][0]) * (y - q[i][1]) / (q[j][1] - q[i][1]) + q[i][0]) inside = !inside;
    }
    return inside;
  }
  const uid = () => 'a' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);

  /* ---------------- 类型 -> 标签/颜色 ---------------- */
  function loadCustoms() {
    try { return JSON.parse(localStorage.getItem(LST) || '[]') || []; } catch (e) { return []; }
  }
  let CUSTOMS = loadCustoms();
  function saveCustoms() {
    try { localStorage.setItem(LST, JSON.stringify(CUSTOMS)); } catch (e) { /* 内存模式 */ }
  }
  /* 未知类型：标签就用原文，颜色从调色板里按名字稳定地取一个 */
  function typeInfo(k) {
    const b = TYPES.find(x => x.k === k);
    if (b) return b;
    const s = String(k || '未知');
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return { k: s, label: s, color: CUSTOM_COLORS[h % CUSTOM_COLORS.length] };
  }

  /* ---------------- 样式 ---------------- */
  const css = `
  #ann-btn{padding:4px 9px;border:1px solid #2b3040;background:#1b2029;color:#e6e8ee;
    border-radius:5px;font-size:11.5px;cursor:pointer;transition:.1s;}
  #ann-btn:hover{background:#252c3a;border-color:#4a5570;}
  #ann-btn.on{background:#3d2f14;border-color:#ffb020;color:#ffd54a;}

  #ann-panel{position:absolute;left:13px;top:52px;width:360px;min-width:260px;min-height:80px;
    background:#14181ff2;backdrop-filter:blur(10px);border:1px solid #2b3040;border-radius:8px;
    padding:11px 13px 12px;z-index:6;font-size:13px;line-height:1.6;display:none;
    max-height:calc(100% - 90px);overflow:auto;resize:both;}
  #ann-panel.show{display:block;}
  #ann-panel h4{margin:0 0 9px;font-size:.85em;letter-spacing:.6px;color:#8b93a6;
    text-transform:uppercase;font-weight:600;display:flex;align-items:center;gap:8px;}
  #ann-panel h4 .grow{flex:1;}
  #ann-panel h4 button{padding:1px 7px;border:1px solid #2b3040;background:#1b2029;
    color:#c4cbdb;border-radius:4px;font-size:.85em;cursor:pointer;line-height:1.5;}
  #ann-panel h4 button:hover{background:#252c3a;color:#fff;}

  .ann-tools{display:flex;gap:5px;margin-bottom:9px;}
  .ann-tools button{flex:1;padding:6px 4px;border-radius:5px;border:1px solid #2b3040;
    background:#1b2029;color:#9aa4ba;cursor:pointer;font-size:.92em;transition:.1s;}
  .ann-tools button:hover{background:#252c3a;color:#fff;}
  .ann-tools button.on{background:#26334a;border-color:#5b6d94;color:#cfe0ff;}
  .ann-tools button.on[data-tool="del"]{background:#3a1d24;border-color:#a8445a;color:#ffb0c0;}

  #ann-panel .hint{color:#8b93a6;font-size:.82em;line-height:1.55;margin-bottom:9px;}
  #ann-panel .hint b{color:#c4cbdb;}
  #ann-form label{display:block;color:#9aa4ba;font-size:.82em;margin:9px 0 4px;}
  #ann-form select,#ann-form textarea,#ann-form input{width:100%;background:#0f1319;
    border:1px solid #2b3040;border-radius:4px;color:#e6e8ee;font-size:.95em;
    padding:6px 8px;outline:none;font-family:inherit;resize:vertical;}
  #ann-form select:focus,#ann-form textarea:focus,#ann-form input:focus{border-color:#4a5570;}
  #ann-form textarea{min-height:58px;line-height:1.55;}
  .ann-actions{display:flex;gap:6px;margin-top:10px;}
  .ann-actions button{flex:1;padding:6px;border-radius:4px;border:1px solid #3a4257;
    background:#1e2430;color:#c4cbdb;cursor:pointer;font-size:.92em;}
  .ann-actions button.primary{background:#1c3d29;border-color:#3fbf6a;color:#8fdcac;}
  .ann-actions button:hover{filter:brightness(1.2);}
  #ann-sync{font-size:.8em;margin-top:6px;color:#8b93a6;}
  #ann-sync.ok{color:#8fdcac;} #ann-sync.bad{color:#ff9db0;}
  #ann-warn{color:#ffb86b;font-size:.82em;line-height:1.5;margin-top:4px;display:none;}
  #ann-dbar{margin:4px 0 8px;padding:6px 8px;border-radius:5px;background:#1d2430;
    border:1px solid #33405a;font-size:.88em;color:#b9c6e0;display:none;}

  .ann-item{display:flex;gap:7px;align-items:flex-start;padding:6px 7px;border-radius:5px;
    background:#1b202a;margin-bottom:5px;cursor:pointer;}
  .ann-item:hover{background:#222836;}
  .ann-item .sw{width:3px;border-radius:2px;flex:none;align-self:stretch;}
  .ann-item .t{flex:1;min-width:0;}
  .ann-item .tt{font-weight:600;font-size:.92em;}
  .ann-item .tx{color:#8b93a6;font-size:.82em;line-height:1.45;
    overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
  .ann-item .del{color:#5c6478;font-size:1.1em;padding:0 3px;}
  .ann-item .del:hover{color:#ff5d7a;}
  .ann-item.on{outline:1px solid #ffd54a;}
  #ann-ops{display:flex;flex-direction:column;gap:3px;}
  .ann-op{display:flex;gap:6px;align-items:center;padding:3px 6px;border-radius:4px;
    background:#0f1319;border:1px solid #232a38;font-size:.85em;color:#c4cbdb;}
  .ann-op .k{flex:none;width:1.2em;text-align:center;}
  .ann-op .d{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .ann-op .x{color:#5c6478;cursor:pointer;padding:0 3px;}
  .ann-op .x:hover{color:#ff5d7a;}
  .ann-op.cur{border-style:dashed;border-color:#ffd54a;color:#ffd54a;}
  .ann-op-empty{color:#5c6478;font-size:.82em;}

  #ann-svg{position:absolute;left:0;top:0;width:2048px;height:2048px;pointer-events:none;}
  @keyframes annpulse{0%,100%{opacity:.5}50%{opacity:1}}
  .ann-draft{animation:annpulse 1.1s ease-in-out infinite;}
  `;
  const st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  /* ---------------- DOM ---------------- */
  const topbar = document.getElementById('topbar');
  const btn = document.createElement('button');
  btn.id = 'ann-btn';
  btn.textContent = '✎ 标注模式';
  topbar.insertBefore(btn, topbar.querySelector('.spacer'));

  const stage = document.getElementById('stage');
  const world = document.getElementById('world');
  const svg = mk('svg', { id: 'ann-svg', viewBox: `0 0 ${S()} ${S()}` });
  world.appendChild(svg);

  const panel = document.createElement('div');
  panel.id = 'ann-panel';
  panel.innerHTML = `
    <h4><span>人工标注</span><span class="grow"></span>
      <button id="ann-fs-dec" title="缩小字号">A−</button>
      <button id="ann-fs-inc" title="放大字号">A+</button></h4>
    <div class="ann-tools">
      <button data-tool="point" class="on" title="快捷键 Q">◎ 点问题</button>
      <button data-tool="link" title="快捷键 W">⤳ 连/补线</button>
      <button data-tool="del" title="快捷键 E">✕ 删线</button>
    </div>
    <div class="hint" id="ann-hint"></div>
    <div id="ann-form" style="display:none">
      <div style="color:#ffd54a;font-size:.82em"><span id="ann-title">新问题</span> · 位置 <span id="ann-xy"></span></div>
      <div id="ann-warn">问题位置在模型视野之外：会计入真值，但做不了 few-shot 判例。</div>
      <label>问题类型</label>
      <select id="ann-type"></select>
      <input id="ann-type-custom" placeholder="输入新的问题类型，例：标线缺失 / 汇入角太急"
             style="display:none;margin-top:4px">
      <label>整体怎么改（自由描述）</label>
      <textarea id="ann-fix" placeholder="例：右转匝道应在鼻端从主线分出：补两条连线，删掉横穿导流区的那条"></textarea>
      <label>补充说明（可选）</label>
      <input id="ann-note" placeholder="例：影像上车行道仍在延续">
      <label>几何操作 <span id="ann-ops-n"></span></label>
      <div id="ann-ops"></div>
      <div class="hint" style="margin:4px 0 0">切到 <b>⤳ 连/补线（W）</b>或 <b>✕ 删线（E）</b>在图上给这个问题加操作，
        数量不限；<b>◎（Q）</b>状态下再点图是移动问题位置。</div>
      <div class="ann-actions">
        <button class="primary" id="ann-save">保存问题</button>
        <button id="ann-cancel">取消</button>
      </div>
    </div>
    <div id="ann-dbar"></div>
    <div id="ann-list"></div>
    <div class="hint" id="ann-count" style="margin-top:9px"></div>
    <div id="ann-sync"></div>
    <div class="ann-actions">
      <button id="ann-export">下载备份 JSON</button>
    </div>
  `;
  stage.appendChild(panel);
  applyUI();

  function applyUI() {
    panel.style.fontSize = UI.fs + 'px';
    panel.style.width = UI.w + 'px';
  }

  const form = panel.querySelector('#ann-form');
  const typeSel = panel.querySelector('#ann-type');
  const customEl = panel.querySelector('#ann-type-custom');
  const fixEl = panel.querySelector('#ann-fix');
  const noteEl = panel.querySelector('#ann-note');
  const opsEl = panel.querySelector('#ann-ops');
  const list = panel.querySelector('#ann-list');
  const dbar = panel.querySelector('#ann-dbar');
  const hintEl = panel.querySelector('#ann-hint');
  typeSel.addEventListener('change', syncCustom);
  fillTypeOptions(TYPES[0].k);

  function fillTypeOptions(selected) {
    const opts = TYPES.map(t => [t.k, `${t.label}（${t.k}）`]);
    CUSTOMS.filter(c => !TYPES.some(t => t.k === c)).forEach(c => opts.push([c, c]));
    opts.push([CUSTOM, '其它（自己输入）…']);
    const all = opts.some(o => o[0] === selected) ? opts : opts.concat([[selected, selected]]);
    typeSel.innerHTML = '';
    all.forEach(([v, l]) => {
      const o = document.createElement('option');
      o.value = v; o.textContent = l;
      typeSel.appendChild(o);
    });
    typeSel.value = selected;
    syncCustom();
  }
  function syncCustom() {
    const on = typeSel.value === CUSTOM;
    customEl.style.display = on ? 'block' : 'none';
    if (on) customEl.focus();
  }

  const HINTS = {
    point: '一个问题 = 一个区域（例如一处分合流口）。在图上点一下标出<b>问题位置</b>，再给它加任意多条连线/删线。'
      + '已打开问题时，再点图是<b>移动问题位置</b>。<b>鼠标靠近车道顶点会自动吸附</b>（按住 Alt 自由取点）。',
    link: '给当前问题加一条连线：按顺序点出<b>正确的走线</b>（或沿看不到的车道点出一串点表示"这里应新增这条线"）。'
      + '<b>Enter 完成这一条</b>，接着点就是下一条；Backspace 撤销上一个点。',
    del: '给当前问题加删线：直接点一条<b>不该存在</b>的线（红线和周边线都可以），可以连续点多条，再点一次取消。',
  };
  /* 换工具只结束正在画的那条连线（够两点就收进问题），不关掉问题本身 */
  function setTool(t, noRender) {
    commitOp();
    tool = t;
    panel.querySelectorAll('.ann-tools button').forEach(b =>
      b.classList.toggle('on', b.dataset.tool === t));
    hintEl.innerHTML = HINTS[t];
    stage.style.cursor = annotMode ? (t === 'del' ? 'pointer' : 'crosshair') : '';
    if (!noRender) { refreshForm(); render(); }
  }
  // 初始化时不能重绘：render() / commitOp() 用到的 lastK 等在后面才声明（let/const 暂时性死区），
  // 这里调 render 会抛 ReferenceError，整个模块中断、标注按钮点了没反应。文件末尾会统一 render 一次。
  setTool('point', true);

  /* ---------------- 坐标换算与吸附 ---------------- */
  function toImage(clientX, clientY) {
    const r = stage.getBoundingClientRect();
    const tf = V().tf;
    return { x: (clientX - r.left - tf.x) / tf.k, y: (clientY - r.top - tf.y) / tf.k };
  }
  const SNAP_PX = 16;     // 顶点吸附半径（屏幕像素）
  const ON_LINE_PX = 9;   // 投影到线上的半径（屏幕像素）

  /* 找最近的顶点 / 线上的点 / 自由点。返回 {x, y, lane, idx, seg} */
  function snapAt(clientX, clientY, free) {
    const p = toImage(clientX, clientY);
    if (free) return { x: p.x, y: p.y, lane: null, idx: null, seg: null };
    const k = V().tf.k, vtol = SNAP_PX / k, ltol = ON_LINE_PX / k;
    let best = null, bd = vtol;
    for (const L of cand().lanes) {
      const P = L.pts;
      for (let i = 0; i < P.length; i++) {
        const d = Math.hypot(P[i][0] - p.x, P[i][1] - p.y);
        if (d < bd) { bd = d; best = { x: P[i][0], y: P[i][1], lane: L.k, road: L.road, idx: i, seg: null }; }
      }
    }
    if (best) return best;
    // 没吸到顶点，就投影到最近的线段上（线上任意一点也能标）
    let bl = null, bld = ltol;
    for (const L of cand().lanes) {
      const P = L.pts;
      for (let i = 1; i < P.length; i++) {
        const a = P[i - 1], b = P[i];
        const vx = b[0] - a[0], vy = b[1] - a[1];
        const l2 = vx * vx + vy * vy;
        const t = l2 ? Math.max(0, Math.min(1, ((p.x - a[0]) * vx + (p.y - a[1]) * vy) / l2)) : 0;
        const qx = a[0] + vx * t, qy = a[1] + vy * t;
        const d = Math.hypot(qx - p.x, qy - p.y);
        if (d < bld) { bld = d; bl = { x: qx, y: qy, lane: L.k, road: L.road, idx: null, seg: i - 1 }; }
      }
    }
    return bl || { x: p.x, y: p.y, lane: null, idx: null, seg: null };
  }

  /* 点离线多远（用于"删线"时选线） */
  function laneAt(clientX, clientY) {
    const p = toImage(clientX, clientY);
    const tol = 12 / V().tf.k;
    let best = null, bd = tol;
    for (const L of cand().lanes) {
      const P = L.pts;
      for (let i = 1; i < P.length; i++) {
        const a = P[i - 1], b = P[i];
        const vx = b[0] - a[0], vy = b[1] - a[1];
        const l2 = vx * vx + vy * vy;
        const t = l2 ? Math.max(0, Math.min(1, ((p.x - a[0]) * vx + (p.y - a[1]) * vy) / l2)) : 0;
        const d = Math.hypot(a[0] + vx * t - p.x, a[1] + vy * t - p.y);
        if (d < bd) { bd = d; best = L; }
      }
    }
    return best;
  }

  /* ---------------- 绘制 ---------------- */
  /* 图元的屏幕尺寸补偿：平移不用管（CSS 已处理），只有缩放变了才需要更新 */
  let lastK = 0;
  function sized(e, fn) { SCALED.push([e, fn]); fn(e, V().tf.k); return e; }
  function restyle(k) {
    for (const [e, fn] of SCALED) fn(e, k);
    lastK = k;
  }

  const PATH = L => 'M' + L.pts.map(q => q[0].toFixed(1) + ' ' + q[1].toFixed(1)).join('L');

  function render() {
    const c = cand(), k = V().tf.k;
    svg.innerHTML = '';
    SCALED = []; lastK = k;
    // v1 的模型视野（150 m）。与"模型视野"图层画的是同一个框时就不重复画。
    const mf = c.model_fov, af = c.annot_fov;
    const same = mf && af && af.every((p, i) =>
      Math.abs(p[0] - mf[i][0]) < 1 && Math.abs(p[1] - mf[i][1]) < 1);
    if (annotMode && af && !same) {
      const tx = mk('text', { x: af[1][0] - 8, y: af[1][1] - 16, 'text-anchor': 'end',
        fill: '#ffd54a', 'font-size': 30, 'font-weight': 600,
        stroke: '#05070b', 'stroke-width': 4, 'paint-order': 'stroke' });
      tx.textContent = 'v1 视野 150 m';
      svg.appendChild(tx);
      svg.appendChild(mk('polygon', { points: af.map(q => q.join(',')).join(' '),
        fill: 'none', stroke: '#ffd54a', 'stroke-width': 2, 'stroke-dasharray': '10 8', opacity: .7 }));
    }

    if (annotMode) drawVertices(k);

    listFor().forEach(a => drawAnnot(a, k));

    if (iss) drawDraft(k);

    hovG = mk('g');
    svg.appendChild(hovG);
    if (hover) drawHover(k);

    renderList();
  }

  /* 所有车道（含周边）的顶点：标注模式下要能选到线要素中间的任意一个点 */
  function drawVertices(k) {
    const c = cand();
    const g = mk('g');
    svg.appendChild(g);
    const tooMany = c.lanes.reduce((n, L) => n + L.pts.length, 0) > 9000;
    // 顶点可能有上千个，缩放时逐个查找/改属性会卡；先把元素和原始坐标收在一起，批量改
    const dots = [], ends = [];
    c.lanes.forEach(L => {
      if (tooMany && !L.hi) return;          // 车道特别密时只标待核实车道的顶点
      const P = L.pts;
      for (let i = 0; i < P.length; i++) {
        const isEnd = (i === 0 || i === P.length - 1);
        const e = isEnd
          ? mk('rect', { rx: 1.5, fill: '#0d1016', stroke: L.hi ? '#ffffff' : '#9aa4ba' })
          : mk('circle', { cx: P[i][0], cy: P[i][1],
              fill: L.hi ? '#ffd0d0' : '#aeb8cc', stroke: '#0d1016' });
        e.setAttribute('opacity', L.hi ? .95 : .6);
        g.appendChild(e);
        (isEnd ? ends : dots).push([e, P[i][0], P[i][1]]);
      }
    });
    sized(g, (e, kk) => {
      const r = 3.2 / kk, sw = 1.3 / kk, es = 5 / kk;
      for (const [d] of dots) { d.setAttribute('r', r); d.setAttribute('stroke-width', sw * .7); }
      for (const [d, x, y] of ends) {
        d.setAttribute('x', x - es); d.setAttribute('y', y - es);
        d.setAttribute('width', es * 2); d.setAttribute('height', es * 2);
        d.setAttribute('stroke-width', sw);
      }
    });
  }

  /* 一条几何操作：连线画折线+箭头，删线把整条线标红并打叉 */
  function drawOp(g, o, color, k, live) {
    const pts = o.pts || [];
    if (o.kind === 'del') {
      const L = o.lane != null ? cand().lanes.find(x => x.k === o.lane) : null;
      if (L) {
        g.appendChild(sized(mk('path', { d: PATH(L), fill: 'none', stroke: '#ff5d7a',
          'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: .85 }),
          (e, kk) => e.setAttribute('stroke-width', 6 / kk)));
        g.appendChild(sized(mk('path', { d: PATH(L), fill: 'none', stroke: '#05070b',
          'stroke-linecap': 'round', 'stroke-dasharray': '6 6' }),
          (e, kk) => e.setAttribute('stroke-width', 1.6 / kk)));
      }
      if (pts[0]) {
        const x = pts[0].x, y = pts[0].y;
        [45, -45].forEach(dg => {
          const c = Math.cos(dg * Math.PI / 180), s = Math.sin(dg * Math.PI / 180);
          g.appendChild(sized(mk('line', { stroke: '#ff5d7a', 'stroke-linecap': 'round' }), (e, kk) => {
            const r = 9 / kk;
            e.setAttribute('x1', x - c * r); e.setAttribute('y1', y - s * r);
            e.setAttribute('x2', x + c * r); e.setAttribute('y2', y + s * r);
            e.setAttribute('stroke-width', 3 / kk);
          }));
        });
      }
      return;
    }
    if (pts.length >= 2) {
      const d = 'M' + pts.map(p => p.x + ' ' + p.y).join('L');
      g.appendChild(sized(mk('path', { d, fill: 'none', stroke: '#05070b',
        'stroke-linejoin': 'round', 'stroke-linecap': 'round' }),
        (e, kk) => e.setAttribute('stroke-width', 7 / kk)));
      g.appendChild(sized(mk('path', { d, fill: 'none', stroke: color,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round', 'stroke-dasharray': live ? '9 7' : 'none' }),
        (e, kk) => e.setAttribute('stroke-width', 3.4 / kk)));
      if (!live) {
        const p1 = pts[pts.length - 2], p2 = pts[pts.length - 1];
        const ang = Math.atan2(p2.y - p1.y, p2.x - p1.x);
        g.appendChild(sized(mk('path', { fill: color }), (e, kk) => {
          const L = 15 / kk;
          e.setAttribute('d', `M${p2.x} ${p2.y}L${p2.x - Math.cos(ang - .4) * L} ${p2.y - Math.sin(ang - .4) * L}`
            + `L${p2.x - Math.cos(ang + .4) * L} ${p2.y - Math.sin(ang + .4) * L}Z`);
        }));
      }
    }
    pts.forEach((p, i) => {
      g.appendChild(sized(mk('circle', { cx: p.x, cy: p.y, fill: '#0d1016', stroke: color }),
        (e, kk) => { e.setAttribute('r', (live && i === pts.length - 1 ? 8 : 4.5) / kk);
                     e.setAttribute('stroke-width', 2.2 / kk); }));
    });
  }

  /* 问题锚点：圆圈 + 十字 + 编号 */
  function drawAnchor(g, p, color, label, k, on) {
    const x = p.x, y = p.y;
    g.appendChild(sized(mk('circle', { cx: x, cy: y, fill: 'none', stroke: color }),
      (e, kk) => { e.setAttribute('r', (on ? 20 : 15) / kk); e.setAttribute('stroke-width', (on ? 3.6 : 2.6) / kk); }));
    [[1, 0], [0, 1]].forEach(([dx, dy]) => g.appendChild(sized(
      mk('line', { stroke: color, opacity: .85 }), (e, kk) => {
        const r = 22 / kk;
        e.setAttribute('x1', x - dx * r); e.setAttribute('y1', y - dy * r);
        e.setAttribute('x2', x + dx * r); e.setAttribute('y2', y + dy * r);
        e.setAttribute('stroke-width', 1.6 / kk);
      })));
    const lab = mk('text', { fill: color, 'font-weight': 600, stroke: '#05070b',
      'paint-order': 'stroke', 'stroke-linejoin': 'round' });
    lab.textContent = label;
    g.appendChild(sized(lab, (e, kk) => {
      e.setAttribute('x', x + 19 / kk); e.setAttribute('y', y - 17 / kk);
      e.setAttribute('font-size', 15 / kk); e.setAttribute('stroke-width', 3.5 / kk);
    }));
  }

  /* 已保存的问题。正在编辑的那条由 drawDraft 画（显示的是未保存的修改） */
  function drawAnnot(a, k) {
    if (editing === a.id) return;
    const t = typeInfo(a.type);
    const g = mk('g', { opacity: iss ? .45 : .95 });   // 有问题在编辑时其它问题淡出
    (a.ops || []).forEach(o => drawOp(g, o, t.color, k, false));
    const p = (a.pts && a.pts[0]) || { x: a.x, y: a.y };
    drawAnchor(g, p, t.color, '#' + a.seq, k, false);
    svg.appendChild(g);
  }

  /* 正在编辑的问题：锚点闪烁，已加的操作实线，正在画的连线虚线 */
  function drawDraft(k) {
    const g = mk('g');
    svg.appendChild(g);
    iss.ops.forEach(o => drawOp(g, o, '#ffd54a', k, false));
    if (op) drawOp(g, op, '#ffd54a', k, true);
    const ga = mk('g', { class: 'ann-draft' });
    g.appendChild(ga);
    const a = editing && listFor().find(x => x.id === editing);
    drawAnchor(ga, iss.anchor, '#ffd54a', a ? '#' + a.seq : '新', k, true);
  }

  /* 悬浮反馈：吸附到哪个顶点 / 哪条线 */
  function drawHover(k) {
    if (!hovG) return;
    hovG.innerHTML = '';
    const g = hovG;
    if (hover.laneHit) {
      const L = hover.laneHit;
      g.appendChild(sized(mk('path', { d: PATH(L), fill: 'none', stroke: '#ff5d7a',
        'stroke-linecap': 'round', opacity: .95 }),
        (e, kk) => e.setAttribute('stroke-width', 9 / kk)));
      const m = L.pts[Math.floor(L.pts.length / 2)];
      const tx = mk('text', { x: m[0] + 10 / k, y: m[1] - 12 / k, fill: '#ff9db0',
        stroke: '#05070b', 'paint-order': 'stroke', 'font-weight': 600 });
      tx.textContent = `删 L${L.k}`;
      g.appendChild(sized(tx, (e, kk) => { e.setAttribute('font-size', 16 / kk); e.setAttribute('stroke-width', 3.5 / kk); }));
      return;
    }
    if (hover.idx == null && hover.lane == null) return;
    g.appendChild(sized(mk('circle', { cx: hover.x, cy: hover.y, fill: 'none', stroke: '#ffd54a' }),
      (e, kk) => { e.setAttribute('r', 11 / kk); e.setAttribute('stroke-width', 2.2 / kk); }));
    if (hover.lane != null) {
      const txt = hover.idx != null ? `L${hover.lane} · 第 ${hover.idx + 1} 点` : `L${hover.lane} · 线上`;
      const tx = mk('text', { x: hover.x + 14 / k, y: hover.y - 14 / k, fill: '#ffd54a',
        stroke: '#05070b', 'paint-order': 'stroke', 'font-weight': 600 });
      tx.textContent = txt;
      g.appendChild(sized(tx, (e, kk) => { e.setAttribute('font-size', 15 / kk); e.setAttribute('stroke-width', 3.5 / kk); }));
    }
  }

  /* ---------------- 列表 ---------------- */
  function curCid() { return cand().cid; }
  function listFor() { return ANNOTS[curCid()] || []; }

  function renderList() {
    const arr = listFor();
    list.innerHTML = '';
    if (!arr.length) {
      list.innerHTML = '<div style="color:#5c6478;font-size:.82em;padding:4px 0">这一处还没有标注</div>';
    }
    arr.forEach(a => {
      const t = typeInfo(a.type);
      const n = (a.ops || []).length;
      const d = document.createElement('div');
      d.className = 'ann-item' + (editing === a.id ? ' on' : '');
      d.innerHTML = `<div class="sw" style="background:${t.color}"></div>
        <div class="t"><div class="tt" style="color:${t.color}"></div>
        <div class="tx"></div></div>
        <div class="del" title="删除这个问题（连同它的所有操作）">×</div>`;
      d.querySelector('.tt').textContent = `#${a.seq} ${t.label}` + (n ? ` · ${n} 条操作` : '');
      d.querySelector('.tx').textContent = a.fix || '';
      d.onclick = (ev) => {
        if (ev.target.classList.contains('del')) {
          ANNOTS[curCid()] = arr.filter(x => x.id !== a.id);
          if (editing === a.id) { iss = null; op = null; editing = null; }
          persist(); render(); return;
        }
        editAnnot(a);
      };
      list.appendChild(d);
    });
    const total = Object.values(ANNOTS).reduce((s, v) => s + v.length, 0);
    panel.querySelector('#ann-count').textContent = `本处 ${arr.length} 个问题 · 全局 ${total} 个`;
  }

  /* ---------------- 当前问题的编辑状态 ---------------- */
  const mkPt = s => ({ x: s.x, y: s.y, lane: s.lane, idx: s.idx });
  const cpOp = o => ({ kind: o.kind, lane: o.lane != null ? o.lane : null, pts: (o.pts || []).map(p => ({ ...p })) });

  function openIssue(anchor) {
    iss = { cid: curCid(), anchor: mkPt(anchor), ops: [] };
    op = null; editing = null;
    fixEl.value = ''; noteEl.value = '';
    fillTypeOptions(typeSel.value || TYPES[0].k);     // 沿用上一次选的类型
  }
  /* 正在画的连线：够两点就收进当前问题，不够就丢掉 */
  function commitOp() {
    if (op && iss && op.pts.length >= 2) iss.ops.push(op);
    op = null;
  }

  function editAnnot(a) {
    commitOp();
    editing = a.id;
    iss = { cid: a.cid, anchor: { ...((a.pts && a.pts[0]) || { x: a.x, y: a.y, lane: null, idx: null }) },
            ops: (a.ops || []).map(cpOp) };
    op = null;
    fillTypeOptions(a.type);
    customEl.value = a.type;
    fixEl.value = a.fix || ''; noteEl.value = a.note || '';
    zoomTo(iss.anchor);
    refreshForm(); render();
  }

  const ptTxt = p => `${p.x.toFixed(0)}, ${p.y.toFixed(0)}`
    + (p.lane != null ? (p.idx != null ? ` · L${p.lane} 第 ${p.idx + 1} 点` : ` · L${p.lane} 线上`) : '');

  function zoomTo(p) {
    if (!p) return;
    if (window.VIEW.center) window.VIEW.center(p.x, p.y, 0.55);
  }

  /* ---------------- 表单 ---------------- */
  function opDesc(o) {
    if (o.kind === 'del') {
      const L = cand().lanes.find(x => x.k === o.lane);
      return `删除 L${o.lane}` + (L ? `（${L.len_m} m）` : '');
    }
    const ends = [o.pts[0], o.pts[o.pts.length - 1]].map(p =>
      p.lane != null ? `L${p.lane}` + (p.idx != null ? `#${p.idx + 1}` : '') : '空白处');
    return `连线 ${o.pts.length} 点：${ends[0]} → ${ends[1]}`;
  }
  /* 按 iss / op 的状态刷新表单；没有打开的问题就收起表单 */
  function refreshForm() {
    if (!iss) { form.style.display = 'none'; dbar.style.display = 'none'; return; }
    form.style.display = 'block';
    panel.querySelector('#ann-title').textContent = editing
      ? `编辑问题 #${(listFor().find(a => a.id === editing) || {}).seq || ''}` : '新问题';
    panel.querySelector('#ann-xy').textContent = ptTxt(iss.anchor);
    panel.querySelector('#ann-warn').style.display = inFov(cand(), iss.anchor.x, iss.anchor.y) ? 'none' : 'block';
    panel.querySelector('#ann-ops-n').textContent = iss.ops.length ? `（${iss.ops.length}）` : '';
    opsEl.innerHTML = '';
    if (!iss.ops.length && !op) {
      opsEl.innerHTML = '<div class="ann-op-empty">还没有操作。只写文字也可以保存。</div>';
    }
    iss.ops.forEach((o, i) => {
      const d = document.createElement('div');
      d.className = 'ann-op';
      d.innerHTML = `<span class="k"></span><span class="d"></span><span class="x" title="删掉这条操作">×</span>`;
      d.querySelector('.k').textContent = o.kind === 'del' ? '✕' : '⤳';
      d.querySelector('.d').textContent = `${i + 1}. ${opDesc(o)}`;
      d.querySelector('.x').onclick = () => { iss.ops.splice(i, 1); refreshForm(); render(); };
      opsEl.appendChild(d);
    });
    if (op) {
      const d = document.createElement('div');
      d.className = 'ann-op cur';
      d.textContent = `⤳ 正在画：${op.pts.length} 个点（Enter 完成，Backspace 撤销）`;
      opsEl.appendChild(d);
    }
    dbar.style.display = 'none';
  }
  function closeIssue() {
    iss = null; op = null; editing = null;
    refreshForm();
  }

  function currentType() {
    if (typeSel.value !== CUSTOM) return typeSel.value;
    return customEl.value.trim();
  }

  /* ---------------- 交互 ---------------- */
  btn.onclick = () => {
    annotMode = !annotMode;
    window.ANNOT_MODE = annotMode;          // 给主脚本判断（双击缩放会抢操作）
    btn.classList.toggle('on', annotMode);
    btn.textContent = annotMode ? '✎ 标注中…' : '✎ 标注模式';
    panel.classList.toggle('show', annotMode);
    stage.style.cursor = annotMode ? (tool === 'del' ? 'pointer' : 'crosshair') : '';
    if (!annotMode) { closeIssue(); hover = null; }
    render();
  };

  panel.querySelectorAll('.ann-tools button').forEach(b =>
    b.onclick = () => setTool(b.dataset.tool));

  panel.querySelector('#ann-fs-dec').onclick = () => {
    UI.fs = Math.max(10, UI.fs - 1); applyUI(); saveUI();
  };
  panel.querySelector('#ann-fs-inc').onclick = () => {
    UI.fs = Math.min(22, UI.fs + 1); applyUI(); saveUI();
  };
  // 手动拖右下角改宽度后记住
  new MutationObserver(() => {
    const w = parseFloat(panel.style.width) || panel.offsetWidth;
    if (Math.abs(w - UI.w) > 4) { UI.w = Math.round(w); saveUI(); }
  }).observe(panel, { attributes: true, attributeFilter: ['style'] });

  let down = null, hoverRAF = 0;
  // 只记录按下位置；mousedown 在面板外才有效
  stage.addEventListener('mousedown', e => {
    down = e.target.closest && e.target.closest('#ann-panel') ? null : { x: e.clientX, y: e.clientY };
  }, true);

  stage.addEventListener('mousemove', e => {
    if (!annotMode || down) return;
    if (e.target.closest && e.target.closest('#ann-panel')) {
      // 只清掉悬浮标记，不整页重绘（顶点可能有几千个，重绘很贵）
      if (hover) { hover = null; if (hovG) hovG.innerHTML = ''; }
      return;
    }
    if (hoverRAF) return;
    const cx = e.clientX, cy = e.clientY, alt = e.altKey;
    hoverRAF = requestAnimationFrame(() => {
      hoverRAF = 0;
      let h;
      if (tool === 'del') {
        const L = laneAt(cx, cy);
        h = L ? { laneHit: L } : null;
      } else {
        const s = snapAt(cx, cy, alt);
        h = (s.lane != null) ? s : null;
      }
      const same = (!h && !hover) || (h && hover && h.lane === hover.lane &&
        h.idx === hover.idx && h.laneHit === hover.laneHit);
      if (!same) { hover = h; if (hovG) drawHover(V().tf.k); }
    });
  }, true);

  // 拖拽在画布外松开时也要清掉按下记录。用冒泡阶段：必须在 stage 的捕获处理之后
  window.addEventListener('mouseup', () => { down = null; });

  stage.addEventListener('mouseup', e => {
    if (!annotMode || !down) return;
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    down = null;
    if (moved > 4 || e.button !== 0) return;
    if (e.target.closest && e.target.closest('#ann-panel')) return;
    const p = toImage(e.clientX, e.clientY);
    if (p.x < 0 || p.y < 0 || p.x > S() || p.y > S()) return;
    onCanvasClick(e, p);
  }, true);

  function onCanvasClick(e, p) {
    const s = tool === 'del' ? null : snapAt(e.clientX, e.clientY, e.altKey);
    if (tool === 'point') {
      commitOp();
      if (iss) iss.anchor = mkPt(s);             // 已打开问题：移动问题位置
      else openIssue(s);
      refreshForm(); render();
      if (!editing) fixEl.focus();
      return;
    }
    if (tool === 'del') {
      const L = laneAt(e.clientX, e.clientY);
      if (!L) { setSync('这里没有线：删线请直接点在线上', 'bad'); return; }
      if (!iss) openIssue({ x: p.x, y: p.y, lane: L.k, idx: null });
      // 同一条线再点一次 = 取消删除
      const i = iss.ops.findIndex(o => o.kind === 'del' && o.lane === L.k);
      if (i >= 0) iss.ops.splice(i, 1);
      else iss.ops.push({ kind: 'del', lane: L.k, pts: [{ x: p.x, y: p.y, lane: L.k, idx: null }] });
      refreshForm(); render();
      return;
    }
    // link：连续点点，Enter 结束一条
    if (!iss) openIssue(s);
    if (!op) op = { kind: 'link', lane: null, pts: [] };
    op.pts.push(mkPt(s));
    refreshForm(); render();
  }

  const opRow = o => ({
    kind: o.kind, lane: o.kind === 'del' ? o.lane : null,
    pts: o.pts.map(q => ({ x: +q.x.toFixed(1), y: +q.y.toFixed(1),
      lane: q.lane != null ? q.lane : null, idx: q.idx != null ? q.idx : null })),
    // 每个点都要有整幅卫片坐标：判例做 few-shot 时用的是模型那版的旋转/尺寸，
    // 只有 xy_global 这种坐标系在所有版本之间通用。
    xy_global_pts: o.pts.map(q => toGlobal(cand(), q.x, q.y)),
  });

  panel.querySelector('#ann-save').onclick = () => {
    if (!iss) return;
    if (op && op.pts.length === 1) {
      setSync('✗ 正在画的连线只有 1 个点：再点一个点，或按 Backspace 撤销', 'bad');
      return;
    }
    commitOp();
    const type = currentType();
    if (!type) {
      setSync('✗ 选了“其它”类型，请先输入类型名再保存', 'bad');
      customEl.focus();
      return;
    }
    if (!TYPES.some(t => t.k === type) && !CUSTOMS.includes(type)) {
      CUSTOMS.push(type); saveCustoms();
    }
    const cid = curCid();
    const arr = ANNOTS[cid] || (ANNOTS[cid] = []);
    const a0 = iss.anchor;
    const common = {
      kind: 'issue',
      x: +a0.x.toFixed(1), y: +a0.y.toFixed(1),
      xy_global: toGlobal(cand(), a0.x, a0.y),
      in_fov: inFov(cand(), a0.x, a0.y),
      pts: [{ x: +a0.x.toFixed(1), y: +a0.y.toFixed(1),
              lane: a0.lane != null ? a0.lane : null, idx: a0.idx != null ? a0.idx : null }],
      xy_global_pts: [toGlobal(cand(), a0.x, a0.y)],
      lane: null,
      ops: iss.ops.map(opRow),
      type, fix: fixEl.value, note: noteEl.value,
      at: new Date().toISOString(),
    };
    const old = editing && arr.find(x => x.id === editing);
    if (old) {
      for (const k of Object.keys(old)) if (!['id', 'seq', 'cid'].includes(k)) delete old[k];
      Object.assign(old, common);
    } else {
      const seq = Math.max(0, ...arr.map(x => x.seq || 0)) + 1;
      arr.push(Object.assign({ id: uid(), seq, cid }, common));
    }
    closeIssue();
    setTool('point', true);
    persist(); render();
  };
  panel.querySelector('#ann-cancel').onclick = () => { closeIssue(); render(); };

  document.addEventListener('keydown', e => {
    if (!annotMode) return;
    const inField = e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA'
                 || e.target.tagName === 'SELECT';
    if (e.key === 'Escape') {
      if (inField) { e.target.blur(); return; }
      // Esc 先丢掉正在画的连线，再按一次才关掉整个问题
      if (op) op = null; else closeIssue();
      refreshForm(); render();
      e.preventDefault(); return;
    }
    if (inField) return;
    if (e.key === 'q') { setTool('point'); e.preventDefault(); }
    if (e.key === 'w') { setTool('link'); e.preventDefault(); }
    if (e.key === 'e') { setTool('del'); e.preventDefault(); }
    if (e.key === 'Enter' && op) {
      if (op.pts.length >= 2) commitOp();
      else setSync('✗ 连线至少要两个点', 'bad');
      refreshForm(); render(); e.preventDefault();
    }
    if (e.key === 'Backspace' && op && op.pts.length) {
      op.pts.pop();
      if (!op.pts.length) op = null;
      refreshForm(); render(); e.preventDefault();
    }
  });

  /* ---------------- 存储 ---------------- */
  const syncEl = panel.querySelector('#ann-sync');
  function setSync(txt, cls) { syncEl.textContent = txt; syncEl.className = cls || ''; }
  function allRows() { return Object.values(ANNOTS).flat(); }
  function payload() {
    const rows = allRows();
    return { saved_at: new Date().toISOString(), n: rows.length, rows };
  }
  function saveLocal() {
    try { localStorage.setItem(LS, JSON.stringify(ANNOTS)); } catch (e) { /* 内存模式 */ }
  }
  async function push() {
    try {
      const r = await fetch('api/annots', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload()),
      });
      if (!r.ok) throw new Error(r.status);
      const j = await r.json();
      setSync(`✓ 已自动保存 ${j.n} 条到服务器`, 'ok');
    } catch (e) {
      setSync('✗ 没连上服务器，标注只存在本浏览器里；请用"下载备份 JSON"', 'bad');
    }
  }
  function persist() { saveLocal(); push(); }

  /* 打开页面时以服务端文件为准；服务端是空的而本地有，就把本地的推上去 */
  async function pull() {
    try {
      const r = await fetch('api/annots', { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const rows = (await r.json()).rows || [];
      if (rows.length) {
        for (const k of Object.keys(ANNOTS)) delete ANNOTS[k];
        rows.forEach(a => (ANNOTS[a.cid] || (ANNOTS[a.cid] = [])).push(normalize(a)));
        saveLocal();
        setSync(`✓ 已从服务器读到 ${rows.length} 条`, 'ok');
        render();
      } else if (allRows().length) {
        push();
      } else {
        setSync('已连上服务器，保存后自动记录', 'ok');
      }
    } catch (e) {
      setSync('✗ 没连上服务器，标注只存在本浏览器里；请用"下载备份 JSON"', 'bad');
    }
  }

  panel.querySelector('#ann-export').onclick = () => {
    const blob = new Blob([JSON.stringify(payload(), null, 1)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'human_annots.json';
    a.click();
  };

  /* ---------------- 与主脚本协作 ---------------- */
  // 换候选时同步重绘（顺便清掉上一处的悬浮状态）
  const _draw = window.draw;
  window.draw = function () {
    _draw.apply(this, arguments);
    hover = null;
    if (iss && iss.cid !== curCid()) closeIssue();
    render();
  };
  // 缩放后按屏幕像素修正线宽/点径（平移不触发，代价可控）
  window.addEventListener('view:tf', () => {
    const k = V().tf.k;
    if (Math.abs(k - lastK) > 1e-6) { restyle(k); lastK = k; }
  });

  render();
  pull();

  window.ANNOTS = ANNOTS;
})();
