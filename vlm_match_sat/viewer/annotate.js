/* 人工标注模块（v1 用）
 * ------------------------------------------------------------------
 * 目标：让使用者在查看器里直接指出"这里错了 / 这里对"，并写下问题类型和改法。
 * 产出两样东西：
 *   1. few-shot 示例 —— 直接喂进 prompt，让模型看到真实判例
 *   2. 真值（ground truth）—— 用来算 precision / recall
 *
 * 坐标：与查看器一致，2048 裁剪图像素坐标系。
 * 存储：localStorage（离线可用）+ POST api/annots（服务模式下落盘 annotations.json）
 */
(function () {
  'use strict';

  const TYPES = [
    { k: 'missing_lane', label: '该续未续', color: '#ff4d4d' },
    { k: 'wrong_break', label: '连了但不该连', color: '#ff9f2e' },
    { k: 'wrong_branch', label: '分合流错', color: '#c14dff' },
    { k: 'wrong_count', label: '车道数不符', color: '#3d9bff' },
    { k: 'ok', label: '这里是对的', color: '#3fbf6a' },
  ];
  const LS = 'lane-annots-v1';
  let ANNOTS = {};
  try { ANNOTS = JSON.parse(localStorage.getItem(LS) || '{}') || {}; } catch (e) { ANNOTS = {}; }

  let annotMode = false;
  let draft = null;      // {x, y}
  let editing = null;    // 正在编辑的标注 id

  const S = () => window.DATA.size;
  const uid = () => 'a' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);

  /* ---------------- 样式 ---------------- */
  const css = `
  #ann-btn{padding:4px 9px;border:1px solid #2b3040;background:#1b2029;color:#e6e8ee;
    border-radius:5px;font-size:11.5px;cursor:pointer;transition:.1s;}
  #ann-btn:hover{background:#252c3a;border-color:#4a5570;}
  #ann-btn.on{background:#3d2f14;border-color:#ffb020;color:#ffd54a;}
  #ann-panel{position:absolute;left:13px;top:52px;width:268px;background:#14181fee;
    backdrop-filter:blur(10px);border:1px solid #2b3040;border-radius:8px;padding:10px 12px;
    z-index:6;font-size:11.5px;display:none;max-height:calc(100% - 92px);overflow-y:auto;}
  #ann-panel.show{display:block;}
  #ann-panel h4{margin:0 0 8px;font-size:11px;letter-spacing:.6px;color:#8b93a6;
    text-transform:uppercase;font-weight:600;}
  #ann-panel .hint{color:#8b93a6;font-size:10.5px;line-height:1.5;margin-bottom:8px;}
  #ann-form label{display:block;color:#9aa4ba;font-size:10.5px;margin:7px 0 3px;}
  #ann-form select,#ann-form textarea,#ann-form input{width:100%;background:#0f1319;
    border:1px solid #2b3040;border-radius:4px;color:#e6e8ee;font-size:11.5px;
    padding:5px 7px;outline:none;font-family:inherit;resize:vertical;}
  #ann-form select:focus,#ann-form textarea:focus{border-color:#4a5570;}
  #ann-form textarea{min-height:52px;line-height:1.5;}
  #ann-actions{display:flex;gap:6px;margin-top:9px;}
  #ann-actions button{flex:1;padding:5px;border-radius:4px;border:1px solid #3a4257;
    background:#1e2430;color:#c4cbdb;cursor:pointer;font-size:11.5px;}
  #ann-actions button.primary{background:#1c3d29;border-color:#3fbf6a;color:#8fdcac;}
  #ann-actions button:hover{filter:brightness(1.2);}
  .ann-item{display:flex;gap:6px;align-items:flex-start;padding:5px 6px;border-radius:4px;
    background:#1b202a;margin-bottom:4px;cursor:pointer;}
  .ann-item:hover{background:#222836;}
  .ann-item .sw{width:3px;border-radius:2px;flex:none;align-self:stretch;}
  .ann-item .t{flex:1;min-width:0;}
  .ann-item .tt{font-weight:600;font-size:11px;}
  .ann-item .tx{color:#8b93a6;font-size:10px;line-height:1.4;
    overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
  .ann-item .del{color:#5c6478;font-size:13px;padding:0 3px;}
  .ann-item .del:hover{color:#ff5d7a;}
  #ann-svg{position:absolute;left:0;top:0;width:2048px;height:2048px;pointer-events:none;}
  @keyframes annpulse{0%,100%{opacity:.45;r:16}50%{opacity:1;r:22}}
  .ann-draft{animation:annpulse 1.2s ease-in-out infinite;}
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
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.id = 'ann-svg';
  svg.setAttribute('viewBox', `0 0 ${S()} ${S()}`);
  world.appendChild(svg);

  const panel = document.createElement('div');
  panel.id = 'ann-panel';
  panel.innerHTML = `
    <h4>人工标注</h4>
    <div class="hint">开启后在图上点一下，标出问题的<b>大致位置</b>，
      再选类型、写下该怎么改。标完点"导出"。</div>
    <div id="ann-form" style="display:none">
      <div style="color:#ffd54a;font-size:10.5px">位置 x=<span id="ann-xy"></span></div>
      <label>问题类型</label>
      <select id="ann-type">
        ${TYPES.map(t => `<option value="${t.k}">${t.label}（${t.k}）</option>`).join('')}
      </select>
      <label>怎么改（自由描述，越具体越好）</label>
      <textarea id="ann-fix" placeholder="例：这条线应向南延长约 30 m，接到鼻端再并入主线"></textarea>
      <label>补充说明（可选）</label>
      <input id="ann-note" placeholder="例：影像上车行道仍在延续">
      <div id="ann-actions">
        <button class="primary" id="ann-save">保存</button>
        <button id="ann-cancel">取消</button>
      </div>
    </div>
    <div id="ann-list"></div>
    <div id="ann-actions" style="margin-top:8px">
      <button id="ann-export">导出标注（few-shot + 真值）</button>
    </div>
    <div class="hint" id="ann-count" style="margin-top:6px"></div>
  `;
  stage.appendChild(panel);

  const form = panel.querySelector('#ann-form');
  const typeSel = panel.querySelector('#ann-type');
  const fixEl = panel.querySelector('#ann-fix');
  const noteEl = panel.querySelector('#ann-note');
  const list = panel.querySelector('#ann-list');

  /* ---------------- 坐标换算 ---------------- */
  function toImage(clientX, clientY) {
    const r = stage.getBoundingClientRect();
    // 与主脚本相同的变换：translate(tf.x, tf.y) scale(tf.k)
    return {
      x: (clientX - r.left - window.tf.x) / window.tf.k,
      y: (clientY - r.top - window.tf.y) / window.tf.k,
    };
  }
  function toScreen(x, y) {
    const r = stage.getBoundingClientRect();
    return { x: r.left + window.tf.x + x * window.tf.k,
             y: r.top + window.tf.y + y * window.tf.k };
  }

  /* ---------------- 绘制标注 ---------------- */
  function curCid() { return window.DATA.cands[window.cur].cid; }
  function listFor(cid) { return ANNOTS[cid] || []; }

  function render() {
    const cid = curCid();
    svg.innerHTML = '';
    listFor(cid).forEach(a => {
      const t = TYPES.find(x => x.k === a.type) || TYPES[0];
      const on = editing === a.id;
      const g = document.createElementNS(svg.namespaceURI, 'g');
      [['circle', { cx: a.x, cy: a.y, r: on ? 20 : 15, fill: 'none',
                    stroke: t.color, 'stroke-width': on ? 3.5 : 2.6 }],
       ['line', { x1: a.x - 22, y1: a.y, x2: a.x + 22, y2: a.y, stroke: t.color,
                  'stroke-width': 1.5, opacity: .8 }],
       ['line', { x1: a.x, y1: a.y - 22, x2: a.x, y2: a.y + 22, stroke: t.color,
                  'stroke-width': 1.5, opacity: .8 }],
      ].forEach(([tag, at]) => {
        const e = document.createElementNS(svg.namespaceURI, tag);
        Object.entries(at).forEach(([k, v]) => e.setAttribute(k, v));
        g.appendChild(e);
      });
      const tx = document.createElementNS(svg.namespaceURI, 'text');
      tx.setAttribute('x', a.x + 19); tx.setAttribute('y', a.y - 17);
      tx.setAttribute('fill', t.color); tx.setAttribute('font-size', '13');
      tx.setAttribute('font-weight', '600'); tx.setAttribute('stroke', '#05070b');
      tx.setAttribute('stroke-width', '3.5'); tx.setAttribute('paint-order', 'stroke');
      tx.textContent = `#${a.seq}`;
      g.appendChild(tx);
      svg.appendChild(g);
    });

    if (draft) {
      const e = document.createElementNS(svg.namespaceURI, 'circle');
      e.setAttribute('cx', draft.x); e.setAttribute('cy', draft.y);
      e.setAttribute('r', 18); e.setAttribute('fill', 'none');
      e.setAttribute('stroke', '#ffd54a'); e.setAttribute('stroke-width', 2.5);
      e.setAttribute('class', 'ann-draft');
      svg.appendChild(e);
    }
    renderList();
  }

  function renderList() {
    const cid = curCid();
    const arr = listFor(cid);
    list.innerHTML = '';
    if (!arr.length) {
      list.innerHTML = '<div style="color:#5c6478;font-size:10.5px;padding:4px 0">这一处还没有标注</div>';
    }
    arr.forEach(a => {
      const t = TYPES.find(x => x.k === a.type) || TYPES[0];
      const d = document.createElement('div');
      d.className = 'ann-item';
      d.innerHTML = `<div class="sw" style="background:${t.color}"></div>
        <div class="t"><div class="tt" style="color:${t.color}">#${a.seq} ${t.label}</div>
        <div class="tx">${(a.fix || '').replace(/[<>&]/g, '')}</div></div>
        <div class="del" title="删除">×</div>`;
      d.onclick = (ev) => {
        if (ev.target.classList.contains('del')) {
          ANNOTS[cid] = arr.filter(x => x.id !== a.id);
          persist(); render(); return;
        }
        editing = a.id;
        form.style.display = 'block';
        draft = { x: a.x, y: a.y };
        typeSel.value = a.type; fixEl.value = a.fix || ''; noteEl.value = a.note || '';
        panel.querySelector('#ann-xy').textContent = `${a.x.toFixed(0)}, ${a.y.toFixed(0)}`;
        render();
      };
      list.appendChild(d);
    });
    const total = Object.values(ANNOTS).reduce((s, v) => s + v.length, 0);
    panel.querySelector('#ann-count').textContent =
      `本处 ${arr.length} 条 · 全局 ${total} 条`;
  }

  /* ---------------- 交互 ---------------- */
  btn.onclick = () => {
    annotMode = !annotMode;
    btn.classList.toggle('on', annotMode);
    btn.textContent = annotMode ? '✎ 标注中…' : '✎ 标注模式';
    panel.classList.toggle('show', annotMode);
    stage.style.cursor = annotMode ? 'crosshair' : '';
    if (!annotMode) { draft = null; editing = null; form.style.display = 'none'; render(); }
  };

  // 与拖拽共存：位移小于 4px 才算点击
  let down = null;
  stage.addEventListener('mousedown', e => { down = { x: e.clientX, y: e.clientY }; }, true);
  stage.addEventListener('mouseup', e => {
    if (!annotMode || !down) return;
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    down = null;
    if (moved > 4 || e.button !== 0) return;
    if (e.target.closest && e.target.closest('#ann-panel')) return;
    const p = toImage(e.clientX, e.clientY);
    if (p.x < 0 || p.y < 0 || p.x > S() || p.y > S()) return;
    draft = p; editing = null;
    form.style.display = 'block';
    panel.querySelector('#ann-xy').textContent = `${p.x.toFixed(0)}, ${p.y.toFixed(0)}`;
    fixEl.value = ''; noteEl.value = '';
    typeSel.value = 'missing_lane';
    fixEl.focus();
    render();
  }, true);

  panel.querySelector('#ann-save').onclick = () => {
    if (!draft) return;
    const cid = curCid();
    const arr = ANNOTS[cid] || (ANNOTS[cid] = []);
    if (editing) {
      const a = arr.find(x => x.id === editing);
      if (a) { a.type = typeSel.value; a.fix = fixEl.value; a.note = noteEl.value; }
    } else {
      const seq = Math.max(0, ...arr.map(x => x.seq || 0)) + 1;
      arr.push({ id: uid(), seq, x: +draft.x.toFixed(1), y: +draft.y.toFixed(1),
                 type: typeSel.value, fix: fixEl.value, note: noteEl.value,
                 cid, at: new Date().toISOString() });
    }
    draft = null; editing = null; form.style.display = 'none';
    persist(); render();
  };
  panel.querySelector('#ann-cancel').onclick = () => {
    draft = null; editing = null; form.style.display = 'none'; render();
  };
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && annotMode) {
      draft = null; editing = null; form.style.display = 'none'; render();
    }
  });

  function persist() {
    try { localStorage.setItem(LS, JSON.stringify(ANNOTS)); } catch (e) { /* 内存模式 */ }
  }

  /* ---------------- 导出 ---------------- */
  async function exportAnnots() {
    const rows = [];
    for (const [cid, arr] of Object.entries(ANNOTS)) {
      for (const a of arr) rows.push(a);
    }
    const payload = { saved_at: new Date().toISOString(), n: rows.length,
                      annots: ANNOTS, rows };
    try {
      const r = await fetch('api/annots', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        const j = await r.json().catch(() => ({}));
        alert(`已保存 ${j.n != null ? j.n : rows.length} 条到 annotations.json`);
        return;
      }
    } catch (e) { /* 退回下载 */ }
    const blob = new Blob([JSON.stringify(payload, null, 1)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'annotations.json';
    a.click();
  }
  panel.querySelector('#ann-export').onclick = exportAnnots;

  /* ---------------- 接管 draw：换候选时同步重绘 ---------------- */
  const _draw = window.draw;
  window.draw = function () { _draw.apply(this, arguments); render(); };
  window.addEventListener('load', () => setTimeout(render, 50));
  setTimeout(render, 60);

  window.ANNOTS = ANNOTS;
})();
