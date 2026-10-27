"""Small local update controls, using the existing privileged broker and CSRF guard."""
from flask import jsonify,request

CONTENT=r'''<h1>Software updates</h1>
<p><a href="/">Receiver</a> · <a href="/wifi">Wi-Fi</a> · <a href="/uploads">Uploads</a></p>
<p id="summary">Loading update status…</p>
<label><input style="width:auto" type="checkbox" id="automatic" disabled> Automatic updates</label>
<p>Checks run every six hours when Internet is available. Automatic installation occurs between 3 and 5 a.m. in the Pi's local timezone. Turning this off keeps automatic checks and manual installation available.</p>
<button id="check">Check now</button> <button id="install">Install now</button>
<p>Installing briefly stops reception and restarts the application. Settings and reception history are preserved. The previous application is restored if service/API/database checks fail. No automatic Pi reboot.</p>
<p id="result" role="status"></p><pre id="details" style="white-space:pre-wrap;overflow-wrap:anywhere"></pre>
<script>
const token={{token|tojson}},auto=document.getElementById('automatic'),result=document.getElementById('result');
async function call(value){const r=await fetch('/api/updates',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify(value)});const v=await r.json();if(!r.ok)throw Error(v.error||'Update operation failed');return v;}
async function refresh(){try{const r=await fetch('/api/updates',{cache:'no-store'});const s=await r.json();if(!r.ok)throw Error(s.error||'Update status unavailable');auto.checked=s.automatic;auto.disabled=false;document.getElementById('summary').textContent='Installed: '+s.installed_version+' · Available: '+(s.available_version||'not checked')+' · '+(s.phase||'Ready');document.getElementById('details').textContent='Last check: '+(s.last_check||'never')+'\nLast successful install: '+(s.last_success||'none')+(s.last_error?'\n'+s.last_error:'');}catch(e){result.textContent=e.message;}}
auto.onchange=async()=>{auto.disabled=true;try{await call({operation:'set_auto',enabled:auto.checked});result.textContent=auto.checked?'Automatic installation enabled.':'Automatic installation paused.';}catch(e){result.textContent=e.message;}await refresh();};
for(const action of ['check','install'])document.getElementById(action).onclick=async()=>{if(action==='install'&&!confirm('Install the available stable update now? Reception will pause briefly.'))return;try{await call({operation:action});result.textContent=action==='check'?'Check queued.':'Installation queued. This page may briefly disconnect.';}catch(e){result.textContent=e.message;}};
refresh();setInterval(refresh,5000);
</script>'''

def install(module):
    import field_features as f
    def page():return f.page(CONTENT)
    def api():
        if request.method=='GET':value={'operation':'status'}
        else:
            module.csrf();value=request.get_json(silent=True)
            if not isinstance(value,dict):return jsonify(error='Invalid update request'),400
        if value.get('operation') not in ('status','set_auto','check','install'):return jsonify(error='Invalid update action'),400
        try:
            return jsonify(f.broker('update',**value))
        except Exception as exc:return jsonify(error=str(exc)),503
    module.app.add_url_rule('/updates','updates_page',page)
    module.app.add_url_rule('/api/updates','updates_api',api,methods=['GET','POST'])
