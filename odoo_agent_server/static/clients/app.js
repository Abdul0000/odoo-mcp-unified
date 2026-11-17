'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let csrf = '', current = null, dirty = false, busy = false, pollGeneration = 0, creating = false, defaults = {};
  let view = 'clients', runtime = null, runtimeDirty = false, controllerAvailable = false, activityGeneration = 0;
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
    for (const id of ['save-validate','save-only','client-select','new-client','server-update','server-restart','services-update','services-restart','save-settings','reload-settings']) $(id).disabled = value;
    for (const field of fields) $(field.name).disabled = value;
    $('client-name').disabled = value || !creating;
    for(const input of $('runtime-fields').querySelectorAll('input')) input.disabled=value;
    for(const id of ['services-update','services-restart']) $(id).disabled=value||!controllerAvailable;
  }
  function showLogin() { $('login').hidden=false; $('manager').hidden=true; $('signout').hidden=true; $('server-menu').hidden=true; $('loading').hidden=true; }
  async function api(path, options={}) {
    const headers = {...options.headers};
    if (options.body) headers['Content-Type']='application/json';
    if (csrf) headers['X-CSRF-Token']=csrf;
    const response = await fetch(path,{...options,headers,credentials:'same-origin'});
    const data = await response.json().catch(()=>({detail:'Server is temporarily unavailable. Please retry shortly.'}));
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
    $('status-icon').textContent=state==='success' ? '✓' : ['error','warning'].includes(state) ? '!' : '•';
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
    current=detail; $('client-title').textContent=creating?'New client':detail.name;
    $('new-client-name').hidden=!creating; $('client-name').required=creating; $('client-name').disabled=!creating||busy;
    $('save-validate').textContent=creating?'Add & Validate →':'Save & Validate →'; $('save-only').textContent=creating?'Add client':'Save only';
    $('client-meta').textContent=[detail.values.branch,detail.values.environment].filter(Boolean).join(' · ');
    $('client-source').textContent=creating?'New client':detail.settings_source==='saved'?'Source: saved Oracle settings':'Source: Oracle environment';
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
      if (current.validation.full_validation_passed) status('Previously validated','Save & Validate to check the current connection.','success');
      else if(current.validation.validated) status('Connection partially ready',current.validation.summary || 'Review the separate coding and connection checks.','warning');
    } finally { setBusy(false); }
  }
  async function loadClients(selected) {
    const result=await api('/admin/clients'); defaults=result.defaults || {}; $('client-select').replaceChildren();
    for (const row of result.clients) { if (!row.name) continue; const option=document.createElement('option'); option.value=row.name; option.textContent=row.name; $('client-select').append(option); }
    const filter=$('activity-client'), previous=filter.value; filter.replaceChildren();
    const all=document.createElement('option'); all.value=''; all.textContent='All clients'; filter.append(all);
    for(const row of result.clients) { if(!row.name) continue; const option=document.createElement('option'); option.value=row.name; option.textContent=row.name; filter.append(option); }
    filter.value=previous;
    if(selected) $('client-select').value=selected;
  }
  function newClient() {
    if(dirty && !window.confirm('Discard unsaved changes?')) return;
    creating=true; pollGeneration++; message(''); $('validation').hidden=true; $('client-select').selectedIndex=-1;
    $('client-name').value='';
    fill({name:'',revision:null,values:{...defaults,path:''},configured_secrets:{},validation:{}});
    $('client-meta').textContent='New connection'; setBusy(false); $('client-name').focus();
  }
  async function openManager() {
    await loadClients();
    $('login').hidden=true; $('manager').hidden=false; $('signout').hidden=false; $('loading').hidden=true;
    if ($('client-select').value) { creating=false; await selectClient($('client-select').value); }
    else newClient();
    await loadRuntime();
    try { const server=await api('/admin/server'); controllerAvailable=server.available; $('server-menu').hidden=!server.available;
      $('controller-note').textContent=server.available?'Active coding, validation or deployment work blocks maintenance.':'Service controller is unavailable. Ask the Oracle operator to install it.'; setBusy(busy);
      if(server.active_job_id) { setBusy(true); pollServer(server.active_job_id).finally(()=>setBusy(false)); }
    } catch(_) { controllerAvailable=false; $('server-menu').hidden=true; $('controller-note').textContent='Unable to read service controller status.'; setBusy(busy); }
  }
  $('new-client').addEventListener('click',newClient);
  $('odoosh_ssh_host').addEventListener('change',()=>{
    const match=$('odoosh_ssh_host').value.trim().match(/^(?:ssh\s+)?([\w.-]+)@([\w.-]+)(?:\s+-p\s+(\d+))?$/);
    if (match) { $('odoosh_ssh_user').value=match[1]; $('odoosh_ssh_host').value=match[2]; if(match[3]) $('odoosh_ssh_port').value=match[3]; }
  });
  $('client-form').addEventListener('input',()=>markDirty(true));
  $('client-form').addEventListener('change',()=>markDirty(true));
  $('client-select').addEventListener('change',async()=>{
    if (dirty && !window.confirm('Discard unsaved changes for this client?')) { $('client-select').value=current.name; return; }
    creating=false;
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
        const title=job.full_validation_passed?'Client ready':ok?(job.deploy_ready?'Coding & deployment ready · HTTPS needs attention':'Coding ready · deployment needs attention'):'Connection needs attention';
        status(title,job.summary || 'Review the connection settings and try again.',job.full_validation_passed?'success':ok?'warning':'error');
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
      const wasCreating=creating;
      const result=await api(wasCreating?'/admin/clients':'/admin/clients/'+encodeURIComponent(current.name),{method:wasCreating?'POST':'PATCH',body:JSON.stringify(wasCreating?{name:$('client-name').value,values}:{revision:current.revision,values})});
      creating=false; fill(result.client); if(wasCreating) await loadClients(result.client.name);
      if (!validate) { status('Settings saved','New settings are active. Choose Save & Validate to check connectivity.','success'); return; }
      status('Checking connection…','Settings saved. Starting the connection check.');
      const job=await api('/admin/clients/'+encodeURIComponent(current.name)+'/validate',{method:'POST',body:'{}'});
      if (job.job_id) await poll(job.job_id,generation);
      else status('Settings saved',job.summary || 'Connection check completed.',job.validated?'success':'error');
    } catch(error) { message(error.message); status('Action needs attention',error.message,'error'); }
    finally { setBusy(false); }
  }
  async function pollServer(id) {
    $('server-status').hidden=false; const deadline=Date.now()+15*60*1000;
    while(Date.now()<deadline) {
      try {
        const job=await api('/admin/server/'+id);
        $('server-title').textContent=job.status==='succeeded'?'Oracle services ready':job.status==='failed'?'Server action needs attention':'Working on Oracle…';
        $('server-message').textContent=job.message || 'Waiting for the server…';
        if(job.terminal) { $('server-status').dataset.state=job.status==='succeeded'?'success':'error'; if(job.status==='succeeded'&&!runtimeDirty) await loadRuntime(); return; }
      } catch(error) { $('server-message').textContent='Services are restarting. Reconnecting automatically…'; }
      await new Promise(resolve=>setTimeout(resolve,2000));
    }
    $('server-message').textContent='This is taking longer than expected. Check Oracle service logs before retrying.';
  }
  async function serverAction(action) {
    if(busy) return;
    if((dirty||runtimeDirty)&&!window.confirm('There are unsaved settings. Continue maintenance without saving them?')) return;
    const question=action==='update'?'Update MCP from master, run deployment checks, and restart Oracle services?':'Restart the MCP API and worker on Oracle? Active work will block this action.';
    if(!window.confirm(question)) return;
    $('server-menu').open=false; setBusy(true); message('');
    $('server-status').hidden=false; $('server-title').textContent='Requesting Oracle action…'; $('server-message').textContent='Checking for active work.';
    try { const job=await api('/admin/server',{method:'POST',body:JSON.stringify({action})}); await pollServer(job.id); }
    catch(error) { $('server-status').dataset.state='error'; $('server-title').textContent='Server action blocked'; $('server-message').textContent=error.message; }
    finally { setBusy(false); }
  }
  function renderRuntime() {
    if(!runtime) return;
    $('runtime-fields').replaceChildren();
    $('restart-badge').hidden=!runtime.restart_required;
    $('runtime-title').textContent=view==='storage'?'Storage & retention':'Runtime & performance';
    for(const field of runtime.fields) {
      if(field.group!==view) continue;
      const wrapper=document.createElement('div'); wrapper.className='field';
      const label=document.createElement('label'); label.htmlFor='runtime-'+field.name; label.textContent=field.label;
      const input=document.createElement('input'); input.id=label.htmlFor; input.type='number'; input.min=field.min; input.max=field.max; input.step='1'; input.required=true; input.value=runtime.values[field.name]; input.disabled=busy;
      input.addEventListener('input',()=>{runtime.values[field.name]=input.value; runtimeDirty=true; $('settings-message').textContent='Unsaved server settings';});
      const hint=document.createElement('p'); hint.className='hint'; hint.textContent=field.help;
      if(runtime.values[field.name]!==runtime.effective[field.name]) hint.textContent+=' Running: '+runtime.effective[field.name]+'.';
      const env=document.createElement('span'); env.className='env-key'; env.textContent=field.env+' · '+field.source;
      wrapper.append(label,input,hint,env); $('runtime-fields').append(wrapper);
    }
  }
  async function loadRuntime() {
    try {
      runtime=await api('/admin/settings'); runtimeDirty=false; renderRuntime();
      $('system-overview').replaceChildren();
      const labels={execution_enabled:'Command execution',functional_operations_enabled:'Functional operations',approval_required:'Approval guard',github_token_configured:'Global repository token',browser_configured:'Browser connection',state_dir:'State directory',execution_root:'Execution directory'};
      for(const [key,value] of Object.entries(runtime.system)) {
        const row=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd'); dt.textContent=labels[key]||key; dd.textContent=typeof value==='boolean'?(value?'Enabled':'Disabled'):value; row.append(dt,dd); $('system-overview').append(row);
      }
      $('settings-message').textContent=runtime.restart_required?'Saved changes are waiting for a service restart.':'';
    } catch(error) { message(error.message); }
  }
  function switchView(next) {
    view=next; message('');
    const titles={clients:['Client manager','Manage real client connections and validate access.'],runtime:['Runtime & performance','Tune worker capacity, queues and cache lifetimes.'],storage:['Storage & retention','Control how long sessions, jobs and artifacts are kept.'],services:['Oracle services','Apply server settings and manage MCP releases.'],tasks:['Tasks','Resume from persistent checkpoints and review verification evidence.'],diagnostics:['Diagnostics','Evidence-linked debugging sessions and concrete blockers.'],deployments:['Deployments','Track source, upgrade and live deployment proof separately.'],knowledge:['Client knowledge','Configuration and source-bound facts from this client.']};
    $('page-title').textContent=titles[next][0]; $('page-description').textContent=titles[next][1];
    $('activity-view').hidden=!['tasks','diagnostics','deployments','knowledge'].includes(next);
    if(!$('activity-view').hidden) loadActivity();
    $('clients-view').hidden=next!=='clients'; $('settings-view').hidden=!['runtime','storage'].includes(next); $('services-view').hidden=next!=='services';
    for(const button of document.querySelectorAll('[data-view]')) { if(button.dataset.view===next) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current'); }
    renderRuntime();
  }
  function element(tag,text,className) { const node=document.createElement(tag); if(text!=null) node.textContent=String(text); if(className) node.className=className; return node; }
  function evidenceRows(parent,rows) {
    const list=element('ul',null,'evidence-list');
    for(const row of rows) list.append(element('li',row));
    parent.append(list);
  }
  async function loadActivity(refreshKnowledge=false) {
    const generation=++activityGeneration, mode=view, client=$('activity-client').value;
    $('activity-title').textContent={tasks:'Tasks & workspaces',diagnostics:'Debugging evidence',deployments:'Deployment history',knowledge:'Client knowledge'}[mode]||'Activity';
    $('activity-state').textContent='Loading server evidence…'; $('activity-content').replaceChildren();
    try {
      if(mode==='knowledge') {
        if(!client) { $('activity-state').textContent='Select one client to view its knowledge.'; return; }
        const result=await api('/admin/knowledge/'+encodeURIComponent(client)+(refreshKnowledge?'/refresh':''),refreshKnowledge?{method:'POST',body:'{}'}:{});
        if(generation!==activityGeneration||view!==mode) return;
        const card=element('article',null,'activity-item'); card.append(element('h3',result.client_name));
        evidenceRows(card,['Branch: '+result.branch,'Source SHA: '+(result.source_sha||'unavailable'),'Source: '+(result.source_clean?'clean':'unverified or changed'),'Runtime facts: '+(result.runtime_facts?'authenticated HTTPS snapshot':'not verified'), 'Base module version: '+(result.runtime_facts?.base_module_version||'not verified')]);
        if(result.runtime_error) card.append(element('p',result.runtime_error));
        if(result.source_error) card.append(element('p',result.source_error));
        if(result.source_modules?.length) { const detail=element('details'),summary=element('summary','Source modules ('+result.source_modules.length+')'); detail.append(summary); evidenceRows(detail,result.source_modules.map(m=>m.module+' · depends: '+m.depends.join(', '))); card.append(detail); }
        const button=element('button','Refresh source & runtime facts','secondary'); button.type='button'; button.addEventListener('click',async()=>{button.disabled=true; await loadActivity(true);}); card.append(button);
        $('activity-content').append(card); $('activity-state').textContent='Facts are bound to this client configuration and source revision. '+(result.cache_hit?'Using a saved snapshot.':'Use Refresh source & runtime facts to fetch current metadata.'); return;
      }
      const result=await api('/admin/activity'+(client?'?client_name='+encodeURIComponent(client):''));
      if(generation!==activityGeneration||view!==mode) return;
      const rows=result[mode]||[];
      $('activity-state').textContent=rows.length?rows.length+' '+mode+' · persisted evidence':'No '+mode+' recorded for this selection.';
      for(const row of rows) {
        const card=element('article',null,'activity-item'),heading=element('div',null,'activity-heading');
        heading.append(element('h3',mode==='tasks'?(row.description||row.task_id):(row.module_name||row.id||row.job_id)),element('span',mode==='deployments'?'Job: '+(row.status||'unknown'):(row.status||row.phase||'unknown'),'tag')); card.append(heading);
        const when=row.updated_at||row.finished_at||row.created_at;
        card.append(element('p',[row.client_name,row.branch,when?new Date(when).toLocaleString():null].filter(Boolean).join(' · ')));
        if(mode==='tasks') {
          evidenceRows(card,['Target: '+row.target,'Phase: '+row.phase,'Coding verified: '+Boolean(row.claims?.coding_verified),'Push verified: '+Boolean(row.claims?.push_verified),'Deployment verified: '+Boolean(row.claims?.deployment_verified),'Functional verified: '+Boolean(row.claims?.functional_verified),'Task ID: '+row.task_id]);
          if(row.commit_sha) card.append(element('code',row.commit_sha));
          if(!row.configuration_current) card.append(element('p','Client settings changed; old claims are invalid. Open a new task and inspect preserved work.'));
          const details=element('details'),summary=element('summary','Timeline ('+(row.events||[]).length+')'); details.append(summary); evidenceRows(details,(row.events||[]).map(e=>new Date(e.at).toLocaleTimeString()+' · '+e.tool+' · '+e.status)); card.append(details);
          const evidenceButton=element('button','View checks, diff & logs','secondary'); evidenceButton.type='button';
          evidenceButton.addEventListener('click',async()=>{
            evidenceButton.disabled=true;
            try {
              const data=await api('/admin/tasks/'+row.task_id+'/evidence');
              const detail=element('details'); detail.open=true; detail.append(element('summary','Workspace evidence'));
              if(data.message) detail.append(element('p',data.message));
              evidenceRows(detail,data.checks.map(c=>c.name+' · '+c.layer+' · '+c.status+' · '+c.command));
              detail.append(element('pre',data.diff_available?(data.diff||'No tracked working-tree diff. Untracked files remain listed in workspace status.'):(data.diff_message||'Diff unavailable while work is running.'),'evidence-code'));
              if(data.diff_truncated) detail.append(element('p','Diff preview is limited to 2,000 characters.'));
              for(const command of data.commands) {detail.append(element('p',command.command+' · '+command.status+' · exit '+command.exit_code),element('pre',command.output||'No captured output.','evidence-code'));}
              card.append(detail); evidenceButton.hidden=true;
            }catch(error){message(error.message);evidenceButton.disabled=false;}
          }); card.append(evidenceButton);
          const button=element('button','Reconcile existing work','secondary'); button.type='button'; button.disabled=!row.configuration_current;
          button.addEventListener('click',async()=>{button.disabled=true; try{await api('/admin/tasks/'+row.task_id+'/resume',{method:'POST',body:'{}'}); await loadActivity();}catch(error){message(error.message);button.disabled=false;}}); card.append(button);
        } else if(mode==='deployments') {
          evidenceRows(card,['Phase: '+row.phase,'Source deployed: '+(row.source_deployed==null?'Not recorded':row.source_deployed),'Module upgraded: '+(row.module_upgraded==null?'Not recorded':row.module_upgraded),'Deployment confirmed: '+row.deploy_confirmed,'Job ID: '+row.job_id]);
          if(row.sha) card.append(element('code',row.sha));
          if(row.message) card.append(element('p',row.message));
          const details=element('details'),summary=element('summary','Deployment steps'); details.append(summary); evidenceRows(details,row.steps.map(s=>s.name+' · '+s.status+(s.duration_ms!=null?' · '+s.duration_ms+' ms':'')));card.append(details);
        } else {
          evidenceRows(card,['Origin: '+(row.diagnosis.origin||'unknown'),'Category: '+(row.diagnosis.failure_category||'unknown'),'Verified fixed: '+row.truth_gate.verified_fixed]);
          card.append(element('p',row.diagnosis.crux||'Diagnosis pending'),element('p',row.diagnosis.next_strategy||''));
          const details=element('details'),summary=element('summary','Evidence ('+row.evidence.length+')'); details.append(summary); evidenceRows(details,row.evidence.map(e=>(e.trusted?'Trusted':'Untrusted')+' · '+e.kind+' · '+e.summary)); card.append(details);
        }
        $('activity-content').append(card);
      }
      if(mode==='tasks'&&result.workspaces.length) {
        const details=element('details'),summary=element('summary','Execution workspaces ('+result.workspaces.length+')'); details.append(summary); evidenceRows(details,result.workspaces.map(w=>(w.client_name||'Legacy')+' · '+w.workspace_id+' · '+(w.phase||w.status)+(w.commit?' · '+w.commit:''))); $('activity-content').append(details);
      }
    } catch(error) { if(generation===activityGeneration) $('activity-state').textContent=error.message; }
  }
  $('activity-client').addEventListener('change',()=>loadActivity());
  $('activity-refresh').addEventListener('click',()=>loadActivity());
  $('client-knowledge').addEventListener('click',()=>{if(creating||!current)return; $('activity-client').value=current.name; switchView('knowledge');});
  for(const button of document.querySelectorAll('[data-view]')) button.addEventListener('click',()=>switchView(button.dataset.view));
  $('reload-settings').addEventListener('click',async()=>{if(busy||(runtimeDirty&&!window.confirm('Discard unsaved server settings?'))) return; await loadRuntime();});
  $('runtime-form').addEventListener('submit',async event=>{
    event.preventDefault(); if(busy||!runtime) return;
    const values={};
    for(const field of runtime.fields) {
      const value=Number(runtime.values[field.name]);
      if(!Number.isInteger(value)||value<field.min||value>field.max) { message(field.label+' must be between '+field.min+' and '+field.max); return; }
      values[field.name]=value;
    }
    setBusy(true); message('');
    try {
      runtime=await api('/admin/settings',{method:'PATCH',body:JSON.stringify({revision:runtime.revision,values})}); runtimeDirty=false; renderRuntime();
      $('settings-message').dataset.state='success'; $('settings-message').textContent=runtime.restart_required?'Settings saved. Open Oracle services → Restart services to apply.':'Settings saved. Running configuration is up to date.';
    } catch(error) { $('settings-message').dataset.state='error'; $('settings-message').textContent=error.message; }
    finally {setBusy(false);}
  });
  $('global-server-status').append($('server-status'));
  $('services-restart').addEventListener('click',()=>serverAction('restart'));
  $('services-update').addEventListener('click',()=>serverAction('update'));
  $('server-restart').addEventListener('click',()=>serverAction('restart'));
  $('server-update').addEventListener('click',()=>serverAction('update'));
  $('client-form').addEventListener('submit',event=>{ event.preventDefault(); save(true); });
  $('save-only').addEventListener('click',()=>save(false));
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault(); message(''); const button=$('login-form').querySelector('button'); button.disabled=true;
    try { const result=await api('/admin/session',{method:'POST',body:JSON.stringify({token:$('admin-token').value})}); $('admin-token').value=''; csrf=result.csrf_token; await openManager(); }
    catch(error) { message(error.message); } finally { button.disabled=false; }
  });
  $('signout').addEventListener('click',async()=>{ try { if((dirty||runtimeDirty)&&!window.confirm('Sign out and discard unsaved settings?')) return; await api('/admin/session',{method:'DELETE'}); runtime=null; runtimeDirty=false; activityGeneration++; $('activity-content').replaceChildren(); $('runtime-fields').replaceChildren(); $('system-overview').replaceChildren(); csrf=''; current=null; pollGeneration++; $('client-form').reset(); markDirty(false); showLogin(); } catch(error) { message(error.message); } });
  window.addEventListener('beforeunload',event=>{ if(dirty||runtimeDirty){event.preventDefault();event.returnValue='';} });
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
