"""Раздельные режимы приёмки: настоящий HTTP по умолчанию, явный адаптер среды."""
import base64
import re
import urllib.error
import urllib.request


def mount(page, root, server, bridge=False):
    base='http://127.0.0.1:'+str(server.server_port)
    if not bridge:
        page.goto(base+'/#'+server.key)
        return
    # Режим не проверяет Origin/cookies/CSP браузера; эти проверки выполняются в CI.
    proxy=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(payload):
        req=urllib.request.Request(base+payload['path'],headers={'Cookie':'arktika_session='+server.key,
            'Origin':base,'Content-Type':'application/json','X-Arktika-Request':'1'},
            data=payload['body'].encode() if payload.get('body') is not None else None,method=payload.get('method','GET'))
        try:response=proxy.open(req,timeout=30)
        except urllib.error.HTTPError as exc:response=exc
        with response:return dict(status=response.status,headers=dict(response.headers),body=base64.b64encode(response.read()).decode())
    page.expose_function('httpBridge',request)
    html=(root/'static/index.html').read_text(encoding='utf-8')
    html=re.sub(r'<script[^>]+src=[^>]+></script>','',html);html=re.sub(r'<link[^>]+>','',html)
    css=''.join((root/'static'/name).read_text(encoding='utf-8') for name in ('style.css','catalog.css'))
    page.set_content(html.replace('</head>','<style>'+css+'</style></head>'))
    page.evaluate('''() => {
      window.fetch=async(path,options={})=>{const r=await httpBridge({path:String(path),method:options.method||'GET',body:options.body??null});return new Response(Uint8Array.from(atob(r.body),c=>c.charCodeAt(0)),{status:r.status,headers:r.headers});};
      const setter=Element.prototype.setAttribute;
      Element.prototype.setAttribute=function(name,value){
        if((name==='href'||name==='src')&&(this.tagName==='image'||this.tagName==='IMG')&&String(value).startsWith('/')){const wanted=String(value);this.__wanted=wanted;fetch(wanted).then(r=>r.blob()).then(b=>{if(this.__wanted===wanted)setter.call(this,name,URL.createObjectURL(b));});}
        else setter.call(this,name,value);
      };
    }''')
    for name in ('app.js','catalog.js','product_flow.js','studio.js'):
        page.add_script_tag(content=(root/'static'/name).read_text(encoding='utf-8'))
