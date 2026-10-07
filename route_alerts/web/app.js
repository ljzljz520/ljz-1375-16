let STATE=null, PICKING=null;
const $=s=>document.querySelector(s);
const api=async (p,opt)=>{const r=await fetch(p,opt);return r.json();};
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const reasonTag=r=>{
  const code=r.code||r;
  const name={BLOCKED_CONSTRUCTION:'施工封闭',BLOCKED_CONTROL:'交通管制',
    CLOSED_BY_RULE:'规则封闭',AGE_TOO_LOW:'年龄不足',ELDERLY_FORBIDDEN:'老人禁行',
    WHEELCHAIR_INACCESSIBLE:'轮椅不可达',OUTSIDE_SHIFT:'非班次时间',
    DIRECTION_FORBIDDEN:'方向不符',PENDING_CONFLICT:'来源矛盾·待核'}[code]||code;
  return `<span class="tag t-blocked">${name}</span>`;
};

async function loadState(){
  STATE=await api('/api/state');
  $('#ver').textContent=`数据版本 #${STATE.version.bump} · 更新 ${STATE.version.updated_at}`;
  drawMap();
}
const nodeMap=()=>Object.fromEntries(STATE.nodes.map(n=>[n.node_id,n]));

function drawMap(extra){
  const svg=$('#map'), N=nodeMap(); svg.innerHTML='';
  const E=window.SVG_NS='http://www.w3.org/2000/svg';
  const el=(t,a)=>{const e=document.createElementNS(E,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
  // 路线规则几何（活动）
  (extra&&extra.rules?extra.rules:STATE.rules).filter(r=>r.status==='active').forEach(r=>{
    const color=r.effect==='block'?'#e05b6b':r.effect==='allow'?'#37b26b':'#5b8def';
    if(r.geom_type==='polygon'){
      svg.appendChild(el('polygon',{points:r.geometry.map(p=>p.join(',')).join(' '),
        fill:color,'fill-opacity':.12,stroke:color,'stroke-width':2,'stroke-dasharray':'6 4'}));
    } else {
      svg.appendChild(el('polyline',{points:r.geometry.map(p=>p.join(',')).join(' '),
        fill:'none',stroke:color,'stroke-width':5,'stroke-opacity':.75}));
    }
  });
  // 线段
  STATE.segments.forEach(s=>{
    const cls={船:'#4db8d4',摆渡车:'#d4a04d',电梯:'#c084f5'}[s.attrs.surface]||'#8090ad';
    const line=el('polyline',{points:s.geometry.map(p=>p.join(',')).join(' '),
      fill:'none',stroke:cls,'stroke-width':4,'stroke-linecap':'round',
      'data-seg':s.seg_key,style:'cursor:pointer'});
    line.addEventListener('click',()=>segInfo(s.seg_key));
    svg.appendChild(line);
  });
  // 高亮/待核
  (extra&&extra.highlights||[]).forEach(h=>{
    const s=STATE.segments.find(x=>x.seg_key===h.seg_key);
    if(!s)return;
    svg.appendChild(el('polyline',{points:s.geometry.map(p=>p.join(',')).join(' '),
      fill:'none',stroke:h.color||'#e8a13a','stroke-width':9,'stroke-opacity':.4}));
  });
  // 节点
  STATE.nodes.forEach(n=>{
    const g=el('g',{style:'cursor:pointer'});
    g.appendChild(el('circle',{cx:n.x,cy:n.y,r:6,fill:'#e8edf7',stroke:'#0b101c','stroke-width':2}));
    const t=el('text',{x:n.x+9,y:n.y-8,fill:'#c8d4ec','font-size':13});
    t.textContent=`${n.node_id} ${n.name}`; g.appendChild(t);
    g.addEventListener('click',()=>{if(PICKING){PICKING.value=n.node_id;
      PICKING=null;svg.classList.remove('pick');renderTab(currentTab);}else nodeInfo(n);});
    svg.appendChild(g);
  });
}
function segInfo(k){
  const s=STATE.segments.find(x=>x.seg_key===k), N=nodeMap();
  const weekly=s.service_weekly?`${s.service_weekly.days.length===7?'每日':s.service_weekly.days.join('/')} ${s.service_weekly.start}-${s.service_weekly.end}`:'全天开放';
  showPanel(`<h2>线段 ${k} ${esc(s.name)}</h2>
    <div class="muted">${s.from} → ${s.to}；表面=${esc(s.attrs.surface||'')}；轮椅=${s.attrs.wheelchair_ok?'可':'否'}；老人=${s.attrs.elderly_ok?'可':(s.attrs.elderly_ok===false?'否':'可')}；儿童${s.attrs.min_age?` ≥${s.attrs.min_age}岁`:'不限'}</div>
    <div class="muted" style="margin-top:6px">班次：${weekly}</div>
    <h2 style="margin-top:12px">命中该线段的提醒（几何求交结果）</h2>
    ${STATE.rules.filter(r=>r.matches.some(m=>m.seg_key===k)).map(r=>{
      const m=r.matches.find(x=>x.seg_key===k);
      return `<div class="item"><b>[${r.status}] ${esc(r.title)}</b>
      <div class="muted">${r.ann_id} · ${r.rule_type} · ${r.effect} · 方向 ${r.direction}
      ${m.point_only?'· 仅节点接触(point-only，不据此封闭)':'· 区间重叠(真实命中)'}</div></div>`;
    }).join('')||'<div class="muted">无命中提醒</div>'}`);
}
function nodeInfo(n){
  showPanel(`<h2>节点 ${n.node_id} ${esc(n.name)}</h2><div class="muted">坐标 (${n.x}, ${n.y})</div>
  <p class="muted">在“出行规划”中如定位被拒，可在此手选起终点。</p>`);
}
function showPanel(h){$('#panel').innerHTML=h;}

/* ---------------- 路线影响 ---------------- */
function impactTab(){
  const opts=()=>STATE.routes.map(r=>`<option value="${r.route_id}|${r.version}">${r.route_id} v${r.version} ${r.current?'(当前)':''} ${esc(r.name)}</option>`).join('');
  showPanel(`<h2>① 查询路线在具体时刻/人群下的影响</h2>
  <label>路线版本</label><select id="im-route">${opts()}</select>
  <div class="row"><div><label>生效时刻</label><input id="im-at" value="2026-10-07T10:00"></div>
  <div><label>收录时刻（撤文迟到视角）</label><input id="im-obs" value="2026-10-07T10:00"></div></div>
  <label>人群条件</label>
  <div class="row"><label class="chk"><input type="checkbox" id="im-wheel">轮椅/无障碍</label>
  <label class="chk"><input type="checkbox" id="im-elder">老人</label></div>
  <label class="chk">儿童年龄 <input type="number" id="im-age" style="width:80px" placeholder="不限"></label>
  <button class="act" onclick="doImpact()">计算影响（时空+方向+班次）</button>
  <div id="im-out" style="margin-top:12px"></div>`);
}
async function doImpact(){
  const [rid,ver]=$('#im-route').value.split('|');
  const q=new URLSearchParams({route_id:rid,version:ver,at:$('#im-at').value,
    observed_at:$('#im-obs').value,wheelchair:$('#im-wheel').checked,
    elderly:$('#im-elder').checked,child_age:$('#im-age').value});
  const r=await api('/api/impact?'+q);
  const stName={open:'可通行（有保证）',blocked:'受阻',pending:'部分待核（不保证通行）'}[r.state];
  const tagCls={open:'t-open',blocked:'t-blocked',pending:'t-pending'}[r.state];
  const hl=[];
  $('#im-out').innerHTML=`<div class="item">${rid} v${r.version} 结论：
    <span class="tag ${tagCls}">${stName}</span></div>` +
    r.edges.map(e=>{
      if(e.state!=='open')hl.push({seg_key:e.seg_key,color:e.state==='pending'?'#c084f5':'#e05b6b'});
      return `<div class="item"><b>${e.seg_key} ${esc(e.name)}</b> ${e.reversed?'<span class="muted">(反方向行驶)</span>':''}
        <span class="tag t-${e.state}">${{open:'可通行',blocked:'受阻',pending:'待核'}[e.state]}</span>
        ${e.reasons.map(x=>`<div style="margin-top:5px">${reasonTag(x)}
          <span class="muted">依据：${esc(x.source||'路网固有属性/班次')}（${x.ann_id||'-'}）</span></div>`).join('')}
        ${e.info.map(x=>`<div class="small" style="color:var(--accent)">ℹ ${esc(x.title)}（${x.ann_id}，信息类不停用）</div>`).join('')}
        ${e.state==='blocked'&&!e.reasons.length?'<div class="muted">班次：'+esc(e.service)+'</div>':''}
        <div class="muted small">位置：${e.seg_key}；时间：${esc($('#im-at').value)}；班次窗口：${esc(e.service)}</div>
      </div>`;
    }).join('');
  drawMap({rules:STATE.rules,highlights:hl});
}

/* ---------------- 规划 ---------------- */
let currentTab='impact';
function planTab(){
  const nodes=STATE.nodes.map(n=>`<option value="${n.node_id}">${n.node_id} ${esc(n.name)}</option>`).join('');
  showPanel(`<h2>② 起终点规划 · 两种策略</h2>
  <div class="row"><div><label>起点（定位被拒可在地图点选）</label>
    <input id="p-start" value="G_S"><button class="ghost small" onclick="pickNode('#p-start')">地图点选</button></div>
  <div><label>终点</label><input id="p-goal" value="G_N"><button class="ghost small" onclick="pickNode('#p-goal')">地图点选</button></div></div>
  <label>出发时间</label><input id="p-at" value="2026-10-07T10:00">
  <div class="row"><label class="chk"><input type="checkbox" id="p-wheel">轮椅/无障碍</label>
  <label class="chk"><input type="checkbox" id="p-elder">老人</label></div>
  <label class="chk">儿童年龄 <input type="number" id="p-age" style="width:80px" placeholder="不限"></label>
  <div class="row">
    <button class="act ghost" onclick="doPlan('filter')">策略A：先求可达路线，再筛人群/约束</button>
    <button class="act" onclick="doPlan('prune')">策略B：约束直接进入搜索</button>
  </div>
  <div class="muted small" style="margin-top:8px">对比指标：候选路径数、边状态评估次数、节点展开数；
  无解时给出原因集合与“关键割”（放松该类约束即恢复通行）。</div>
  <div id="p-out" style="margin-top:10px"></div>`);
}
function pickNode(sel){PICKING=$(sel);$('#map').classList.add('pick');
  showPanel($('#panel').innerHTML+'<div class="tag t-stale">请在右侧地图点选节点…</div>');}
async function doPlan(mode){
  const q=new URLSearchParams({start:$('#p-start').value,goal:$('#p-goal').value,
    at:$('#p-at').value,mode,wheelchair:$('#p-wheel').checked,
    elderly:$('#p-elder').checked,child_age:$('#p-age').value});
  const r=await api('/api/plan?'+q);
  const hl=(r.feasible[0]?pathSegs(r.feasible[0].path_nodes):[]).map(k=>({seg_key:k,color:'#37b26b'}));
  r.no_solution.reasons.forEach(x=>x.segments.forEach(k=>hl.push({seg_key:k,color:'#e05b6b'})));
  let html=`<div class="item"><b>${mode==='filter'?'策略A（先可达后筛选）':'策略B（直接搜索）'}</b><br>
    <span class="muted">候选 ${r.metrics.candidates} · 边评估 ${r.metrics.edge_checks} · 节点展开 ${r.metrics.nodes_expanded}</span></div>`;
  if(r.feasible.length){
    html+=r.feasible.slice(0,3).map(f=>`<div class="item">
      <span class="tag ${f.guaranteed?'t-open':'t-pending'}">${f.guaranteed?'通行保证':'待核·不保证'}</span>
      <b>${f.path_nodes.join(' → ')}</b>
      ${f.pending&&f.pending.length?`<div class="small" style="color:var(--pend)">⚠ 待核：${[...new Set(f.pending.flatMap(p=>p.pending.map(x=>x.seg_key)))].join(',')}（来源相互矛盾，需人工核实）</div>`:''}
    </div>`).join('');
  } else {
    html+='<div class="item"><b>无可行路线。无解原因集合：</b>'+
      r.no_solution.reasons.map(x=>`<div style="margin-top:6px">
        ${reasonTag(x.code)} <span class="${x['class']&&r.no_solution.critical.includes(x['class'])?'crit':''}">${esc(x.reason)}</span>
        ${r.no_solution.critical.includes(x['class'])?'<span class="tag t-stale">关键割</span>':''}
        <div class="muted small">路段：${x.segments.join(', ')}；依据公告：${x.ann_ids.join(', ')||'固有属性/班次'}</div></div>`).join('')+
      `<div class="muted small" style="margin-top:6px">${esc(r.no_solution.note)}</div></div>`;
  }
  if(mode==='filter'&&r.rejected.length){
    html+=`<details class="item"><summary><b>被筛掉的 ${r.rejected.length} 条可达路径及其原因</b></summary>`+
      r.rejected.slice(0,8).map(x=>`<div class="small" style="margin-top:5px">${x.path_nodes.join('→')}<br>`+
        [...new Set(x.reasons.map(z=>z.code))].map(reasonTag).join('')+'</div>').join('')+'</details>';
  }
  $('#p-out').innerHTML=html;
  drawMap({rules:STATE.rules,highlights:hl});
}
function pathSegs(nodes){
  const keys=[];
  for(let i=0;i<nodes.length-1;i++){
    const s=STATE.segments.find(s=>(s.from===nodes[i]&&s.to===nodes[i+1])||(s.to===nodes[i]&&s.from===nodes[i+1]));
    if(s)keys.push(s.seg_key);
  }
  return keys;
}

/* ---------------- 管理 ---------------- */
function manageTab(){
  showPanel(`<h2>③ 公告/规则录入 · 矛盾标记 · 撤销/修订/改线</h2>
  <div class="muted small">规则保存时立即与全部线段做几何求交；同名或 bbox 重合不命中。</div>
  <label>公告/规则 JSON（可修改后录入；ANN8 是“同名陷阱”示范）</label>
  <textarea id="m-json"></textarea>
  <div class="row">
    <button class="act" onclick="doAnnounce()">录入/修订公告（自动求交）</button>
    <button class="act ghost" onclick="doRevoke()">撤销（撤文迟到）</button>
  </div>
  <div class="row">
    <button class="act ghost" onclick="loadSample()">填入修订样例(ANN1v2 改为夜间)</button>
    <button class="act ghost" onclick="resetAll()">重置演示数据</button>
  </div>
  <div id="m-extra" style="margin-top:10px"></div>
  <h2 style="margin-top:14px">当前公告与待核矛盾</h2>
  <div id="m-list"></div>`);
  renderManageList();
  api('/api/missing?at=2026-10-07T10:00').then(m=>{
    $('#m-extra').innerHTML='<div class="item"><b>缺信息范围</b>'+
      m.items.map(x=>`<div class="small"><span class="tag t-stale">${x.severity}</span>${esc(x.detail)}</div>`).join('')+
      `<div class="muted small">${esc(m.coverage_note)}</div></div>`;
  });
}
function renderManageList(){
  const anns=Object.fromEntries(STATE.announcements.map(a=>[a.ann_id,a]));
  let html=STATE.rules.map(r=>{
    const a=anns[r.ann_id];
    return `<div class="item"><b>[v${r.version}/${r.status}] ${esc(r.title)}</b>
      <div class="muted small">${r.ann_id} 来源=${esc(a.source)}(${a.source_level}) · 收录 ${r.observed_at}
      ${a.status==='revoked'?` · 已撤销@${a.revoked_at}，撤文收录@${a.revoke_observed_at||'?'}`:''}
      ${a.revision_of?` · 修订自 ${a.revision_of}`:''}</div>
      <div class="small">命中线段：${r.matches.length?r.matches.map(m=>`<span class="tag ${m.point_only?'t-info':'t-blocked'}">${m.seg_key}${m.point_only?'(点接触)':''}</span>`).join(''):'<span class="muted">无（几何不相交）</span>'}</div>
      <button class="ghost small" onclick="ackRule(${r.id})">查看确认 v${r.version}</button>
      <span id="ack-${r.id}"></span></div>`;
  }).join('');
  // 矛盾检测（同段 block vs allow 同时活动）
  const confs=[];
  STATE.segments.forEach(s=>{
    const hit=STATE.rules.filter(r=>r.status==='active'&&r.matches.some(m=>m.seg_key===s.seg_key));
    const bl=hit.filter(r=>r.effect==='block'),al=hit.filter(r=>r.effect==='allow');
    if(bl.length&&al.length)confs.push(`<div class="item"><span class="tag t-pending">待核</span>
      <b>${s.seg_key} ${esc(s.name)}</b>：${bl.map(x=>x.ann_id+'「'+esc(x.title)+'」').join('，')}
      与 ${al.map(x=>x.ann_id+'「'+esc(x.title)+'」').join('，')} 相互矛盾。
      <div class="muted small">系统不自行拼接成通行保证；影响结果标记为“待核”。</div></div>`);
  });
  $('#m-list').innerHTML=(confs.length?'<b style="color:var(--pend)">⚠ 来源矛盾（待核）</b>'+confs.join(''):'<div class="muted small">当前无生效矛盾对</div>')+html;
}
async function ackRule(id){
  const r=await fetch('/api/ack',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({user:'u1',rule_id:id,at:'2026-10-07T10:00'})}).then(r=>r.json());
  $('#ack-'+id).innerHTML=`<span class="tag ${r.has_new_version?'t-stale':'t-open'}">已确认v${r.ack_version}${r.has_new_version?' · 已有新版本，旧确认不隐藏变化':'（=当前版本）'}</span>`;
}
async function doAnnounce(){
  const body=JSON.parse($('#m-json').value);
  const r=await fetch('/api/announce',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  if(r.error)return alert(r.error);
  await loadState();renderManageList();
  alert(`已录入 ${r.rule.ann_id} v${r.rule.version}；几何求交命中：${r.rule.matches.map(m=>m.seg_key).join(',')||'无（不与任何线段相交）'}`);
}
async function doRevoke(){
  const id=prompt('公告ID（如 ANN4）','ANN4');
  const revoked_at=prompt('撤销生效时间','2026-10-07T08:00');
  const observed_at=prompt('撤文实际收录时间（晚于生效=迟到撤销）','2026-10-07T09:30');
  await fetch('/api/revoke',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ann_id:id,revoked_at,observed_at})}).then(r=>r.json());
  await loadState();renderManageList();
}
async function resetAll(){
  await fetch('/api/reset',{method:'POST'});await loadState();renderManageList();
}
function loadSample(){
  $('#m-json').value=JSON.stringify({
    ann_id:'ANN1',source:'西山市政',source_level:'市级',observed_at:'2026-10-07T09:00',
    revision_of:'ANN1',title:'西山崖电梯施工修订（仅夜间）',
    body:'据现场反馈改为仅夜间施工，白天恢复通行。',
    rule:{rule_key:'R_CLIFF',effect:'block',rule_type:'construction',
      title:'崖电梯夜间施工(修订)',geom_type:'polyline',
      geometry:STATE.rules.find(r=>r.rule_key==='R_CLIFF').geometry,direction:'B',
      windows:[{start:'2026-10-07T20:00',end:'2026-10-08T06:00'}]}},null,2);
}
function sampleInitial(){
  const s=STATE.rules.find(r=>r.ann_id==='ANN5');
  loadSample();
}

