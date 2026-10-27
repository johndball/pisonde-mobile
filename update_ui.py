"""Small local update controls, using the existing privileged broker and CSRF guard."""
from flask import jsonify,request

CONTENT=r'''<h1>Software updates</h1>
<p><a href="/">Receiver</a> · <a href="/wifi">Wi-Fi</a> · <a href="/uploads">Uploads</a></p>
<p id="summary">Loading update status…</p>
<fieldset><legend>Automatic installation</legend>
<label><input style="width:auto" type="checkbox" id="scheduled" disabled> Install during scheduled maintenance window</label>
<p><label>Maintenance window <select id="window" disabled></select></label> (Pi local time)</p>
<p><label><input style="width:auto" type="checkbox" id="reboot_if_missed" disabled> If update window missed, install at next reboot</label></p>
<p><label><input style="width:auto" type="checkbox" id="immediate" disabled> Immediately install pending updates when a new version is available</label></p>
<button id="save-policy" disabled>Save update settings</button>
<p id="policy-result" role="status"></p></fieldset>
<p>Choose any combination. Immediate installation takes priority. With all three unchecked, checks and manual installation remain available. Checks run every six hours; a new version installs immediately when detected if enabled, including Check now. The reboot option applies to an update already known before a maintenance window ended; the worker runs about 10–15 minutes after boot and retries when needed. No automatic Pi reboot.</p>
<button id="check">Check now</button> <button id="install">Install now</button>
<p>Installing briefly stops reception and restarts the application. Settings and reception history are preserved. The previous application is restored if service/API/database checks fail. No automatic Pi reboot.</p>
<p id="result" role="status"></p><pre id="details" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>
<script>
const token={{token|tojson}},result=document.getElementById('result');
let pending=null,refreshing=false,dirty=false,saving=false;
const optionNames=['scheduled','reboot_if_missed','immediate'],windowSelect=document.getElementById('window'),save=document.getElementById('save-policy');
for(let start=1;start<24;start+=2){const end=(start+2)%24,option=document.createElement('option');option.value=start;option.textContent=String(start).padStart(2,'0')+'00–'+String(end).padStart(2,'0')+'00';windowSelect.append(option);}
for(const element of [...optionNames.map(id=>document.getElementById(id)),windowSelect])element.onchange=()=>{dirty=true;save.disabled=false;document.getElementById('policy-result').textContent='Unsaved settings';};
async function bounded(url,options={}){const abort=new AbortController(),timer=setTimeout(()=>abort.abort(),10000);try{return await fetch(url,{...options,signal:abort.signal,cache:'no-store'});}catch(e){if(e.name==='AbortError')throw Error('Connection timed out. Status will retry automatically.');throw e;}finally{clearTimeout(timer);}}
async function call(value){const r=await bounded('/api/updates',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(value)});const v=await r.json();if(!r.ok)throw Error(v.error||'Update operation failed');return v;}
function render(s){
 if(!dirty&&!saving){for(const id of optionNames)document.getElementById(id).checked=!!s[id];windowSelect.value=s.window_start;}
 for(const id of optionNames)document.getElementById(id).disabled=saving;
 windowSelect.disabled=saving;save.disabled=saving||!dirty;
 document.getElementById('summary').textContent='Installed: '+s.installed_version+' · Available: '+(s.available_version||'not checked')+' · '+(s.phase||'Ready')+(s.running_version&&s.running_version!==s.installed_version?' · Running: '+s.running_version:'');
 document.getElementById('details').textContent='Last check: '+(s.last_check||'never')+'\nLast successful install: '+(s.last_success||'none')+(s.available_commit?'\nAvailable source: '+s.available_commit.slice(0,12):'')+(s.last_error?'\n'+s.last_error:'');
 if(pending){
  const matching=s.operation_id===pending.id,elapsed=Date.now()-pending.start;
  if(matching&&s.completed_at){result.textContent=(s.phase||'Completed')+(s.last_error?': '+s.last_error:'');pending=null;}
  else if(elapsed>pending.limit||elapsed>90000&&(!matching||!s.started_at)){result.textContent='Update operation timed out or did not start. Check the connection and worker log; automatic status refresh continues.';pending=null;}
  else result.textContent=matching?(s.phase||'Working…'):'Waiting for update worker…';
 }else result.textContent=(s.phase||'Ready')+(s.last_error?': '+s.last_error:'');
 document.getElementById('check').disabled=!!pending;document.getElementById('install').disabled=!!pending;
}
async function refresh(){if(refreshing)return;refreshing=true;try{const r=await bounded('/api/updates');const s=await r.json();if(!r.ok)throw Error(s.error||'Update status unavailable');render(s);}catch(e){result.textContent=e.message+' Retrying…';if(pending&&Date.now()-pending.start>pending.limit){pending=null;document.getElementById('check').disabled=false;document.getElementById('install').disabled=false;result.textContent='Update status timed out. Automatic refresh continues; check the connection.';}}finally{refreshing=false;}}
save.onclick=async()=>{saving=true;save.disabled=true;const cfg={};for(const id of optionNames){const element=document.getElementById(id);cfg[id]=element.checked;element.disabled=true;}cfg.window_start=Number(windowSelect.value);cfg.window_end=(cfg.window_start+2)%24;windowSelect.disabled=true;
 try{await call({operation:'set_policy',settings:cfg});dirty=false;document.getElementById('policy-result').textContent='Update settings saved.';}catch(e){document.getElementById('policy-result').textContent=e.message;}finally{saving=false;await refresh();}};
for(const action of ['check','install'])document.getElementById(action).onclick=async()=>{
 if(action==='install'&&!confirm('Install the available stable update now? Reception will pause briefly.'))return;
 document.getElementById('check').disabled=true;document.getElementById('install').disabled=true;result.textContent='Queueing '+action+'…';
 try{const value=await call({operation:action});pending={id:value.operation_id,start:Date.now(),limit:action==='check'?360000:900000};result.textContent='Queued; waiting for worker…';await refresh();}
 catch(e){result.textContent=e.message;document.getElementById('check').disabled=false;document.getElementById('install').disabled=false;}
};
refresh();setInterval(refresh,2000);
</script>'''

def install(module):
    import field_features as f
    def page():return f.page(CONTENT)
    def api():
        if request.method=='GET':value={'operation':'status'}
        else:
            module.csrf();value=request.get_json(silent=True)
            if not isinstance(value,dict):return jsonify(error='Invalid update request'),400
        if value.get('operation') not in ('status','set_auto','set_policy','check','install'):return jsonify(error='Invalid update action'),400
        try:
            data=f.broker('update',**value)
            if value.get('operation')=='status':data['running_version']=module.VERSION
            return jsonify(data)
        except Exception as exc:return jsonify(error=str(exc)),503
    @module.app.after_request
    def no_cache(response):
        if request.path in ('/updates','/api/updates'):response.headers['Cache-Control']='no-store'
        return response
    module.app.add_url_rule('/updates','updates_page',page)
    module.app.add_url_rule('/api/updates','updates_api',api,methods=['GET','POST'])
