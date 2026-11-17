'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf = '', current = null, dirty = false, busy = false, pollGeneration = 0;
  const groups = {
    'repository-fields': [['repo','Repository URL','url','https://gitlab.com/group/project.git',true],['branch','Branch','text','staging'],['environment','Environment','select']],
    'odoo-fields': [['odoo_url','Odoo URL','url','https://your-project.odoo.com'],['odoo_db','Database name','text','Your database'],['odoo_login','Odoo login','text','Your Odoo username',true]],
    'ssh-fields': [['odoosh_ssh_host','SSH host','text','project-build.dev.odoo.com',true],['odoosh_ssh_user','SSH username','text','Build number'],['odoosh_ssh_port','SSH port','number','22']],
    'credential-fields': [['token','Repository access token','password'],['odoo_password','Odoo password','password'],['odoo_api_key','Odoo API key','password']],
    'advanced-fields': [['path','Local checkout path','text','/var/lib/odoo-mcp/repos/client',true],['odoosh_db_name','SSH database name','text'],['odoosh_ssh_key_path','SSH key path on Oracle','text'],['odoosh_odoo_path','Odoo source path','text','/home/odoo/src/odoo'],['odoosh_user_addons_path','Custom addons path','text','/home/odoo/src/user'],['odoo_verify_ssl','Verify Odoo TLS certificate','checkbox']]
  };
  const fields = [];
  for (const [group, definitions] of Object.entries(groups)) {
    for (const [name,label,type,placeholder,full] of definitions) {
      fields.push({name,type});
      const wrapper = document.createElement('div'); wrapper.className = 'field' + (full ? ' full' : '');
      const text = document.createElement('label'); text.htmlFor = name; text.textContent = label;
      let input;
      if (type === 'select') {
        input = document.createElement('select');
        for (const [value,title] of [['','Select environment'],['test','Test'],['development','Development'],['staging','Staging'],['production','Production']]) {
          const option = document.createElement('option'); option.value = value; option.textContent = title; input.append(option);
        }
      } else { input = document.createElement('input'); input.type = type; }
      input.id = name; input.name = name; input.autocomplete = 'off';
      if (placeholder) input.placeholder = placeholder;
      if (type === 'number') { input.min = '1'; input.max = '65535'; }
      if (type === 'password') input.autocomplete = 'new-password';
      if (['repo','branch'].includes(name)) input.required = true;
      if (type === 'checkbox') { text.className = 'checkbox-label'; text.prepend(input); wrapper.append(text); }
      else wrapper.append(text,input);
      $(group).append(wrapper);
    }
  }
  function message(text) { $('message').textContent = text || ''; $('message').hidden = !text; }
  function markDirty(value) { dirty = value; $('dirty').hidden = !value; }
  function setBusy(value) {
    busy = value;
    for (const id of ['save-validate','save-only','client-select']) $(id).disabled = value;
    for (const field of fields) $(field.name).disabled = value;
  }
  function showLogin() { $('login').hidden=false; $('manager').hidden=true; $('signout').hidden=true; $('loading').hidden=true; }
  async function api(path, options={}) {
    const headers = {...options.headers};
    if (options.body) headers['Content-Type']='application/json';
    if (csrf) headers['X-CSRF-Token']=csrf;
    const response = await fetch(path,{...options,headers,credentials:'same-origin'});
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) { csrf=''; showLogin(); }
      const detail = typeof data.detail === 'string' ? data.detail : response.status === 422 ? 'Check the fields and try again.' : 'Unable to complete this request. Try again.';
      throw new Error(detail);
    }
    return data;
  }
  function status(title, text, state='running') {
    $('validation').hidden=false; $('validation').dataset.state=state;
    $('status-title').textContent=title; $('status-message').textContent=text || '';
    $('status-icon').textContent=state==='success' ? '✓' : state==='error' ? '!' : '•';
  }
  function renderChecks(checks, steps=[]) {
    $('checks').replaceChildren();
    const rows = Object.keys(checks).length ? Object.entries(checks) : steps.map(s=>[s.name,s.status]);
    const titles={checkout:'Repository',repo_prepare:'Repository',repo:'Repository',odoo_shell:'Odoo connection',odoo_https:'Odoo HTTPS',odoosh:'Odoo.sh SSH',ssh:'Odoo.sh SSH',addons_path:'Addons path'};
    for (const [name,value] of rows) {
      const li=document.createElement('li'); const ok=['ok','passed'].includes(value), failed=['failed','error'].includes(value);
      const icon=document.createElement('span'); icon.className=ok?'check-pass':failed?'check-fail':''; icon.textContent=ok?'✓ ':failed?'! ':'• ';
      li.append(icon,document.createTextNode((titles[name] || name.replaceAll('_',' ')) + ' · ' + value)); $('checks').append(li);
    }
  }
  function fill(detail) {
    current=detail; $('client-title').textContent=detail.name;
    $('client-meta').textContent=[detail.values.branch,detail.values.environment].filter(Boolean).join(' · ');
    for (const {name,type} of fields) {
      const input=$(name), value=detail.values[name];
      if (type==='password') { input.value=''; input.placeholder=detail.configured_secrets[name]?'Configured — leave blank to keep':'Not configured'; }
      else if (type==='checkbox') input.checked=value == null ? true : value===true || String(value).toLowerCase()==='true';
      else input.value=value == null ? (name==='odoosh_ssh_port'?'22':'') : String(value);
    }
    markDirty(false);
  }
  async function selectClient(name) {
    pollGeneration++; message(''); $('validation').hidden=true; $('checks').replaceChildren(); setBusy(true);
    try {
      fill(await api('/admin/clients/'+encodeURIComponent(name)));
      if (current.validation.validated) status('Previously validated','Save & Validate to check the current connection.','success');
    } finally { setBusy(false); }
  }
  async function openManager() {
    const result=await api('/admin/clients'); $('client-select').replaceChildren();
    for (const row of result.clients) { if (!row.name) continue; const option=document.createElement('option'); option.value=row.name; option.textContent=row.name; $('client-select').append(option); }
    $('login').hidden=true; $('manager').hidden=false; $('signout').hidden=false; $('loading').hidden=true;
    if ($('client-select').value) await selectClient($('client-select').value);
    else { current=null; setBusy(true); message('No clients are configured on this server yet.'); }
  }
  $('odoosh_ssh_host').addEventListener('change',()=>{
    const match=$('odoosh_ssh_host').value.trim().match(/^(?:ssh\s+)?([\w.-]+)@([\w.-]+)(?:\s+-p\s+(\d+))?$/);
    if (match) { $('odoosh_ssh_user').value=match[1]; $('odoosh_ssh_host').value=match[2]; if(match[3]) $('odoosh_ssh_port').value=match[3]; }
  });
  $('client-form').addEventListener('input',()=>markDirty(true));
  $('client-form').addEventListener('change',()=>markDirty(true));
  $('client-select').addEventListener('change',async()=>{
    if (dirty && !window.confirm('Discard unsaved changes for this client?')) { $('client-select').value=current.name; return; }
    try { await selectClient($('client-select').value); } catch(error) { message(error.message); }
  });
  async function poll(jobId, generation) {
    const deadline=Date.now()+5*60*1000;
    while (generation===pollGeneration && Date.now()<deadline) {
      const job=await api('/admin/validation/'+encodeURIComponent(jobId));
      if (generation!==pollGeneration) return;
      renderChecks(job.checks,job.steps);
      if (job.terminal) {
        const ok=job.validated || job.coding_ready;
        status(ok ? (job.deploy_ready?'Client ready':'Coding ready · deployment needs attention') : 'Connection needs attention',job.summary || 'Review the connection settings and try again.',ok&&job.deploy_ready?'success':'error');
        fill(await api('/admin/clients/'+encodeURIComponent(current.name))); return;
      }
      status('Checking connection…','Settings saved. Checking repository, Odoo and SSH access.');
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
    if (generation===pollGeneration) status('Check is still running','Settings are saved. Reopen this client or retry validation later to review the result.');
  }
  async function save(validate) {
    if (busy || !current || !$('client-form').reportValidity()) return;
    setBusy(true); message(''); $('checks').replaceChildren(); const generation=++pollGeneration;
    status('Saving settings…','Your existing credentials will be preserved.');
    try {
      const values={};
      for (const {name,type} of fields) {
        const input=$(name);
        if (type==='password') { if(input.value) values[name]=input.value; }
        else if(type==='checkbox') values[name]=input.checked;
        else if(type==='number') values[name]=Number(input.value);
        else values[name]=input.value;
        if(type !== 'password') {
          const initial=current.values[name];
          const equivalent=type==='checkbox' ? values[name]===(initial==null?true:initial===true||String(initial).toLowerCase()==='true') : type==='number' ? values[name]===Number(initial??22) : values[name]===String(initial??'');
          if(equivalent) delete values[name];
        }
      }
      const result=await api('/admin/clients/'+encodeURIComponent(current.name),{method:'PATCH',body:JSON.stringify({revision:current.revision,values})});
      fill(result.client);
      if (!validate) { status('Settings saved','New settings are active. Choose Save & Validate to check connectivity.','success'); return; }
      status('Checking connection…','Settings saved. Starting the connection check.');
      const job=await api('/admin/clients/'+encodeURIComponent(current.name)+'/validate',{method:'POST',body:'{}'});
      if (job.job_id) await poll(job.job_id,generation);
      else status('Settings saved',job.summary || 'Connection check completed.',job.validated?'success':'error');
    } catch(error) { message(error.message); status('Action needs attention',error.message,'error'); }
    finally { setBusy(false); }
  }
  $('client-form').addEventListener('submit',event=>{ event.preventDefault(); save(true); });
  $('save-only').addEventListener('click',()=>save(false));
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault(); message(''); const button=$('login-form').querySelector('button'); button.disabled=true;
    try { const result=await api('/admin/session',{method:'POST',body:JSON.stringify({token:$('admin-token').value})}); $('admin-token').value=''; csrf=result.csrf_token; await openManager(); }
    catch(error) { message(error.message); } finally { button.disabled=false; }
  });
  $('signout').addEventListener('click',async()=>{ try { await api('/admin/session',{method:'DELETE'}); csrf=''; current=null; pollGeneration++; $('client-form').reset(); markDirty(false); showLogin(); } catch(error) { message(error.message); } });
  window.addEventListener('beforeunload',event=>{ if(dirty){event.preventDefault();event.returnValue='';} });
  (async()=>{
    const code=new URLSearchParams(location.hash.slice(1)).get('login');
    if (location.hash) history.replaceState(null,'',location.pathname);
    try {
      const session=code ? await api('/admin/session',{method:'POST',body:JSON.stringify({login_code:code})}) : await api('/admin/session');
      csrf=session.csrf_token; await openManager();
    } catch(error) { showLogin(); if(code) message(error.message); }
    finally { $('loading').hidden=true; }
  })();
})();
