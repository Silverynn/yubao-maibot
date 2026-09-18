import base64,io,json,time,concurrent.futures
from pathlib import Path
from PIL import Image,ImageDraw
import httpx
key=Path('.secrets/agnes_api_key.txt').read_text().strip()
im=Image.new('RGB',(240,120),'white');d=ImageDraw.Draw(im);d.rectangle((10,20,90,100),fill='red');d.ellipse((140,20,220,100),fill='blue');b=io.BytesIO();im.save(b,format='PNG')
url='data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
models=['agnes-3.0-flash','agnes-2.5-flash','agnes-2.0-flash','agnes-2.5-pro','agnes-2.5-pro-alpha','agnes-2.5-pro-beta']
def test(model):
    results=[]
    with httpx.Client(timeout=35) as c:
        for kind in ('text','vision'):
            content='Reply in Chinese in one short sentence: someone says hello to you.' if kind=='text' else [{'type':'text','text':'Describe the colors and shapes on the left and right of this image. Be brief.'},{'type':'image_url','image_url':{'url':url}}]
            start=time.monotonic()
            try:
                r=c.post('https://apihub.agnes-ai.com/v1/chat/completions',headers={'Authorization':'Bearer '+key},json={'model':model,'messages':[{'role':'user','content':content}],'max_tokens':160,'stream':False})
                try: payload=r.json()
                except ValueError: payload={'error':r.text[:200]}
                choices=payload.get('choices') or [{}]
                result={'model':model,'test':kind,'status':r.status_code,'seconds':round(time.monotonic()-start,2),'reply':choices[0].get('message',{}).get('content'),'error':payload.get('error')}
            except Exception as e: result={'model':model,'test':kind,'error':type(e).__name__}
            results.append(result);print(json.dumps(result,ensure_ascii=True),flush=True)
    return results
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results=[r for group in pool.map(test,models) for r in group]
Path('.runtime/agnes_api_test_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