/* ---------------- 离线 & 打印 ---------------- */
function offlineTab(){
  showPanel(`<h2>④ 旧离线计划重连 · 打印（新鲜度+缺信息）</h2>
  <div class="row"><div><label>起点</label><input id="o-start" value="G_S"></div>
  <div><label>终点</label><input id="o-goal" value="G_N"></div></div>
  <label>保存时刻（离线查询时间）</label><input id="o-saveat" value="2026-10-06T20:00">
  <label class="chk"><input type="checkbox" id="o-wheel" checked>轮椅/无障碍</label>
  <div class="row">
    <button class="act" onclick="saveOffline()">① 保存离线计划</button>
    <button class="act ghost" onclick="reconnect()">② 重连比对并按当前数据重算</button>
  </div>
  <label>重连时刻</label><input id="o-now" value="2026-10-08T10:00">
  <button class="act ghost" onclick="doPrint()">打印计划（含新鲜度与缺信息范围）</button>
  <pre class="plan" id="o-out">尚未生成。</pre>`);
}
async function saveOffline(){
  const r=await fetch('/api/offline',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({start:$('#o-start').value,goal:$('#o-goal').value,
      at:$('#o-saveat').value,person:{wheelchair:$('#o-wheel').checked},user:'u1'})}).then(r=>r.json());
  window.PLAN_ID=r.plan_id;$('#o-out').textContent='已保存离线计划：'+r.plan_id+
    '\n快照规则：'+r.payload.rules_snapshot.map(x=>x.ann_id).join(', ');
}
async function reconnect(){
  if(!window.PLAN_ID)return alert('请先保存离线计划');
  const r=await fetch('/api/reconnect',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({plan_id:PLAN_ID,at:$('#o-now').value,
      person:{wheelchair:$('#o-wheel').checked}})}).then(r=>r.json());
  if(r.error)return alert(r.error);
  await loadState();
  $('#o-out').textContent=
    `重连：${r.reconnect_at}\n⚠ ${r.warning}\n\n`+
    `新发布/修订：${r.new_or_changed.map(x=>x.ann_id+' '+x.title+'（'+x.reason+'）').join('；')||'无'}\n`+
    `撤销（含迟到）：${r.revoked.map(x=>x.ann_id+(x.late?' [撤文迟到]':'')).join('；')||'无'}\n`+
    `路线改线：${r.reroutes.map(x=>x.route_id+': v'+x.old_version+'→v'+x.new_version).join('；')||'无'}\n\n`+
    `当前重算：${r.current_plan.feasible.length?r.current_plan.feasible[0].path_nodes.join(' → ')+(r.current_plan.feasible[0].guaranteed?'（通行保证）':'（待核·不保证）'):'无可行路线'}`;
}
async function doPrint(){
  const q=new URLSearchParams({start:$('#o-start').value,goal:$('#o-goal').value,
    at:$('#o-now').value,wheelchair:$('#o-wheel').checked});
  const t=await (await fetch('/api/print?'+q+'&format=text')).text();
  $('#o-out').textContent=t;
  const w=window.open('');w.document.write('<pre style="font:14px monospace;white-space:pre-wrap">'+esc(t)+'</pre>');
  w.document.close();w.print();
}

/* ---------------- tab 切换 ---------------- */
const TABS={impact:impactTab,plan:planTab,manage:manageTab,offline:offlineTab};
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');currentTab=b.dataset.tab;TABS[currentTab]();drawMap();
}));
function renderTab(){TABS[currentTab]();}
(async function init(){
  await loadState();
  impactTab();
})();
