import base64, io, json
from pathlib import Path
from PIL import Image
import httpx
key=Path('.secrets/deepseek_api_key.txt').read_text().strip()
b=io.BytesIO(); Image.new('RGB',(64,64),(255,0,0)).save(b,format='PNG')
headers={'Authorization':'Bearer '+key}
with httpx.Client(timeout=40) as c:
    r=c.post('https://api.deepseek.com/v1/chat/completions',headers=headers,json={'model':'deepseek-flash','messages':[{'role':'user','content':[{'type':'text','text':'Name only the main color in this image.'},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()}}]}],'max_tokens':32,'thinking':{'type':'disabled'}})
    print('VISION_STATUS',r.status_code); print(r.json().get('choices',[{}])[0].get('message',{}).get('content',r.json().get('error',{})))
    r=c.post('https://api.deepseek.com/v1/embeddings',headers=headers,json={'model':'deepseek-flash','input':'test'})
    print('EMBEDDING_STATUS',r.status_code); print(r.text[:300] if r.status_code!=200 else 'vector returned')
