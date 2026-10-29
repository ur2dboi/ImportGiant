(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const state = {context:null, stage:null, file:null, operations:[], busy:false};
  const esc = (x) => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (n) => Number(n || 0).toLocaleString();
  const setNotice = (text, error=false) => { const a=$('#alert'); a.textContent=text; a.classList.toggle('error', error); a.hidden=!text; };
  async function api(path, options={}) {
    const config = {method:options.method || 'GET', headers:{Accept:'application/json'}};
    if (options.body instanceof FormData) config.body=options.body;
    else if (options.body !== undefined) {config.headers['Content-Type']='application/json'; config.body=JSON.stringify(options.body);}
    // Get a fresh, short-lived Shopify session token for every API request.
    if (!window.shopify || typeof window.shopify.idToken !== 'function') {
      throw new Error('Open ImportGiant inside Shopify Admin to authenticate.');
    }
    config.headers.Authorization = `Bearer ${await window.shopify.idToken()}`;
    const response = await fetch(path, config);
    let data={}; try {data=await response.json();} catch (_) {}
    if (!response.ok) throw new Error(data.error || `Request failed (HTTP ${response.status}).`);
    return data;
  }
  function setBusy(button, busy, text) {
    if (!button) return;
    if (busy) {button.dataset.label=button.innerHTML; button.disabled=true; button.textContent=text || 'Working…';}
    else {button.innerHTML=button.dataset.label || button.innerHTML; button.disabled=false; delete button.dataset.label;}
  }
  function statusLabel(status) {
    return ({queued:'Queued',running:'In progress',completed:'Completed',completed_with_warnings:'Review needed',failed:'Failed',interrupted:'Interrupted'})[status] || status;
  }
  function renderActivity() {
    const container=$('#activity-list');
    if (!state.operations.length) {container.innerHTML='<div class="empty-state"><span>↗</span><strong>Your journey starts here.</strong><p>When you start a migration, the details will appear here.</p></div>'; return;}
    container.innerHTML=state.operations.map(op => {
      const isExport=op.kind==='export', done=op.status==='completed', failed=['failed','interrupted','completed_with_warnings'].includes(op.status);
      const counts=op.stats || {}, detail=[];
      if (isExport && counts.products !== undefined) detail.push(`${fmt(counts.products)} products`, `${fmt(counts.variants)} variants`, `${fmt(counts.images)} images`, `${fmt(counts.product_metafields)} metafields`);
      if (!isExport && counts.created !== undefined) detail.push(`${fmt(counts.created)} created`, `${fmt(counts.skipped_existing)} skipped`, `${fmt(counts.metafields_unresolved)} metafields to review`);
      const message=[op.message || '', detail.join(' · ')].filter(Boolean).join(' — ');
      let actions='';
      if (isExport && done && op.download_url) actions+=`<button class="small-btn" data-action="download" data-id="${esc(op.id)}" data-kind="package">Download ZIP</button><button class="small-btn" data-action="review" data-id="${esc(op.id)}">Review package</button>`;
      if (!isExport && op.report_url) actions+=`<button class="small-btn" data-action="download" data-id="${esc(op.id)}" data-kind="report">Download report</button>`;
      return `<div class="activity-item"><span class="activity-icon">${isExport?'↗':'↥'}</span><div class="activity-info"><strong>${isExport?'Product package':'Destination import'}</strong><p>${esc(message)}</p></div><span class="activity-status ${failed?'fail':''}">${esc(statusLabel(op.status))}</span><div class="activity-actions">${actions}</div></div>`;
    }).join('');
  }
  async function refreshActivity(silent=false) {
    if (!state.context) return;
    try {const result=await api('/api/migration/operations');state.operations=result.operations || [];renderActivity();}
    catch(e) {if (!silent) setNotice(e.message,true);}
  }
  function renderStage(stage) {
    state.stage=stage; const counts=stage.counts || {}, source=stage.source || {};
    $('#review-name').textContent=stage.filename || 'Product package';
    $('#review-source').textContent=[source.name, source.domain].filter(Boolean).join(' · ') || 'Source store';
    $('#counts').innerHTML=[['Products',counts.products],['Variants',counts.variants],['Images',counts.images],['Product metafields',counts.product_metafields]].map(([name,count])=>`<div><strong>${fmt(count)}</strong><span>${esc(name)}</span></div>`).join('');
    const notes=['ZIP structure and embedded image checksums checked. Packages are not signed; checks do not authenticate their source.',`${fmt(counts.other_media)} video or 3D items are not transferred. Inventory quantities and metafield definitions are not restored.`];
    if (stage.unsupported_count) notes.push(`${fmt(stage.unsupported_count)} products need a separate migration workflow and will be skipped.`);
    if (stage.risky_metafield_count) notes.push(`${fmt(stage.risky_metafield_count)} metafields have protected namespaces or store-specific references and may need manual review.`);
    $('#review-notes').innerHTML=notes.map(x=>`<p>• ${esc(x)}</p>`).join('');
    const isDest=state.context?.role==='destination';
    $('#confirm-area').hidden=!isDest;
    $('#confirm-domain').value='';$('#confirm-ack').checked=false;$('#import-btn').disabled=true;
    if (isDest) $('#confirm-domain').placeholder=state.context.shop;
    $('#review-section').hidden=false;
    $('#review-section').scrollIntoView({behavior:'smooth',block:'start'});
  }
  function updateConfirm() {
    const domain=$('#confirm-domain').value.trim().toLowerCase();
    $('#import-btn').disabled=!state.stage || !state.context || domain!==state.context.shop.toLowerCase() || !$('#confirm-ack').checked;
  }
  async function download(opid,kind) {
    const result=await api('/api/download-ticket',{method:'POST',body:{operation_id:opid,kind}});
    // One-use ticket is sent in a POST body, never in a URL. Native file
    // streaming avoids buffering large ZIPs in browser memory.
    // A hidden target avoids replacing the embedded App Home document.
    // Keep it alive while the browser streams the file.
    const frame=document.createElement('iframe');frame.name=`ig-download-${Date.now()}-${Math.floor(Math.random()*1e6)}`;frame.hidden=true;frame.title='File download';
    const form=document.createElement('form');form.method='POST';form.action='/download';form.target=frame.name;form.style.display='none';
    const input=document.createElement('input');input.type='hidden';input.name='ticket';input.value=result.ticket;form.appendChild(input);
    document.body.append(frame,form);form.submit();setTimeout(()=>form.remove(),1000);
  }
  async function init() {
    try {
      state.context=await api('/api/context');
      const source=state.context.role==='source';
      $('#connection-pill').innerHTML='<span class="dot"></span> Connected';
      $('#role-badge').textContent=source?'SOURCE · READ ONLY':'DESTINATION · IMPORT';
      $('#workspace-title').textContent=source?'Your source workspace':'Your destination workspace';
      $('#workspace-subtitle').textContent=source?'Create a package without changing this store.':'Review your package before creating draft products.';
      document.querySelectorAll('.shop-name').forEach(x=>x.textContent=state.context.shop);
      $('#source-panel').hidden=!source;$('#destination-panel').hidden=source;$('#workspace').hidden=false;
      await refreshActivity();
    } catch(e) {
      $('#connection-pill').textContent='Not connected';
      $('#workspace-title').textContent='Open this app in Shopify';
      $('#workspace-subtitle').textContent='A verified Shopify Admin session is required.';
      setNotice(e.message,true);
    }
  }
  $('#export-btn').addEventListener('click',async e=>{
    const btn=e.currentTarget;setNotice('');setBusy(btn,true,'Starting export…');
    try {await api('/api/migration/export',{method:'POST',body:{mode:'shopify'}});await refreshActivity();setNotice('Export started. Keep the app running while the package is prepared.');}
    catch(err){setNotice(err.message,true);} finally{setBusy(btn,false);}
  });
  $('#package-file').addEventListener('change',e=>{
    state.file=e.target.files?.[0] || null;$('#file-label').textContent=state.file?.name || 'Choose a product ZIP';$('#review-btn').disabled=!state.file;
  });
  $('#review-btn').addEventListener('click',async e=>{
    const btn=e.currentTarget;if(!state.file)return;
    if(state.file.size>(state.context?.max_upload || 0)){setNotice('This file exceeds the configured package upload limit.',true);return;}
    setNotice('');setBusy(btn,true,'Uploading & verifying…');
    try {const form=new FormData();form.append('file',state.file);const data=await api('/api/migration/stage',{method:'POST',body:form});renderStage(data.stage);setNotice('Package verified. Review the details below.');}
    catch(err){setNotice(err.message,true);} finally{setBusy(btn,false);}
  });
  $('#confirm-domain').addEventListener('input',updateConfirm);$('#confirm-ack').addEventListener('change',updateConfirm);
  $('#import-btn').addEventListener('click',async e=>{
    const btn=e.currentTarget;updateConfirm();if(btn.disabled)return;setNotice('');setBusy(btn,true,'Starting import…');
    try {await api('/api/migration/import',{method:'POST',body:{stage_id:state.stage.stage_id,confirm_domain:$('#confirm-domain').value.trim(),acknowledge:true,force_draft:true}});await refreshActivity();setNotice('Import started. New products are created as drafts.');$('#confirm-ack').checked=false;}
    catch(err){setNotice(err.message,true);} finally{setBusy(btn,false);updateConfirm();}
  });
  $('#refresh-btn').addEventListener('click',()=>refreshActivity());
  $('#activity-list').addEventListener('click',async e=>{
    const button=e.target.closest('button[data-action]');if(!button)return;
    const action=button.dataset.action,id=button.dataset.id;setNotice('');setBusy(button,true,'Working…');
    try {
      if(action==='review'){const data=await api('/api/migration/stage',{method:'POST',body:{operation_id:id}});renderStage(data.stage);}
      else if(action==='download') await download(id,button.dataset.kind);
    } catch(err){setNotice(err.message,true);} finally{setBusy(button,false);}
  });
  setInterval(()=>{if(state.context && state.operations.some(x=>['queued','running'].includes(x.status))) refreshActivity(true);},3000);
  init();
})();
